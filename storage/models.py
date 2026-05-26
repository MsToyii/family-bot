from __future__ import annotations

from .database import get_db, _lock

MAX_HISTORY = 20
CONTEXT_WINDOW = 5


async def save_message(user_id: str, role: str, content: str):
    db = await get_db()
    async with _lock:
        await db.execute(
            "INSERT INTO conversations (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        await db.commit()


async def get_context(user_id: str, rounds: int = CONTEXT_WINDOW) -> list[dict]:
    db = await get_db()
    limit = rounds * 2
    rows = await db.execute_fetchall(
        """SELECT role, content FROM conversations
           WHERE user_id = ?
           ORDER BY created_at DESC LIMIT ?""",
        (user_id, limit),
    )
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


async def trim_history(user_id: str, keep: int = MAX_HISTORY * 2):
    db = await get_db()
    async with _lock:
        await db.execute(
            """DELETE FROM conversations WHERE id IN (
                SELECT id FROM conversations
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT -1 OFFSET ?
            )""",
            (user_id, keep),
        )
        await db.commit()


async def save_progress(
    child_id: str, subject: str, topic: str, status: str, notes: str = ""
):
    db = await get_db()
    async with _lock:
        await db.execute(
            """INSERT INTO learning_progress (child_id, subject, topic, status, notes)
               VALUES (?, ?, ?, ?, ?)""",
            (child_id, subject, topic, status, notes),
        )
        await db.commit()


async def get_daily_stats() -> dict:
    """获取日报统计数据"""
    db = await get_db()

    # 今日消息总数
    today_total = await db.execute_fetchall(
        """SELECT COUNT(*) as cnt FROM conversations
           WHERE date(created_at) = date('now', 'localtime')"""
    )
    total = today_total[0][0] if today_total else 0

    # 今日活跃用户
    today_users = await db.execute_fetchall(
        """SELECT user_id, role, COUNT(*) as cnt FROM conversations
           WHERE date(created_at) = date('now', 'localtime')
           GROUP BY user_id ORDER BY cnt DESC"""
    )
    users = [{"user_id": r[0], "role": r[1], "count": r[2]} for r in today_users]

    # 今日错误（assistant 回复以"抱歉"开头）
    errors = await db.execute_fetchall(
        """SELECT COUNT(*) as cnt FROM conversations
           WHERE date(created_at) = date('now', 'localtime')
           AND role = 'assistant' AND content LIKE '抱歉%'"""
    )
    error_count = errors[0][0] if errors else 0

    # 本周每日趋势
    week_trend = await db.execute_fetchall(
        """SELECT date(created_at) as d, COUNT(*) as cnt FROM conversations
           WHERE created_at >= date('now', '-6 days', 'localtime')
           GROUP BY d ORDER BY d"""
    )
    trend = [{"date": r[0], "count": r[1]} for r in week_trend]

    # 数据库最早日期（用于判断是否有历史数据）
    first = await db.execute_fetchall(
        "SELECT date(min(created_at)) FROM conversations"
    )
    first_date = first[0][0] if first and first[0][0] else "无"

    return {
        "total": total,
        "users": users,
        "error_count": error_count,
        "trend": trend,
        "first_date": first_date,
    }


async def get_progress(child_id: str, subject: str | None = None) -> list[dict]:
    db = await get_db()
    if subject:
        rows = await db.execute_fetchall(
            """SELECT subject, topic, status, notes, updated_at
               FROM learning_progress WHERE child_id = ? AND subject = ?
               ORDER BY updated_at DESC""",
            (child_id, subject),
        )
    else:
        rows = await db.execute_fetchall(
            """SELECT subject, topic, status, notes, updated_at
               FROM learning_progress WHERE child_id = ?
               ORDER BY updated_at DESC""",
            (child_id,),
        )
    return [dict(r) for r in rows]
