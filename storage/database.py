import asyncio
import aiosqlite
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "family_bot.db"

_db: aiosqlite.Connection | None = None
_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(str(DB_PATH))
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
    return _db


async def init_db():
    db = await get_db()
    async with _lock:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_conv_user
                ON conversations(user_id);
            CREATE INDEX IF NOT EXISTS idx_conv_created
                ON conversations(user_id, created_at);

            CREATE TABLE IF NOT EXISTS learning_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                child_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                topic TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('not_started', 'in_progress', 'mastered', 'needs_review')),
                notes TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_lp_child
                ON learning_progress(child_id, subject);
        """)
        await db.commit()


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None
