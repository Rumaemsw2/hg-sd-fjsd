import logging
import os
import random
import re
from urllib.parse import quote, unquote

import aiohttp

log = logging.getLogger("flow-ai-kpc")

IMAGE_GEN = os.getenv("IMAGE_GEN", "1") == "1"
# Бесплатный генератор без ключа.
IMAGE_URL = "https://image.pollinations.ai/prompt/{prompt}?width=1024&height=1024&nologo=true"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


async def generate_image(prompt: str):
    """Генерация картинки (AI)."""
    if not IMAGE_GEN:
        return None
    url = IMAGE_URL.format(prompt=quote(prompt[:500]))
    try:
        timeout = aiohttp.ClientTimeout(total=90)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(url) as r:
                if r.status != 200 or not r.headers.get("Content-Type", "").startswith("image"):
                    log.warning("Image gen failed: %s", r.status)
                    return None
                return await r.read()
    except Exception:
        log.exception("Image gen error")
        return None


async def _bing_image_urls(session: aiohttp.ClientSession, query: str, limit: int = 12) -> list[str]:
    """Прямые URL картинок из Bing Images (async endpoint)."""
    url = (
        "https://www.bing.com/images/async"
        f"?q={quote(query)}&first=0&count={limit}&mmasync=1"
    )
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bing.com/images/search?q=" + quote(query),
    }
    async with session.get(url, headers=headers) as r:
        if r.status != 200:
            log.warning("Bing images status %s", r.status)
            return []
        text = await r.text()

    # murl — полный URL оригинала в HTML async-ответа
    urls = re.findall(r'murl&quot;:&quot;(https?://[^&]+?)&quot;', text)
    if not urls:
        urls = re.findall(r'"murl"\s*:\s*"(https?://[^"]+)"', text)
    if not urls:
        urls = re.findall(r"murl&quot;:&quot;([^&]+)", text)

    cleaned = []
    seen = set()
    for u in urls:
        u = unquote(u).replace("\\u002f", "/").strip()
        if not u.startswith("http"):
            continue
        # отсекаем очевидный мусор
        low = u.lower()
        if any(x in low for x in (".svg", "logo", "sprite", "icon", "1x1", "pixel")):
            continue
        if u in seen:
            continue
        seen.add(u)
        cleaned.append(u)
        if len(cleaned) >= limit:
            break
    return cleaned


async def _ddg_image_urls(session: aiohttp.ClientSession, query: str, limit: int = 8) -> list[str]:
    """Fallback: DuckDuckGo Images (часто ловит challenge, поэтому второй)."""
    headers = {
        "User-Agent": UA,
        "Accept": "application/json,text/html,*/*",
        "Referer": "https://duckduckgo.com/",
    }
    page = f"https://duckduckgo.com/?q={quote(query)}&iax=images&ia=images"
    async with session.get(page, headers=headers) as r:
        if r.status != 200:
            return []
        text = await r.text()
    m = re.search(r'vqd=["\']([^"\']+)["\']', text) or re.search(r"vqd=([\d-]+)", text)
    if not m:
        return []
    vqd = m.group(1)
    params = {"l": "us-en", "o": "json", "q": query, "vqd": vqd, "f": ",,,", "p": "1"}
    async with session.get("https://duckduckgo.com/i.js", params=params, headers=headers) as r:
        if r.status != 200:
            return []
        try:
            data = await r.json(content_type=None)
        except Exception:
            return []
    urls = []
    for item in data.get("results") or []:
        img = (item.get("image") or "").strip()
        if img.startswith("http"):
            urls.append(img)
        if len(urls) >= limit:
            break
    return urls


async def _download_image(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        headers = {
            "User-Agent": UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://www.bing.com/",
        }
        async with session.get(url, headers=headers, allow_redirects=True) as r:
            if r.status != 200:
                return None
            ctype = (r.headers.get("Content-Type") or "").lower()
            if not (ctype.startswith("image/") or "octet-stream" in ctype):
                # иногда CDN не ставит content-type — проверим по сигнатуре
                data = await r.read()
                if len(data) < 3000:
                    return None
                if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"RIFF":
                    return data
                return None
            data = await r.read()
            if len(data) < 3000:
                return None
            return data
    except Exception:
        return None


async def search_image(query: str) -> bytes | None:
    """
    Ищет реальную картинку в интернете (Bing → DuckDuckGo) и возвращает байты.
    Не AI-генерация — обычные фото из выдачи.
    """
    query = (query or "").strip()[:200]
    if not query:
        return None

    timeout = aiohttp.ClientTimeout(total=30)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            urls = await _bing_image_urls(s, query, limit=12)
            if not urls:
                log.warning("Bing empty for %r, trying DDG", query)
                urls = await _ddg_image_urls(s, query, limit=10)
            if not urls:
                log.warning("No image URLs for %r", query)
                return None

            random.shuffle(urls)
            for url in urls[:8]:
                data = await _download_image(s, url)
                if data:
                    return data
            log.warning("Could not download any image for %r", query)
            return None
    except Exception:
        log.exception("Image search error")
        return None
