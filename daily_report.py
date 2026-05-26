"""每日统计报告生成与推送"""
import logging
from datetime import datetime, timedelta

from storage import models as storage

logger = logging.getLogger(__name__)

WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

_report_enabled = True  # 定时推送开关


def is_report_enabled() -> bool:
    return _report_enabled


def set_report_enabled(value: bool):
    global _report_enabled
    _report_enabled = value


async def generate_report(date_offset: int = 0) -> str:
    """查询统计数据，生成日报。date_offset: 0=今天, -1=昨天"""
    stats = await storage.get_daily_stats(date_offset)

    if date_offset == -1:
        report_date = (datetime.now() + timedelta(days=-1)).strftime("%Y-%m-%d")
        title = f"📊 飞书家庭AI助理 — 昨日报告 ({report_date})"
    else:
        today_str = datetime.now().strftime("%Y-%m-%d %A")
        title = f"📊 飞书家庭AI助理 — 今日统计 ({today_str})"

    lines = [
        title,
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if stats["users"]:
        lines.append("")
        lines.append("👤 活跃用户")
        for u in stats["users"]:
            role_label = {"user": "用户", "assistant": "AI", "system": "系统"}.get(
                u["role"], u["role"]
            )
            lines.append(f"  • {u['user_id'][:20]}... [{role_label}]: {u['count']} 条")
    else:
        lines.append("")
        lines.append("👤 暂无用户消息")

    lines.append("")
    lines.append("📈 统计")
    lines.append(f"  • 总消息数: {stats['total']}")
    lines.append(f"  • 错误次数: {stats['error_count']}")

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


async def send_report_to_admins(handler, config, date_offset: int = -1):
    """向所有管理员推送日报。date_offset: -1=昨天(定时), 0=今天(手动)"""
    report = await generate_report(date_offset)
    label = "昨日报告" if date_offset == -1 else "今日统计"
    logger.info(f"推送{label}")
    for admin_id in config.admin_users:
        try:
            await handler.send_message(admin_id, report, msg_type="open_id")
            logger.info(f"{label}已发送至: {admin_id}")
        except Exception as e:
            logger.error(f"发送{label}给 {admin_id} 失败: {e}")
