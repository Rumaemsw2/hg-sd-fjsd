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

from ai import chat, extract_facts, parse_reply, should_speak, summarize, secretary_chat
from media import generate_image, search_image
from memory import Memory

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("flow-ai-kpc")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_NAME = os.getenv("BOT_NAME", "Saver AI")
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
        f"Привет! Я {BOT_NAME} — ИИ-помощник и личный секретарь для Telegram Business.\n\n"
        "Подключи меня в настройках Telegram для бизнеса и разреши доступ к нужным чатам.\n"
        "В выбранном бизнес-чате отправь /ai, чтобы включить ответы ИИ, или /off, чтобы выключить.\n\n"
        "В личке: /obz <запрос> — свободное общение, заметки по сообщениям, удаление.\n"
        "Для уведомлений об изменённых и удалённых сообщениях нажми Start в этом диалоге."
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        f"{BOT_NAME} — ИИ-помощник Telegram Business.\n\n"
        "В бизнес-чате:\n"
        "/ai — включить ИИ в этом чате\n"
        "/off — выключить ИИ в этом чате\n"
        "/status — проверить режим\n"
        "/sum — кратко подвести итог переписки\n"
        "/ask вопрос — спросить ИИ с учётом контекста чата\n"
        "/help — эта справка\n\n"
        "В личке с ботом (личный секретарь):\n"
        "/obz <запрос> — свободное общение с ИИ и команды\n"
        "Примеры:\n"
        "  /obz сделай заметку последних 10 сообщений\n"
        "  /obz отправь в лс последние 5 из чата Иван\n"
        "  /obz удали последние 3 сообщения\n"
        "  /obz что было в переписке?\n\n"
        "Изменения и удаления сообщений приходят уведомлениями в личку. "
        "Для этого открой личный диалог с ботом и нажми Start."
    )


def _is_business_owner(uid: int) -> bool:
    return uid in business_owners.values() or memory.is_admin(uid)


async def _build_secretary_context(owner_id: int) -> str:
    """Собирает краткий контекст известных бизнес-чатов для секретаря."""
    # Найти connection_id владельца
    conn_ids = [cid for cid, oid in business_owners.items() if oid == owner_id]
    if not conn_ids and memory.is_admin(owner_id):
        # админ видит всё
        chats = memory.list_business_chats(limit=15)
    else:
        chats = []
        for cid in conn_ids:
            chats.extend(memory.list_business_chats(connection_id=cid, limit=10))
    if not chats:
        return "(нет закешированных бизнес-чатов)"
    parts = []
    seen = set()
    for conn, chat_id, title in chats:
        key = (conn, chat_id)
        if key in seen:
            continue
        seen.add(key)
        recent = memory.get_recent_business_messages(conn, chat_id, limit=8)
        if not recent:
            continue
        lines = [f"Чат «{title}» (id={chat_id}, conn={conn[:8]}…):"]
        for mid, sender, text in reversed(recent):  # oldest first for readability
            who = "ты" if sender == "__owner__" else sender
            lines.append(f"  [{mid}] {who}: {text[:200]}")
        parts.append("\n".join(lines))
        if len(parts) >= 6:
            break
    return "\n\n".join(parts) if parts else "(кеш пуст)"


async def _resolve_chat_for_action(token: str | None, owner_id: int):
    """Вернуть (connection_id, chat_id, title) по id или части названия, либо самый свежий."""
    conn_ids = [cid for cid, oid in business_owners.items() if oid == owner_id]
    if memory.is_admin(owner_id) and not conn_ids:
        all_chats = memory.list_business_chats(limit=40)
    else:
        all_chats = []
        for cid in conn_ids:
            all_chats.extend(memory.list_business_chats(connection_id=cid, limit=20))
    if not all_chats:
        return None
    if not token:
        return all_chats[0]  # самый свежий
    token_l = token.lower().strip()
    # точное совпадение chat_id
    for conn, cid, title in all_chats:
        if str(cid) == token_l:
            return (conn, cid, title)
    # по названию
    for conn, cid, title in all_chats:
        if token_l in (title or "").lower():
            return (conn, cid, title)
    return all_chats[0]


async def _execute_secretary_actions(raw: str, owner_id: int, reply_to: Message):
    """Парсит теги [NOTE_LAST:...] и [DELETE_LAST:...] и выполняет их. Возвращает очищенный текст ответа."""
    import re
    note_re = re.compile(r"\[NOTE_LAST:(\d+)(?::([^\]]+))?\]", re.I)
    del_re = re.compile(r"\[DELETE_LAST:(\d+)(?::([^\]]+))?\]", re.I)

    async def do_note(n: int, token: str | None):
        resolved = await _resolve_chat_for_action(token, owner_id)
        if not resolved:
            await reply_to.answer("Нет доступных бизнес-чатов с кешем сообщений.")
            return
        conn, chat_id, title = resolved
        rows = memory.get_recent_business_messages(conn, chat_id, limit=min(n, 50))
        if not rows:
            await reply_to.answer(f"В чате «{title}» пока нет закешированных сообщений.")
            return
        lines = [f"📝 Заметка: последние {len(rows)} сообщ. из «{title}»\n"]
        for mid, sender, text in reversed(rows):
            who = "ты" if sender == "__owner__" else sender
            lines.append(f"• {who}: {text[:400]}")
        text_out = "\n".join(lines)
        # режем на части если длинно
        for i in range(0, len(text_out), 3800):
            await reply_to.answer(text_out[i : i + 3800])

    async def do_delete(n: int, token: str | None):
        resolved = await _resolve_chat_for_action(token, owner_id)
        if not resolved:
            await reply_to.answer("Нет доступных бизнес-чатов для удаления.")
            return
        conn, chat_id, title = resolved
        rows = memory.get_recent_business_messages(conn, chat_id, limit=min(n, 100))
        if not rows:
            await reply_to.answer(f"В чате «{title}» нет сообщений в кеше для удаления.")
            return
        msg_ids = [r[0] for r in rows]
        try:
            await bot.delete_business_messages(
                business_connection_id=conn,
                message_ids=msg_ids,
            )
            for mid in msg_ids:
                memory.delete_business_message(conn, chat_id, mid)
            await reply_to.answer(
                f"✅ Удалено {len(msg_ids)} сообщ. в «{title}» (id: {', '.join(map(str, msg_ids[:8]))}"
                + ("…" if len(msg_ids) > 8 else "")
                + ").\nНужны права can_delete_all_messages у бота в настройках Business."
            )
        except Exception as e:
            log.exception("delete_business_messages failed")
            await reply_to.answer(
                f"Не удалось удалить в «{title}»: {e}\n"
                "Проверь, что у бота есть право «Delete all messages» в Telegram Business."
            )

    # выполняем все найденные теги
    for m in note_re.finditer(raw or ""):
        await do_note(int(m.group(1)), (m.group(2) or "").strip() or None)
    for m in del_re.finditer(raw or ""):
        await do_delete(int(m.group(1)), (m.group(2) or "").strip() or None)

    # убираем теги из ответа пользователю
    cleaned = note_re.sub("", raw or "")
    cleaned = del_re.sub("", cleaned).strip()
    return cleaned


@dp.message(Command("obz"), F.chat.type == ChatType.PRIVATE)
async def obz_cmd(message: Message, command: CommandObject):
    uid = message.from_user.id if message.from_user else 0
    if not _is_business_owner(uid):
        await message.answer(
            "Команда /obz доступна владельцу подключённого бизнес-аккаунта "
            "(или админу после /keysi). Сначала подключи бота в Telegram для бизнеса "
            "и нажми Start в этом диалоге."
        )
        return
    args = (command.args or "").strip()
    if not args:
        await message.answer(
            "Личный секретарь. Напиши после /obz что нужно, например:\n\n"
            "• /obz сделай заметку последних 10 сообщений\n"
            "• /obz отправь в лс последние 5 из чата «Имя»\n"
            "• /obz удали последние 3 сообщения\n"
            "• /obz что писали в переписке?\n"
            "• /obz просто поговори со мной\n\n"
            "Я отвечу здесь в личке и выполню действия."
        )
        return

    await bot.send_chat_action(message.chat.id, "typing")
    try:
        ctx = await _build_secretary_context(uid)
        raw = await secretary_chat(args, ctx, bot_name=BOT_NAME)
        cleaned = await _execute_secretary_actions(raw, uid, message)
        if cleaned:
            # режем длинные ответы
            for i in range(0, len(cleaned), 3900):
                await message.answer(cleaned[i : i + 3900])
        elif not any(tag in (raw or "") for tag in ("NOTE_LAST", "DELETE_LAST")):
            await message.answer("Готово.")
    except Exception:
        log.exception("/obz failed")
        await message.answer("Не получилось обработать запрос. Попробуй ещё раз чуть позже.")


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

async def notify_business_owner(owner_id: int | None, text: str):
    """Best-effort DM notification. Owner should press Start in the bot's private chat."""
    if not owner_id:
        return
    try:
        await bot.send_message(owner_id, text[:3900])
    except Exception as exc:
        log.info("Could not DM business owner %s (they may need to press Start): %s", owner_id, exc)


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


async def process_business_command(message: Message, connection_id: str, owner_id: int, text: str) -> bool:
    command_line = text.strip().split(maxsplit=1)
    command = command_line[0].split("@")[0].lower() if command_line else ""
    args = command_line[1].strip() if len(command_line) > 1 else ""
    chat_id = message.chat.id

    if command in {"/ai", "/off"}:
        enabled = command == "/ai"
        memory.set_business_mode(connection_id, chat_id, enabled)
        response = (
            "🤖 ИИ включён в этом чате. Команды: /off, /status, /sum, /ask вопрос."
            if enabled else "⏹ ИИ выключен в этом чате. Чтобы снова включить, отправь /ai."
        )
        await bot.send_message(chat_id=chat_id, text=response, business_connection_id=connection_id)
        return True

    if command == "/status":
        enabled = memory.business_mode_enabled(connection_id, chat_id)
        await bot.send_message(
            chat_id=chat_id,
            text=("🤖 ИИ сейчас включён в этом чате." if enabled else "⏹ ИИ сейчас выключен в этом чате."),
            business_connection_id=connection_id,
        )
        return True

    if command in {"/help", "/start"}:
        await bot.send_message(
            chat_id=chat_id,
            text="Команды Business AI:\n/ai — включить\n/off — выключить\n/status — статус\n/sum — итог переписки\n/ask вопрос — задать вопрос по контексту",
            business_connection_id=connection_id,
        )
        return True

    if command in {"/sum", "/summarize"}:
        if not memory.business_mode_enabled(connection_id, chat_id):
            await bot.send_message(chat_id=chat_id, text="Сначала включи режим командой /ai.", business_connection_id=connection_id)
            return True
        try:
            result = await summarize(memory.context(chat_id), BOT_NAME)
            await bot.send_message(chat_id=chat_id, text=result or "Пока нечего суммировать.", business_connection_id=connection_id)
        except Exception:
            log.exception("Business summary failed")
            await bot.send_message(chat_id=chat_id, text="Не получилось сделать сводку. Попробуй ещё раз чуть позже.", business_connection_id=connection_id)
        return True

    if command == "/ask":
        if not args:
            await bot.send_message(chat_id=chat_id, text="Напиши вопрос после команды, например: /ask о чём мы договорились?", business_connection_id=connection_id)
            return True
        if not memory.business_mode_enabled(connection_id, chat_id):
            await bot.send_message(chat_id=chat_id, text="Сначала включи режим командой /ai.", business_connection_id=connection_id)
            return True
        try:
            raw = await chat(memory.context(chat_id), args, direct=True, bot_name=BOT_NAME)
            await deliver(chat_id, raw, business_connection_id=connection_id)
        except Exception:
            log.exception("Business /ask failed")
            await bot.send_message(chat_id=chat_id, text="Не получилось ответить. Попробуй ещё раз.", business_connection_id=connection_id)
        return True

    return False


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
        elif message.voice:
            text = "[голосовое сообщение]"
        elif message.video:
            text = "[видео]"
        else:
            text = "[вложение]"

    sender_id = message.from_user.id if message.from_user else None
    sender_label = "__owner__" if sender_id == owner_id else display_name(message)
    memory.cache_business_message(connection_id, message.chat.id, message.message_id, sender_label, text)

    # Only the connected account owner may issue management commands.
    if sender_id == owner_id:
        if text.startswith("/"):
            try:
                handled = await process_business_command(message, connection_id, owner_id, text)
                if handled:
                    return
            except Exception:
                log.exception("Business command failed")
                return
        # Never react to the business account's own ordinary outgoing messages.
        return

    if message.from_user and message.from_user.is_bot:
        return
    if not memory.business_mode_enabled(connection_id, message.chat.id):
        return

    if message.sticker:
        sticker = message.sticker
        memory.add_sticker(sticker.file_id, sticker.emoji or "", sticker.set_name or "")
    await handle_incoming(message, text, business_connection_id=connection_id)


@dp.edited_business_message()
async def on_edited_business_message(message: Message):
    connection_id = message.business_connection_id
    if not connection_id:
        return
    owner_id = await get_business_owner(connection_id)
    if owner_id is None or not business_connections_enabled.get(connection_id, False):
        return

    text = (message.text or message.caption or "").strip() or "[изменённое медиа/вложение]"
    old = memory.get_business_message(connection_id, message.chat.id, message.message_id)
    old_sender, old_text = old if old else (display_name(message), "(предыдущая версия не сохранена)")
    sender_id = message.from_user.id if message.from_user else None
    sender_label = "__owner__" if sender_id == owner_id else display_name(message)
    memory.cache_business_message(connection_id, message.chat.id, message.message_id, sender_label, text)

    if sender_id != owner_id and old_text != text:
        chat = message.chat
        title = (
            getattr(chat, "full_name", None)
            or getattr(chat, "title", None)
            or getattr(chat, "username", None)
            or str(chat.id)
        )
        await notify_business_owner(
            owner_id,
            f"✏️ Сообщение изменено в чате «{title}».\nКто: {display_name(message)}\nБыло: {old_text[:900]}\nСтало: {text[:1200]}",
        )
        if memory.business_mode_enabled(connection_id, message.chat.id) and not (message.from_user and message.from_user.is_bot):
            await handle_incoming(message, f"[сообщение изменено] {text}", business_connection_id=connection_id)


@dp.deleted_business_messages()
async def on_deleted_business_messages(event):
    connection_id = event.business_connection_id
    if not connection_id:
        return
    owner_id = await get_business_owner(connection_id)
    if owner_id is None or not business_connections_enabled.get(connection_id, False):
        return

    chat_id = event.chat.id
    removed = []
    missing = []
    for message_id in event.message_ids:
        cached = memory.get_business_message(connection_id, chat_id, message_id)
        if cached:
            sender, body = cached
            label = "ты" if sender == "__owner__" else sender
            removed.append(f"• {label}: {body[:500]}")
            memory.delete_business_message(connection_id, chat_id, message_id)
        else:
            missing.append(str(message_id))
    # Better title for private chats
    chat = event.chat
    title = (
        getattr(chat, "full_name", None)
        or getattr(chat, "title", None)
        or getattr(chat, "username", None)
        or f"чат {chat_id}"
    )
    parts = [f"🗑 Удалено {len(event.message_ids)} сообщение(й) в «{title}»."]
    if removed:
        parts.append("\n" + "\n".join(removed[:12]))
        if len(removed) > 12:
            parts.append(f"…и ещё {len(removed) - 12}")
    if missing:
        parts.append(
            f"\n(без текста, id: {', '.join(missing[:15])}"
            + ("…" if len(missing) > 15 else "")
            + " — не было в кеше)"
        )
    if not removed and not missing:
        parts.append("\nТекст недоступен.")
    await notify_business_owner(owner_id, "\n".join(parts))


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
            if business_connection_id:
                try:
                    await bot.send_chat_action(
                        chat_id=chat_id,
                        action="typing",
                        business_connection_id=business_connection_id,
                    )
                except Exception:
                    pass
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
