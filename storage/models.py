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


async def get_daily_stats(date_offset: int = 0) -> dict:
    """获取日报统计数据。date_offset: 0=今天, -1=昨天"""
    db = await get_db()

    # 目标日期
    target_date = f"date('now', '{date_offset} days', 'localtime')"

    today_total = await db.execute_fetchall(
        f"""SELECT COUNT(*) as cnt FROM conversations
           WHERE date(created_at) = {target_date}"""
    )
    total = today_total[0][0] if today_total else 0

    today_users = await db.execute_fetchall(
        f"""SELECT user_id, role, COUNT(*) as cnt FROM conversations
           WHERE date(created_at) = {target_date}
           GROUP BY user_id ORDER BY cnt DESC"""
    )
    users = [{"user_id": r[0], "role": r[1], "count": r[2]} for r in today_users]

    errors = await db.execute_fetchall(
        f"""SELECT COUNT(*) as cnt FROM conversations
           WHERE date(created_at) = {target_date}
           AND role = 'assistant' AND content LIKE '抱歉%'"""
    )
    error_count = errors[0][0] if errors else 0

    week_trend = await db.execute_fetchall(
        """SELECT date(created_at) as d, COUNT(*) as cnt FROM conversations
           WHERE created_at >= date('now', '-6 days', 'localtime')
           GROUP BY d ORDER BY d"""
    )
    trend = [{"date": r[0], "count": r[1]} for r in week_trend]

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


async def get_conversations_paginated(
    user_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    db = await get_db()
    conditions = ["1=1"]
    params: list = []

    if user_id:
        conditions.append("user_id = ?")
        params.append(user_id)
    if date_from:
        conditions.append("date(created_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("date(created_at) <= ?")
        params.append(date_to)
    if search:
        conditions.append("content LIKE ?")
        params.append(f"%{search}%")

    where = " AND ".join(conditions)

    count_rows = await db.execute_fetchall(
        f"SELECT COUNT(*) FROM conversations WHERE {where}", tuple(params)
    )
    total = count_rows[0][0] if count_rows else 0

    offset = (page - 1) * page_size
    rows = await db.execute_fetchall(
        f"""SELECT id, user_id, role, content, created_at
           FROM conversations WHERE {where}
           ORDER BY created_at DESC LIMIT ? OFFSET ?""",
        tuple(params) + (page_size, offset),
    )
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "page_size": page_size}


async def get_all_progress(subject: str | None = None) -> list[dict]:
    db = await get_db()
    if subject:
        rows = await db.execute_fetchall(
            """SELECT child_id, subject, topic, status, notes, updated_at
               FROM learning_progress WHERE subject = ?
               ORDER BY updated_at DESC""",
            (subject,),
        )
    else:
        rows = await db.execute_fetchall(
            """SELECT child_id, subject, topic, status, notes, updated_at
               FROM learning_progress ORDER BY updated_at DESC"""
        )
    return [dict(r) for r in rows]


async def get_distinct_subjects() -> list[str]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT DISTINCT subject FROM learning_progress ORDER BY subject"
    )
    return [r[0] for r in rows]


async def get_distinct_children() -> list[str]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT DISTINCT child_id FROM learning_progress ORDER BY child_id"
    )
    return [r[0] for r in rows]
