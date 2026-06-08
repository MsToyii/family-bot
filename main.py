"""飞书家庭 AI 助理 — FastAPI 主服务"""
import logging
import json
import asyncio
import re
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
import lark_cli

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

handler = FeishuHandler()
router = ModelRouter()

# 待处理图片缓存: {sender_id: {"image_key": str, "message_id": str}}
_pending_images: dict[str, dict[str, str]] = {}

# 消息去重: {message_id: timestamp}，300 秒内不重复处理
_seen_messages: dict[str, float] = {}
_MESSAGE_DEDUP_TTL = 300

# 群聊 @mention 追踪: {"chat_id:sender_id": timestamp}，60 秒窗口
# 用于放行 @mention 之后发送的纯图片消息
_recent_mentions: dict[str, float] = {}
_RECENT_MENTION_TTL = 60

# 并发保护锁
_message_lock = asyncio.Lock()


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
    elif event_type == "card.action.trigger":
        background_tasks.add_task(_process_card_action, event_data)

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


def _extract_doc_title(text: str) -> str | None:
    """从自然语言中提取文档创建意图和标题"""
    import re
    # 精确指令优先匹配（由后续代码处理）
    if text.startswith("创建文档 ") or text.startswith("写文档 "):
        return None
    # 自然语言模式
    patterns = [
        r"生成.*?文档[：:]*[《「『](.+?)[》」』']",
        r"创建.*?文档[：:]*[《「『](.+?)[》」』']",
        r"写.*?文档[：:]*[《「『](.+?)[》」』']",
        r"输出.*?文档[：:]*[《「『](.+?)[》」』']",
        r"生成.*?文档[：:]*\s*[#\d]+\.?\s*(.+)",
        r"(?:生成|创建|写|帮我写|帮我创建|帮我生成).*?文档",
        r"文档.*?(?:生成|创建|写|输出)",
        r"一份.*?文档",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            if m.lastindex and m.group(1):
                return m.group(1).strip()
            # 有意图但没有明确标题 → 从消息中提取
            title = re.sub(r"(?:请|帮我|能不能|可以|麻烦你?)(?:生成|创建|写|输出)(?:一个|一份|一篇)?(?:关于)?", "", text)
            title = re.sub(r"文档.*$", "", title)
            title = re.sub(r".*?(?:生成|创建|写|输出)", "", title)
            title = title.strip().rstrip("，,。.")
            if title and len(title) < 30:
                return title
            return "未命名文档"
    return None


def _extract_bitable_name(text: str) -> str | None:
    """从自然语言中提取多维表格创建意图"""
    import re
    if text.startswith("创建多维表格 ") or text.startswith("创建表格 "):
        return None
    patterns = [
        r"多维表格[，,]*[：:]*[《「『](.+?)[》」』']",
        r"(?:建立|创建|生成|新建).*?多维表格",
        r"多维表格.*?(?:建立|创建|生成|新建|做)",
    ]
    for pat in patterns:
        if re.search(pat, text):
            name = re.sub(r"(?:请|帮我|能不能|可以|麻烦你?)(?:建立|创建|生成|新建)(?:一个|一份)?", "", text)
            name = re.sub(r"多维表格.*$", "", name)
            name = re.sub(r".*?(?:建立|创建|多维表格)", "", name)
            name = name.strip().rstrip("，,。.")
            if name and len(name) < 30:
                return name
            return "未命名表格"
    return None


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

        # 防护 2: 消息去重（300 秒内同一 message_id 不重复处理）
        now = time.time()
        if message_id:
            async with _message_lock:
                # 清理过期的去重记录（仅当记录数超过阈值时触发）
                if len(_seen_messages) > 100:
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

        msg_type = parsed["msg_type"]

        # 防护 3: 群聊中只响应 @提及
        if _is_group_chat(event_data):
            is_mentioned = _is_bot_mentioned(user_text)
            if is_mentioned:
                # 记录最近 @了机器人的用户（60 秒窗口）
                async with _message_lock:
                    _recent_mentions[f"{chat_id}:{sender_id}"] = time.time()
                    # 定期清理过期记录
                    if len(_recent_mentions) > 50:
                        cutoff = time.time() - _RECENT_MENTION_TTL
                        expired = [k for k, ts in _recent_mentions.items() if ts < cutoff]
                        for k in expired:
                            del _recent_mentions[k]
            elif image_key:
                # 图片/富文本图片消息 → 检查是否近期 @过机器人
                async with _message_lock:
                    last_mention = _recent_mentions.get(f"{chat_id}:{sender_id}", 0)
                if time.time() - last_mention > _RECENT_MENTION_TTL:
                    logger.info(f"群聊图片消息，发送者 {sender_id} 未在窗口期内 @机器人，跳过")
                    return
                logger.info(f"群聊图片消息，发送者 {sender_id} 近期 @过机器人，放行")
            else:
                logger.info(f"群聊消息未 @机器人，跳过: chat={chat_id}")
                return

        # 纯 @提及（不含实际内容），清空后走图片消息路径
        _clean_text = re.sub(r"@_user_1|@_all|@机器人", "", user_text).strip()
        if not _clean_text:
            user_text = ""

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

        # 自然语言意图识别：创建文档
        if not admin_cmd:
            cmd = user_text.strip()
            # 自然语言 → 提取标题创建文档
            doc_title = _extract_doc_title(cmd)
            if doc_title:
                result = await lark_cli.create_doc(doc_title)
                if result.get("ok"):
                    reply = f"✅ 文档创建成功\n📄 [{doc_title}]({result['url']})"
                else:
                    reply = f"❌ 文档创建失败: {result.get('error', '未知错误')}"
                admin_cmd = True
            # 自然语言 → 提取名称创建多维表格
            btable_name = _extract_bitable_name(cmd)
            if not admin_cmd and btable_name:
                result = await lark_cli.create_bitable(btable_name)
                if result.get("ok"):
                    reply = f"✅ 多维表格创建成功\n📊 [{btable_name}]({result['url']})"
                else:
                    reply = f"❌ 多维表格创建失败: {result.get('error', '未知错误')}"
                admin_cmd = True

        # 精确指令（所有用户可用）
        if not admin_cmd:
            cmd = user_text.strip()
            if cmd == "我的ID":
                reply = f"你的 Open ID: `{sender_id}`"
                admin_cmd = True
            elif cmd.startswith("创建文档 "):
                title = cmd[5:].strip()
                if title:
                    result = await lark_cli.create_doc(title)
                    if result.get("ok"):
                        reply = f"✅ 文档创建成功\n📄 [{title}]({result['url']})"
                    else:
                        reply = f"❌ 文档创建失败: {result.get('error', '未知错误')}"
                else:
                    reply = "用法: 创建文档 <标题>"
                admin_cmd = True
            elif cmd.startswith("创建多维表格 "):
                name = cmd[6:].strip()
                if name:
                    result = await lark_cli.create_bitable(name)
                    if result.get("ok"):
                        reply = f"✅ 多维表格创建成功\n📊 [{name}]({result['url']})"
                    else:
                        reply = f"❌ 多维表格创建失败: {result.get('error', '未知错误')}"
                else:
                    reply = "用法: 创建多维表格 <名称>"
                admin_cmd = True

        if not admin_cmd:
            # 图文混合消息（富文本 post 同时携带 text + image_key）→ 直接走视觉模型
            if image_key and user_text and msg_type == "post":
                logger.info(f"图文混合消息，直接处理: {user_text[:60]}")
                img_data = await handler.download_image(image_key, message_id)
                if img_data:
                    reply = await router.route(
                        sender_id=sender_id,
                        user_message=user_text,
                        images=[img_data],
                        needs_visual=True,
                    )
                else:
                    reply = "抱歉，图片下载失败，请重新发送。"

            # 纯图片消息：缓存图片 key，发送操作选择卡片
            elif image_key and not user_text:
                async with _message_lock:
                    _pending_images[sender_id] = {"image_key": image_key, "message_id": message_id}
                logger.info(f"缓存待处理图片: sender={sender_id}, image_key={image_key}")
                card = handler.image_action_card(image_key, message_id)
                await handler.send_card(chat_id, card)
                logger.info(f"图片操作卡片已发送: sender={sender_id}")

            # 文字消息 + 有缓存图片 → 按用户指令处理图片
            elif user_text and sender_id in _pending_images:
                async with _message_lock:
                    pending = _pending_images.pop(sender_id, None)
                if not pending:
                    logger.info(f"缓存图片已被其他任务取走: sender={sender_id}")
                    reply = await router.route(sender_id=sender_id, user_message=user_text)
                else:
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


# ========== 卡片交互 ==========

# 按钮操作 → AI 指令映射
_CARD_ACTION_PROMPTS = {
    "describe": "描述这张图片的内容，用中文详细说明。",
    "ocr": "提取图片中的文字，按原格式输出。如果有手写文字请特别注意识别准确。",
    "solve": "请解答图中的题目，给出详细的解题步骤和最终答案。",
    "guide": (
        "图片中有一道题目或问题。\n"
        "你的角色是启发式教学，目的是培养独立思考能力。\n"
        "请遵循以下原则：\n"
        "1. 不要直接给出答案\n"
        "2. 通过提问引导对方思考解题方向\n"
        "3. 给出步骤提示但不完成关键推理\n"
        "4. 鼓励对方尝试，肯定努力\n"
        "5. 如果对方已有思路，顺着他们的思路引导"
    ),
}


async def _process_card_action(event_data: dict):
    """处理卡片按钮点击回调"""
    try:
        event = event_data.get("event", {})
        action = event.get("action", {})
        value = action.get("value", {})
        action_type = value.get("action", "")
        image_key = value.get("image_key", "")
        message_id = value.get("message_id", "")
        open_id = event.get("operator", {}).get("open_id", "")
        card_chat_id = event.get("open_chat_id", "") or open_id

        if not action_type or not open_id:
            logger.warning(f"无效卡片回调: {json.dumps(event_data, ensure_ascii=False)[:300]}")
            return

        logger.info(f"卡片操作: {action_type}, user={open_id}")

        # 取消 → 清理缓存
        if action_type == "cancel":
            async with _message_lock:
                _pending_images.pop(open_id, None)
            await handler.send_message(card_chat_id, "已取消操作。")
            return

        # 其他操作 → 下载图片 + AI 处理
        prompt = _CARD_ACTION_PROMPTS.get(action_type)
        if not prompt or not image_key:
            return

        async with _message_lock:
            _pending_images.pop(open_id, None)

        img_data = await handler.download_image(image_key, message_id)
        if not img_data:
            await handler.send_message(card_chat_id, "抱歉，图片已过期或无法下载，请重新发送图片。")
            return

        reply = await router.route(
            sender_id=open_id,
            user_message=prompt,
            images=[img_data],
            needs_visual=True,
        )

        if reply:
            # 卡片回调无 open_chat_id 时，用 open_id 发私信
            if not event.get("open_chat_id"):
                await handler.send_message(open_id, reply, msg_type="open_id")
            else:
                await handler.send_message(card_chat_id, reply)

    except Exception as e:
        logger.error(f"处理卡片回调异常: {e}", exc_info=True)


if __name__ == "__main__":
    import uvicorn

    if not config.is_configured:
        logger.warning("=" * 50)
        logger.warning("警告：缺少必要的 API 配置，请复制 .env.example 为 .env 并填入真实值")
        logger.warning("=" * 50)

    uvicorn.run(app, host=config.host, port=config.port)
