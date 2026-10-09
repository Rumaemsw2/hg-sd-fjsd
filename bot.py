import asyncio
import hmac
import io
import logging
import os
import random
import time
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, BusinessConnection, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ai import chat, extract_facts, parse_reply, should_speak
from media import generate_image, search_image
from memory import Memory

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("flow-ai-kpc")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_NAME = os.getenv("BOT_NAME", "КПК")
AUTONOMOUS_MINUTES = int(os.getenv("AUTONOMOUS_MINUTES", "12"))
SILENCE_SECONDS = int(os.getenv("SILENCE_SECONDS", "10"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
STICKER_SETS = [s.strip() for s in os.getenv("STICKER_SETS", "").split(",") if s.strip()]

bot = Bot(TOKEN)
dp = Dispatcher()
memory = Memory()
last_activity = defaultdict(float)
last_reply = defaultdict(float)
chat_locks = defaultdict(asyncio.Lock)
memory_refreshing = set()
# Business connections and per-chat AI toggles. Toggle state is persisted in SQLite.
business_owners = {}  # connection_id -> business account user_id
business_connections_enabled = {}  # connection_id -> is_enabled

ME = {"id": 0, "username": ""}
admin_target = {}  # user_id -> chat_id, куда сейчас пишет админ от имени бота
login_fails = defaultdict(lambda: [0, 0.0])  # user_id -> [попытки, блок до]
pending_clean = {}  # chat_id -> timestamp, когда запрошена очистка
CLEAN_CONFIRM = "I confirm the deletion of the data."
CLEAN_TIMEOUT = 120  # секунд на подтверждение


def display_name(message: Message) -> str:
    u = message.from_user
    if not u:
        return "пользователь"
    return u.full_name or u.username or "пользователь"


def is_group(message: Message) -> bool:
    return message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}


def is_private(message: Message) -> bool:
    return message.chat.type == ChatType.PRIVATE


# ---------------------------------------------------------------- команды

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        f"Привет. Я {BOT_NAME}. В группах могу участвовать в разговоре без обязательного упоминания."
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Команды:\n"
        "/start — запуск\n"
        "/help — помощь\n"
        "/clean — очистить память бота в этом чате (нужно подтверждение)\n\n"
        "В группе я могу отвечать на обычные сообщения, если считаю реплику уместной. "
        "Понимаю фото, кидаю стикеры и иногда картинки."
    )


@dp.message(Command("clean"))
async def clean_cmd(message: Message):
    chat_id = message.chat.id
    pending_clean[chat_id] = time.time()
    await message.answer(
        "⚠️ Ты собираешься удалить всю память бота в этом чате "
        "(историю сообщений и запомненные факты).\n\n"
        "Это действие необратимо и затронет только этот чат.\n\n"
        f"Чтобы подтвердить, напиши точно:\n`{CLEAN_CONFIRM}`\n\n"
        f"Запрос действует {CLEAN_TIMEOUT} секунд.",
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------- админ-режим

def chat_picker(user_id: int):
    kb = InlineKeyboardBuilder()
    chats = memory.list_chats()
    for chat_id, title in chats:
        kb.button(text=title[:40], callback_data=f"adm:pick:{chat_id}")
    kb.button(text="🔄 Обновить", callback_data="adm:list")
    kb.adjust(1)
    text = "Выбери чат, куда писать от имени бота:" if chats else (
        "Пока нет известных групп. Бот запоминает чат, когда там появляется любое сообщение."
    )
    return text, kb.as_markup()


def exit_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="🚪 Выйти из чата", callback_data="adm:exit")
    return kb.as_markup()


@dp.message(Command("keysi"), F.chat.type == ChatType.PRIVATE)
async def keysi(message: Message, command: CommandObject):
    uid = message.from_user.id
    if not ADMIN_PASSWORD:
        await message.answer("Админ-режим выключен: не задан ADMIN_PASSWORD.")
        return

    given = (command.args or "").strip()
    if given:
        # Пароль не должен оставаться в переписке.
        try:
            await message.delete()
        except Exception:
            pass
        fails = login_fails[uid]
        if time.time() < fails[1]:
            await message.answer("Слишком много попыток. Подожди немного.")
            return
        if not hmac.compare_digest(given.encode(), ADMIN_PASSWORD.encode()):
            fails[0] += 1
            if fails[0] >= 5:
                fails[0], fails[1] = 0, time.time() + 600
            await message.answer("Неверный пароль.")
            return
        login_fails.pop(uid, None)
        memory.add_admin(uid)
    elif not memory.is_admin(uid):
        await message.answer("Формат: /keysi <пароль>")
        return

    text, markup = chat_picker(uid)
    await message.answer(text, reply_markup=markup)


@dp.message(Command("keyout"), F.chat.type == ChatType.PRIVATE)
async def keyout(message: Message):
    admin_target.pop(message.from_user.id, None)
    memory.remove_admin(message.from_user.id)
    await message.answer("Админ-доступ снят.")


@dp.callback_query(F.data.startswith("adm:"))
async def admin_callbacks(cb: CallbackQuery):
    uid = cb.from_user.id
    if not memory.is_admin(uid):
        await cb.answer("Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    action = parts[1]

    if action == "pick":
        chat_id = int(parts[2])
        admin_target[uid] = chat_id
        title = memory.chat_title(chat_id)
        await cb.message.edit_text(
            f"Ты пишешь в «{title}» от имени бота.\n"
            "Всё, что отправишь мне сюда (текст, фото, стикеры), уйдёт туда.",
            reply_markup=exit_keyboard(),
        )
    else:  # exit / list
        admin_target.pop(uid, None)
        text, markup = chat_picker(uid)
        await cb.message.edit_text(text, reply_markup=markup)
    await cb.answer()


def admin_active(message: Message) -> bool:
    return (
        message.chat.type == ChatType.PRIVATE
        and message.from_user is not None
        and message.from_user.id in admin_target
        and not (message.text or "").startswith("/")
    )


@dp.message(admin_active)
async def admin_forward(message: Message):
    target = admin_target[message.from_user.id]
    try:
        await bot.copy_message(
            chat_id=target, from_chat_id=message.chat.id, message_id=message.message_id
        )
        shown = message.text or message.caption or "[медиа]"
        memory.add(target, BOT_NAME, shown)
        last_reply[target] = time.time()
        last_activity[target] = time.time()
    except Exception as e:
        log.exception("Admin send failed")
        await message.answer(f"Не отправилось: {e}", reply_markup=exit_keyboard())


# ---------------------------------------------------------------- Telegram Business

@dp.business_connection()
async def on_business_connection(connection: BusinessConnection):
    business_owners[connection.id] = connection.user.id
    business_connections_enabled[connection.id] = bool(connection.is_enabled)
    log.info(
        "Business connection updated: enabled=%s owner_id=%s",
        connection.is_enabled, connection.user.id,
    )


async def get_business_owner(connection_id: str):
    """Refresh connection metadata if the process started after the connection event."""
    if connection_id in business_owners:
        return business_owners[connection_id]
    try:
        connection = await bot.get_business_connection(connection_id)
        business_owners[connection_id] = connection.user.id
        business_connections_enabled[connection_id] = bool(connection.is_enabled)
        return connection.user.id
    except Exception:
        log.exception("Could not fetch Telegram Business connection metadata")
        return None


@dp.business_message()
async def on_business_message(message: Message):
    connection_id = message.business_connection_id
    if not connection_id:
        return

    owner_id = await get_business_owner(connection_id)
    # Fail closed if connection metadata cannot be verified.
    if owner_id is None or not business_connections_enabled.get(connection_id, False):
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        if message.photo:
            text = "[фото]"
        elif message.sticker:
            text = f"[стикер {message.sticker.emoji or ''}]".strip()
        else:
            return

    # Commands may only be issued by the owner of the connected Business account.
    sender_id = message.from_user.id if message.from_user else None
    command = text.split(maxsplit=1)[0].split("@")[0].lower() if text else ""
    if sender_id is not None and owner_id is not None and sender_id == owner_id:
        if command in {"/ai", "/off"}:
            enabled = command == "/ai"
            memory.set_business_mode(connection_id, message.chat.id, enabled)
            response = (
                "🤖 ИИ включён в этом чате. Чтобы выключить, отправь /off."
                if enabled else "⏹ ИИ выключен в этом чате."
            )
            try:
                await bot.send_message(
                    chat_id=message.chat.id,
                    text=response,
                    business_connection_id=connection_id,
                )
            except Exception:
                log.exception("Failed to acknowledge Business command")
            return
        # Never react to the business account's own outgoing messages.
        return

    # Do not process messages from bots or messages while /ai is disabled.
    if message.from_user and message.from_user.is_bot:
        return
    if not memory.business_mode_enabled(connection_id, message.chat.id):
        return

    if message.sticker:
        sticker = message.sticker
        memory.add_sticker(sticker.file_id, sticker.emoji or "", sticker.set_name or "")
    await handle_incoming(message, text, business_connection_id=connection_id)


# ---------------------------------------------------------------- обычные сообщения

async def download_photo(message: Message):
    try:
        buf = io.BytesIO()
        await bot.download(message.photo[-1], destination=buf)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        log.exception("Photo download failed")
        return None


async def deliver(chat_id: int, raw: str, business_connection_id: str | None = None) -> bool:
    text, stickers, img_prompt, search_query = parse_reply(raw)
    if not (text or stickers or img_prompt or search_query):
        return False

    if text:
        await bot.send_message(chat_id, text, business_connection_id=business_connection_id)
        memory.add(chat_id, BOT_NAME, text)

    # Реальное фото из поиска (DuckDuckGo) — приоритетнее генерации
    if search_query:
        data = await search_image(search_query)
        if data:
            await bot.send_photo(chat_id, BufferedInputFile(data, "image.jpg"), business_connection_id=business_connection_id)
            memory.add(chat_id, BOT_NAME, f"[фото] {search_query[:200]}")
        else:
            log.warning("Search image failed for %r", search_query)
    elif img_prompt:
        data = await generate_image(img_prompt)
        if data:
            await bot.send_photo(chat_id, BufferedInputFile(data, "image.jpg"), business_connection_id=business_connection_id)
            memory.add(chat_id, BOT_NAME, f"[фото] {img_prompt[:200]}")

    if stickers:
        file_id = memory.pick_sticker(stickers[0])
        if file_id:
            try:
                await bot.send_sticker(chat_id, file_id, business_connection_id=business_connection_id)
                memory.add(chat_id, BOT_NAME, f"[стикер {stickers[0]}]")
            except Exception:
                log.exception("Sticker send failed")

    last_reply[chat_id] = time.time()
    return True


async def handle_incoming(message: Message, text: str, business_connection_id: str | None = None):
    chat_id = message.chat.id
    # Business replies are strictly reactive; do not let the ordinary autonomous
    # loop send messages into these chats without a business_connection_id.
    if not business_connection_id:
        last_activity[chat_id] = time.time()

    if is_group(message):
        memory.touch_chat(chat_id, message.chat.title, message.chat.type)

    count = memory.add(chat_id, display_name(message), text)
    if count is not None and count % 12 == 0 and chat_id not in memory_refreshing:
        memory_refreshing.add(chat_id)
        asyncio.create_task(refresh_long_term_memory(chat_id))

    direct = bool(business_connection_id) or is_private(message)
    if not direct:
        entities = message.entities or message.caption_entities or []
        source = message.text or message.caption or ""
        for entity in entities:
            if entity.type == "mention":
                mention = source[entity.offset:entity.offset + entity.length]
                if mention.lower() == f"@{ME['username'].lower()}":
                    direct = True
                    break
        if (
            message.reply_to_message
            and message.reply_to_message.from_user
            and message.reply_to_message.from_user.id == ME["id"]
        ):
            direct = True
        if not direct and time.time() - last_reply[chat_id] < SILENCE_SECONDS:
            return

    async with chat_locks[chat_id]:
        try:
            ctx = memory.context(chat_id)
            if not await should_speak(ctx, text, direct=direct, bot_name=BOT_NAME):
                return
            image = await download_photo(message) if message.photo else None
            raw = await chat(ctx, text, direct=direct, bot_name=BOT_NAME, image=image)
            await deliver(chat_id, raw, business_connection_id=business_connection_id)
        except Exception:
            log.exception("Reply failed")


@dp.message(F.photo)
async def on_photo(message: Message):
    if message.from_user and message.from_user.is_bot:
        return
    caption = (message.caption or "").strip()
    await handle_incoming(message, f"[фото] {caption}".strip())


@dp.message(F.sticker)
async def on_sticker(message: Message):
    if message.from_user and message.from_user.is_bot:
        return
    s = message.sticker
    memory.add_sticker(s.file_id, s.emoji or "", s.set_name or "")
    await handle_incoming(message, f"[стикер {s.emoji or ''}]".strip())


@dp.message(F.text)
async def on_text(message: Message):
    if message.from_user and message.from_user.is_bot:
        return
    text = (message.text or "").strip()
    if not text:
        return

    chat_id = message.chat.id
    # Подтверждение /clean
    if chat_id in pending_clean:
        if time.time() - pending_clean[chat_id] > CLEAN_TIMEOUT:
            pending_clean.pop(chat_id, None)
        elif text == CLEAN_CONFIRM:
            pending_clean.pop(chat_id, None)
            memory.clear_chat(chat_id)
            last_activity.pop(chat_id, None)
            last_reply.pop(chat_id, None)
            memory_refreshing.discard(chat_id)
            await message.answer(
                "✅ Память этого чата очищена. Я больше не помню, о чём здесь говорили."
            )
            return
        # если текст не совпал — просто ждём дальше, не сбрасываем pending

    await handle_incoming(message, text)


# ---------------------------------------------------------------- служебное

async def health_handler(reader, writer):
    try:
        await reader.read(1024)
        body = b"Flow AI KPK is running"
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        writer.write(response)
        await writer.drain()
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = await asyncio.start_server(health_handler, host="0.0.0.0", port=port)
    log.info("HTTP health server listening on 0.0.0.0:%s", port)
    return server


async def refresh_long_term_memory(chat_id):
    try:
        facts = await extract_facts(memory.context(chat_id))
        for item in facts:
            if not isinstance(item, dict):
                continue
            username = str(item.get("username", "пользователь"))[:100]
            fact = str(item.get("fact", "")).strip()
            if fact:
                memory.add_fact(chat_id, username, fact)
    except Exception:
        log.exception("Long-term memory refresh failed")
    finally:
        memory_refreshing.discard(chat_id)


async def preload_stickers():
    for name in STICKER_SETS:
        try:
            pack = await bot.get_sticker_set(name)
            for st in pack.stickers:
                memory.add_sticker(st.file_id, st.emoji or "", name)
            log.info("Loaded sticker set %s (%d)", name, len(pack.stickers))
        except Exception:
            log.exception("Sticker set %s failed", name)


async def autonomous_loop():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for chat_id, activity in list(last_activity.items()):
            if now - activity < AUTONOMOUS_MINUTES * 60:
                continue
            if now - last_reply[chat_id] < AUTONOMOUS_MINUTES * 60:
                continue
            if now - activity > AUTONOMOUS_MINUTES * 4 * 60:
                continue

            async with chat_locks[chat_id]:
                if random.random() > 0.70:
                    continue
                try:
                    raw = await chat(
                        memory.context(chat_id),
                        "autonomous",
                        direct=False,
                        bot_name=BOT_NAME,
                        autonomous=True,
                    )
                    await deliver(chat_id, raw)
                except Exception:
                    log.exception("Autonomous send failed")


async def main():
    log.info("Starting %s", BOT_NAME)
    me = await bot.get_me()
    ME["id"], ME["username"] = me.id, me.username or ""

    server = await start_health_server()
    asyncio.create_task(preload_stickers())
    asyncio.create_task(autonomous_loop())

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
