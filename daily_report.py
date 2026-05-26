"""每日统计报告生成与推送"""
import asyncio
import logging
from datetime import datetime

from storage import models as storage

logger = logging.getLogger(__name__)

WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]


async def generate_report() -> str:
    """查询统计数据，生成纯文本日报"""
    stats = await storage.get_daily_stats()
    today_str = datetime.now().strftime("%Y-%m-%d %A")

    lines = [
        f"📊 飞书家庭AI助理 — 日报 ({today_str})",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    # 活跃用户
    if stats["users"]:
        lines.append("")
        lines.append("👤 今日活跃用户")
        for u in stats["users"]:
            role_label = {"user": "用户", "assistant": "AI", "system": "系统"}.get(
                u["role"], u["role"]
            )
            lines.append(f"  • {u['user_id'][:20]}... [{role_label}]: {u['count']} 条")
    else:
        lines.append("")
        lines.append("👤 今日暂无用户消息")

    # 统计
    lines.append("")
    lines.append("📈 今日统计")
    lines.append(f"  • 总消息数: {stats['total']}")
    lines.append(f"  • 错误次数: {stats['error_count']}")

    # 本周趋势
    if stats["trend"]:
        lines.append("")
        lines.append("📅 近7日趋势")
        parts = []
        for t in stats["trend"]:
            try:
                dt = datetime.strptime(t["date"], "%Y-%m-%d")
                day_name = WEEKDAYS[dt.weekday()]
            except Exception:
                day_name = t["date"]
            parts.append(f"{day_name}({t['date'][5:]}) {t['count']}")
        lines.append("  " + " | ".join(parts))

    lines.append("")
    lines.append(f"📌 数据起始: {stats['first_date']}")

    return "\n".join(lines)


async def send_report_to_admins(handler, config):
    """向所有管理员推送日报"""
    report = await generate_report()
    logger.info("推送每日报告")
    for admin_id in config.admin_users:
        try:
            await handler.send_message(admin_id, report, msg_type="open_id")
            logger.info(f"报告已发送至: {admin_id}")
        except Exception as e:
            logger.error(f"发送报告给 {admin_id} 失败: {e}")
