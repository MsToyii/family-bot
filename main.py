"""飞书家庭 AI 助理 — FastAPI 主服务"""
import logging
import json
import asyncio
import time
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from config import config

from feishu_handler import FeishuHandler
from model_router import ModelRouter
from storage.database import init_db
from daily_report import send_report_to_admins, generate_report, is_report_enabled, set_report_enabled
from admin_routes import router as admin_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

handler = FeishuHandler()
router = ModelRouter()

# 待处理图片缓存: {sender_id: {"image_key": str, "message_id": str}}
_pending_images: dict[str, dict[str, str]] = {}

# 消息去重: {message_id: timestamp}，5 分钟内不重复处理
_seen_messages: dict[str, float] = {}
_MESSAGE_DEDUP_TTL = 300  # 秒


_report_task: asyncio.Task | None = None


async def _daily_report_loop():
    """每天定时推送前一天的日报（时间由 REPORT_TIME 配置）"""
    while True:
        now = datetime.now()
        try:
            h, m = map(int, config.report_time.split(":"))
        except (ValueError, AttributeError):
            h, m = 9, 0
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(f"日报定时: 下次推送 {target.strftime('%Y-%m-%d %H:%M')} (等待 {wait_seconds:.0f}s)")
        await asyncio.sleep(wait_seconds)
        if is_report_enabled():
            try:
                await send_report_to_admins(handler, config, date_offset=-1)
            except Exception as e:
                logger.error(f"日报推送异常: {e}")
        else:
            logger.info("日报定时推送已关闭，跳过")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化 DB"""
    global _report_task
    logger.info("初始化 SQLite 数据库...")
    await init_db()
    logger.info("服务启动完成")
    _report_task = asyncio.create_task(_daily_report_loop())
    yield
    if _report_task:
        _report_task.cancel()


app = FastAPI(title="飞书家庭AI助理", lifespan=lifespan)
app.include_router(admin_router)


@app.get("/")
async def root():
    return {"status": "ok", "service": "飞书家庭AI助理"}


@app.get("/callback")
async def callback_get(request: Request):
    """飞书可能发送 GET 请求验证回调地址"""
    body = await request.body()
    logger.info(f"GET /callback: {body.decode('utf-8', errors='ignore')[:500]}")
    return JSONResponse({"code": 0})


@app.post("/callback")
async def callback(request: Request, background_tasks: BackgroundTasks):
    """飞书事件回调入口。立即返回 200，异步处理消息。"""
    body = await request.body()
    body_str = body.decode("utf-8")

    try:
        event_data = json.loads(body_str)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, status_code=400)

    # URL 验证（飞书配置回调时发送 challenge）
    if "challenge" in event_data:
        challenge = event_data["challenge"]
        logger.info(f"URL 验证 challenge: {challenge}")
        return JSONResponse({"challenge": challenge})

    # 事件推送（兼容新旧两种格式）
    event_type = event_data.get("header", {}).get("event_type", "")
    if not event_type:
        event_type = event_data.get("event", {}).get("event_type", "")
    logger.info(f"事件类型: {event_type}")
    logger.info(f"事件数据: {json.dumps(event_data, ensure_ascii=False)[:500]}")

    if event_type == "im.message.receive_v1":
        background_tasks.add_task(_process_message, event_data)

    return JSONResponse({"code": 0})


def _is_group_chat(event_data: dict) -> bool:
    """判断是否为群聊（通过飞书 event.message.chat_type 字段）"""
    chat_type = event_data.get("event", {}).get("message", {}).get("chat_type", "")
    return chat_type == "group"


def _is_bot_mentioned(user_text: str) -> bool:
    """检查消息是否 @了机器人"""
    if not user_text:
        return False
    return "@_user_1" in user_text or "@_all" in user_text or "@机器人" in user_text


async def _process_message(event_data: dict):
    """后台异步处理消息"""
    try:
        msg_event = event_data.get("event", {})
        message = msg_event.get("message", {})
        message_id = message.get("message_id", "")
        chat_id = message.get("chat_id", "")
        sender_id = handler.get_sender_id(event_data)

        # 防护 1: 拒绝空 sender 的系统事件
        if not sender_id:
            logger.info(f"跳过系统事件（sender 为空）: chat={chat_id}, msg_id={message_id}")
            return

        # 防护 2: 消息去重（5 分钟内同一 message_id 不重复处理）
        now = time.time()
        if message_id:
            # 清理过期的去重记录
            expired = [mid for mid, ts in _seen_messages.items() if now - ts > _MESSAGE_DEDUP_TTL]
            for mid in expired:
                del _seen_messages[mid]
            if message_id in _seen_messages:
                logger.info(f"跳过重复消息: msg_id={message_id}")
                return
            _seen_messages[message_id] = now

        parsed = handler.parse_message(event_data)
        logger.info(f"收到消息: sender={sender_id}, chat={chat_id}, type={parsed['msg_type']}")

        user_text = parsed["content"].get("text", "")
        image_key = parsed["content"].get("image_key", "")
        reply = ""
        admin_cmd = False

        # 防护 3: 群聊中只响应 @提及，P2P 私聊正常回复
        if _is_group_chat(event_data) and not _is_bot_mentioned(user_text):
            logger.info(f"群聊消息未 @机器人，跳过: chat={chat_id}")
            return

        # 管理员指令
        if sender_id in config.admin_users:
            cmd = user_text.strip()
            if cmd in ("日报", "今日统计"):
                reply = await generate_report(date_offset=0)
                admin_cmd = True
                logger.info(f"管理员 {sender_id} 查询今日统计")
            elif cmd == "开启日报":
                set_report_enabled(True)
                reply = "✅ 每日定时推送已开启（每天 9:00 推送昨日报告）"
                admin_cmd = True
            elif cmd == "关闭日报":
                set_report_enabled(False)
                reply = "⏸️ 每日定时推送已关闭。随时发送「日报」手动查询。"
                admin_cmd = True

        # 文档 / 多维表格创建（所有用户可用）
        if not admin_cmd:
            cmd = user_text.strip()
            if cmd == "我的ID":
                reply = f"你的 Open ID: `{sender_id}`"
                admin_cmd = True
            elif cmd.startswith("创建文档 "):
                title = cmd[5:].strip()
                if title:
                    result = await handler.create_doc(title)
                    if result:
                        reply = f"✅ 文档创建成功\n📄 [{title}]({result['url']})"
                    else:
                        reply = "❌ 文档创建失败，请检查应用权限。\n\n飞书开放平台 → 应用 → 权限管理 → 确认「云文档」权限已开启。"
                else:
                    reply = "用法: 创建文档 <标题>"
                admin_cmd = True
            elif cmd.startswith("创建多维表格 "):
                name = cmd[6:].strip()
                if name:
                    result = await handler.create_bitable(name)
                    if result:
                        reply = f"✅ 多维表格创建成功\n📊 [{name}]({result['url']})"
                    else:
                        reply = "❌ 多维表格创建失败，请检查应用权限。\n\n飞书开放平台 → 应用 → 权限管理 → 确认「多维表格」权限已开启。"
                else:
                    reply = "用法: 创建多维表格 <名称>"
                admin_cmd = True

        if not admin_cmd:
            # 图片消息：缓存图片 key，询问用户如何处理
            if image_key and not user_text:
                message_id = event_data.get("event", {}).get("message", {}).get("message_id", "")
                _pending_images[sender_id] = {"image_key": image_key, "message_id": message_id}
                logger.info(f"缓存待处理图片: sender={sender_id}, image_key={image_key}")
                reply = "收到图片，请问需要我如何处理？\n例如：\n- 「识别这张图片」— 描述图片内容\n- 「提取文字」— OCR 文字识别\n- 「分析题目」— 识别并解答题目"

            # 文字消息 + 有缓存图片 → 按用户指令处理图片
            elif user_text and sender_id in _pending_images:
                pending = _pending_images.pop(sender_id)
                logger.info(f"处理缓存图片: {pending['image_key']}, 指令: {user_text}")
                img_data = await handler.download_image(pending["image_key"], pending["message_id"])
                if img_data:
                    reply = await router.route(
                        sender_id=sender_id,
                        user_message=user_text,
                        images=[img_data],
                        needs_visual=True,
                    )
                else:
                    reply = "抱歉，图片下载失败，请重新发送。"

            # 纯文字消息 → 正常 AI 对话
            elif user_text:
                reply = await router.route(
                    sender_id=sender_id,
                    user_message=user_text,
                )

        # 发送回复
        if reply and chat_id:
            await handler.send_message(chat_id, reply)
            logger.info(f"回复已发送: {reply[:50]}...")

    except Exception as e:
        logger.error(f"处理消息异常: {e}", exc_info=True)


if __name__ == "__main__":
    import uvicorn

    if not config.is_configured:
        logger.warning("=" * 50)
        logger.warning("警告：缺少必要的 API 配置，请复制 .env.example 为 .env 并填入真实值")
        logger.warning("=" * 50)

    uvicorn.run(app, host=config.host, port=config.port)
