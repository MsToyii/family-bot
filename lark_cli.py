"""lark-cli 封装 — 通过子进程调用飞书官方 CLI"""
import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

LARK_CLI = "/usr/bin/lark-cli"
LARK_CLI_ENV = {**os.environ, "HOME": os.environ.get("HOME", "/home/ubuntu")}


async def _run(*args: str) -> dict:
    """异步执行 lark-cli 命令，返回解析后的 JSON"""
    cmd = [LARK_CLI, *args]
    logger.info(f"lark-cli: {' '.join(args)}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=LARK_CLI_ENV,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="ignore").strip()
        logger.error(f"lark-cli 失败 [{proc.returncode}]: {err}")
        return {"ok": False, "error": err}

    try:
        return json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError:
        return {"ok": False, "error": stdout.decode("utf-8", errors="ignore")}


async def create_doc(title: str, content: str = "") -> dict:
    """创建飞书文档，返回 {ok, document_id, url}"""
    md = f"# {title}\n\n{content}" if content else f"# {title}"
    result = await _run(
        "docs", "+create",
        "--api-version", "v2",
        "--doc-format", "markdown",
        "--content", md,
    )
    if result.get("ok"):
        doc = result.get("data", {}).get("document", {})
        return {
            "ok": True,
            "document_id": doc.get("document_id", ""),
            "url": doc.get("url", ""),
        }
    return {"ok": False, "error": result.get("error", "未知错误")}


async def create_bitable(name: str) -> dict:
    """创建飞书多维表格，返回 {ok, app_token, url}"""
    result = await _run(
        "base", "+app-create",
        "--name", name,
    )
    if result.get("ok"):
        app = result.get("data", {}).get("app", {})
        return {
            "ok": True,
            "app_token": app.get("app_token", ""),
            "url": app.get("url", ""),
        }
    return {"ok": False, "error": result.get("error", "未知错误")}


async def search_docs(query: str) -> list[dict]:
    """搜索文档/多维表格"""
    result = await _run(
        "drive", "+search",
        "--query", query,
    )
    if result.get("ok"):
        return result.get("data", {}).get("items", [])
    return []


async def get_calendar_agenda(days: int = 1) -> list[dict]:
    """获取日历日程"""
    result = await _run("calendar", "+agenda", "--days", str(days))
    if result.get("ok"):
        return result.get("data", {}).get("items", [])
    return []


async def send_cli_message(chat_id: str, text: str) -> dict:
    """通过 CLI 发送消息（作为用户发消息，可发富文本等）"""
    return await _run(
        "im", "+messages-send",
        "--chat-id", chat_id,
        "--text", text,
    )
