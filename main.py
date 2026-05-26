"""飞书家庭 AI 助理 — FastAPI 主服务"""
import logging
import json
import asyncio
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from config import config

from feishu_handler import FeishuHandler
from model_router import ModelRouter
from storage.database import init_db
from daily_report import send_report_to_admins, generate_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

handler = FeishuHandler()
router = ModelRouter()

# 待处理图片缓存: {sender_id: {"image_key": str, "message_id": str}}
_pending_images: dict[str, dict[str, str]] = {}


_report_task: asyncio.Task | None = None


async def _daily_report_loop():
    """每天 21:00 推送日报"""
    while True:
        now = datetime.now()
        target = now.replace(hour=21, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        logger.info(f"日报定时: 下次推送 {target.strftime('%Y-%m-%d %H:%M')} (等待 {wait_seconds:.0f}s)")
        await asyncio.sleep(wait_seconds)
        try:
            await send_report_to_admins(handler, config)
        except Exception as e:
            logger.error(f"日报推送异常: {e}")


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


async def _process_message(event_data: dict):
    """后台异步处理消息"""
    try:
        parsed = handler.parse_message(event_data)
        sender_id = handler.get_sender_id(event_data)
        chat_id = event_data.get("event", {}).get("message", {}).get("chat_id", "")
        logger.info(f"收到消息: sender={sender_id}, chat={chat_id}, type={parsed['msg_type']}")

        user_text = parsed["content"].get("text", "")
        image_key = parsed["content"].get("image_key", "")
        reply = ""

        # 管理员查询日报
        if user_text.strip() in ("日报", "今日统计") and sender_id in config.admin_users:
            reply = await generate_report()
            logger.info(f"管理员 {sender_id} 查询日报")

        # 图片消息：缓存图片 key，询问用户如何处理
        elif image_key and not user_text:
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
