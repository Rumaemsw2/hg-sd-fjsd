import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).with_name("memory.db")


class Memory:
    def __init__(self, limit=120):
        self.limit = limit
        self.lock = threading.Lock()
        self.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                fact TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(chat_id, username, fact)
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                type TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS stickers (
                file_id TEXT PRIMARY KEY,
                emoji TEXT NOT NULL DEFAULT '',
                set_name TEXT NOT NULL DEFAULT ''
            )
        """)
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS business_modes (
                connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (connection_id, chat_id)
            )
        """)
        self.db.commit()

    # ---------- Telegram Business AI switches ----------
    def set_business_mode(self, connection_id, chat_id, enabled):
        with self.lock:
            self.db.execute(
                """INSERT INTO business_modes(connection_id, chat_id, enabled)
                   VALUES (?, ?, ?)
                   ON CONFLICT(connection_id, chat_id) DO UPDATE SET
                     enabled = excluded.enabled, updated_at = CURRENT_TIMESTAMP""",
                (str(connection_id), int(chat_id), 1 if enabled else 0),
            )
            self.db.commit()

    def business_mode_enabled(self, connection_id, chat_id):
        with self.lock:
            row = self.db.execute(
                "SELECT enabled FROM business_modes WHERE connection_id = ? AND chat_id = ?",
                (str(connection_id), int(chat_id)),
            ).fetchone()
        return bool(row and row[0])

    # ---------- чаты / админы / стикеры ----------
    def touch_chat(self, chat_id, title, chat_type):
        with self.lock:
            self.db.execute(
                """INSERT INTO chats(chat_id, title, type) VALUES (?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     title = excluded.title, type = excluded.type,
                     updated_at = CURRENT_TIMESTAMP""",
                (chat_id, (title or str(chat_id))[:100], chat_type),
            )
            self.db.commit()

    def list_chats(self, limit=20):
        with self.lock:
            return self.db.execute(
                """SELECT chat_id, title FROM chats
                   WHERE type IN ('group', 'supergroup')
                   ORDER BY updated_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()

    def chat_title(self, chat_id):
        with self.lock:
            row = self.db.execute(
                "SELECT title FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row[0] if row else str(chat_id)

    def add_admin(self, user_id):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (user_id,))
            self.db.commit()

    def remove_admin(self, user_id):
        with self.lock:
            self.db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            self.db.commit()

    def is_admin(self, user_id):
        with self.lock:
            return self.db.execute(
                "SELECT 1 FROM admins WHERE user_id = ?", (user_id,)
            ).fetchone() is not None

    @staticmethod
    def _emoji(e):
        return (e or "").replace("\ufe0f", "").strip()

    def add_sticker(self, file_id, emoji, set_name=""):
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO stickers(file_id, emoji, set_name) VALUES (?, ?, ?)",
                (file_id, self._emoji(emoji), set_name or ""),
            )
            self.db.commit()

    def pick_sticker(self, emoji):
        e = self._emoji(emoji)
        with self.lock:
            row = self.db.execute(
                "SELECT file_id FROM stickers WHERE emoji = ? ORDER BY RANDOM() LIMIT 1",
                (e,),
            ).fetchone()
            if not row:
                row = self.db.execute(
                    "SELECT file_id FROM stickers ORDER BY RANDOM() LIMIT 1"
                ).fetchone()
        return row[0] if row else None

    def add(self, chat_id, username, text):
        with self.lock:
            self.db.execute(
                "INSERT INTO messages(chat_id, username, text) VALUES (?, ?, ?)",
                (chat_id, username[:100], text[:4000]),
            )
            self.db.execute(
                """DELETE FROM messages
                   WHERE chat_id = ? AND id NOT IN (
                       SELECT id FROM messages
                       WHERE chat_id = ?
                       ORDER BY id DESC LIMIT ?
                   )""",
                (chat_id, chat_id, self.limit),
            )
            self.db.commit()
            return self.db.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0]

    def context(self, chat_id):
        with self.lock:
            facts = self.db.execute(
                """SELECT username, fact FROM facts
                   WHERE chat_id = ?
                   ORDER BY id DESC LIMIT 80""",
                (chat_id,),
            ).fetchall()
            rows = self.db.execute(
                """SELECT username, text FROM messages
                   WHERE chat_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (chat_id, self.limit),
            ).fetchall()

        rows.reverse()
        parts = []

        if facts:
            parts.append(
                "ДОЛГОСРОЧНАЯ ПАМЯТЬ "
                "(только ранее явно сказанные факты):"
            )
            parts.extend(f"{u}: {f}" for u, f in reversed(facts))

        parts.append("ПОСЛЕДНИЕ СООБЩЕНИЯ:")
        parts.extend(f"{u}: {t}" for u, t in rows)
        return "\n".join(parts)

    def add_fact(self, chat_id, username, fact):
        fact = (fact or "").strip()[:500]
        if not fact:
            return

        with self.lock:
            self.db.execute(
                """INSERT OR IGNORE INTO facts(chat_id, username, fact)
                   VALUES (?, ?, ?)""",
                (chat_id, username[:100], fact),
            )
            self.db.execute(
                """DELETE FROM facts
                   WHERE chat_id = ? AND id NOT IN (
                       SELECT id FROM facts
                       WHERE chat_id = ?
                       ORDER BY id DESC LIMIT 200
                   )""",
                (chat_id, chat_id),
            )
            self.db.commit()

    def clear_chat(self, chat_id):
        """Удаляет всю историю сообщений и факты только для этого чата."""
        with self.lock:
            self.db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            self.db.execute("DELETE FROM facts WHERE chat_id = ?", (chat_id,))
            self.db.commit()
