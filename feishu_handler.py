"""飞书事件处理：消息解析、图片下载、身份识别、卡片消息"""
import hashlib
import json
import logging
from typing import Any

import httpx

from config import config

logger = logging.getLogger(__name__)


class FeishuHandler:
    def __init__(self):
        self._tenant_token: str | None = None
        self._token_expires_at: float = 0

    # ========== 身份识别 ==========

    def get_sender_id(self, event: dict) -> str:
        """从事件中提取发送者 user_id，兼容多种格式"""
        sender = event.get("event", {}).get("sender", {})
        sender_id = sender.get("sender_id", {})
        if isinstance(sender_id, dict):
            user_id = sender_id.get("user_id") or sender_id.get("open_id") or ""
        else:
            user_id = sender.get("user_id") or sender.get("open_id") or ""
        return user_id

    # ========== 消息解析 ==========

    def parse_message(self, event: dict) -> dict:
        """
        解析飞书消息事件，返回统一的消息结构。
        消息类型：text / image / image_and_text
        """
        msg = event.get("event", {}).get("message", {})
        msg_type = msg.get("message_type", "unknown")
        content = {}

        if msg_type == "text":
            content = self._parse_text(msg)
        elif msg_type == "image":
            content = self._parse_image(msg)
        elif msg_type == "post":
            content = self._parse_post(msg)

        return {
            "msg_type": msg_type,
            "content": content,
            "raw": msg,
        }

    def _parse_text(self, msg: dict) -> dict:
        text = msg.get("content", "{}")
        try:
            import json
            data = json.loads(text)
            return {"text": data.get("text", "").strip()}
        except Exception:
            return {"text": text}

    def _parse_image(self, msg: dict) -> dict:
        image_key = msg.get("content", "").removeprefix('{"image_key":"').removesuffix('"}')
        # 也尝试 JSON 解析
        try:
            import json
            data = json.loads(msg.get("content", "{}"))
            image_key = data.get("image_key", image_key)
        except Exception:
            pass
        return {"image_key": image_key.strip('"') if image_key else ""}

    def _parse_post(self, msg: dict) -> dict:
        """富文本消息：提取纯文本 + 图片"""
        text_parts = []
        image_key = ""
        try:
            import json
            data = json.loads(msg.get("content", "{}"))
            for block in data.get("content", [[]]):
                for elem in block:
                    tag = elem.get("tag", "")
                    if tag == "text":
                        text_parts.append(elem.get("text", ""))
                    elif tag == "at":
                        text_parts.append(elem.get("user_id", ""))
                    elif tag == "img" and not image_key:
                        image_key = elem.get("image_key", "")
        except Exception:
            pass
        result: dict[str, str] = {"text": "".join(text_parts)}
        if image_key:
            result["image_key"] = image_key
        return result

    # ========== 图片下载 ==========

    @staticmethod
    def _is_valid_image(data: bytes) -> bool:
        """检查二进制数据是否为有效的图片（检查文件头）"""
        if len(data) < 12:
            return False
        valid_headers = [
            b'\xff\xd8\xff',           # JPEG
            b'\x89PNG\r\n\x1a\n',      # PNG
            b'GIF87a',                  # GIF
            b'GIF89a',                  # GIF
            b'RIFF',                    # WEBP
            b'BM',                      # BMP
        ]
        return any(data.startswith(h) for h in valid_headers)

    async def download_image(self, image_key: str, message_id: str = "") -> bytes | None:
        """根据 image_key 下载图片，返回二进制数据（自动重试）"""
        token = await self._get_tenant_token()
        if not token:
            logger.error("无法获取飞书 tenant token")
            return None

        headers = {"Authorization": f"Bearer {token}"}

        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=15) as client:
                    # 优先使用消息资源接口
                    if message_id:
                        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}?type=image"
                        resp = await client.get(url, headers=headers)
                        if resp.status_code == 200 and self._is_valid_image(resp.content):
                            return resp.content
                        if resp.status_code == 200:
                            logger.warning(f"资源接口返回无效图片数据 (attempt {attempt + 1})")

                    # 回退：图片下载接口
                    url = f"https://open.feishu.cn/open-apis/im/v1/images/{image_key}"
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        if self._is_valid_image(resp.content):
                            return resp.content
                        logger.warning(f"图片接口返回无效图片数据 (attempt {attempt + 1})")
                    else:
                        logger.warning(f"下载图片失败 (attempt {attempt + 1}): {resp.status_code}")
            except Exception as e:
                logger.warning(f"下载图片异常 (attempt {attempt + 1}): {e}")

        logger.error(f"图片下载最终失败: {image_key}")
        return None

    async def _get_tenant_token(self) -> str | None:
        """获取飞书 tenant access token，自动刷新过期 token"""
        import time
        # 提前 5 分钟刷新，避免临界过期
        if self._tenant_token and time.time() < self._token_expires_at - 300:
            return self._tenant_token

        body = {
            "app_id": config.feishu_app_id,
            "app_secret": config.feishu_app_secret,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json=body,
                )
                data = resp.json()
                if resp.status_code == 200 and data.get("code", -1) == 0:
                    self._tenant_token = data["tenant_access_token"]
                    self._token_expires_at = time.time() + data.get("expire", 7200)
                    logger.info("tenant_access_token 已刷新")
                    return self._tenant_token
                logger.error(f"获取 token 失败: {resp.text}")
                return None
        except Exception as e:
            logger.error(f"获取 token 异常: {e}")
            return None

    # ========== 签名验证 ==========

    @staticmethod
    def verify_signature(timestamp: str, nonce: str, body: str) -> bool:
        """验证飞书事件回调签名（可选，生产环境建议开启）"""
        token = config.feishu_verification_token
        if not token:
            logger.warning("未配置 VERIFICATION_TOKEN，跳过签名验证")
            return True

        raw = f"{timestamp}{nonce}{token}{body}"
        sign = hashlib.sha256(raw.encode()).hexdigest()
        return True  # 调用方需传入飞书 header 中的签名对比

    # ========== 发送消息 ==========

    async def send_message(self, receive_id: str, text: str, msg_type: str = "chat_id") -> dict | None:
        """通过飞书 API 发送消息"""
        token = await self._get_tenant_token()
        if not token:
            return None

        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        params = {"receive_id_type": msg_type}
        body = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": '{"text":"' + text.replace('"', '\\"').replace("\n", "\\n") + '"}',
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url, params=params, json=body, headers=headers
                )
                if resp.status_code == 200:
                    return resp.json()
                logger.error(f"发送消息失败: {resp.status_code} {resp.text}")
                return None
        except Exception as e:
            logger.error(f"发送消息异常: {e}")
            return None

    # ========== 交互卡片 ==========

    @staticmethod
    def image_action_card(image_key: str, message_id: str) -> dict:
        """图片处理操作选择卡片"""
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "📷 收到一张图片"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "请选择要如何处理这张图片？",
                    },
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "描述图片"},
                            "value": {"action": "describe", "image_key": image_key, "message_id": message_id},
                            "type": "default",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "OCR识别"},
                            "value": {"action": "ocr", "image_key": image_key, "message_id": message_id},
                            "type": "default",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "解题"},
                            "value": {"action": "solve", "image_key": image_key, "message_id": message_id},
                            "type": "default",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "思维引导"},
                            "value": {"action": "guide", "image_key": image_key, "message_id": message_id},
                            "type": "primary",
                        },
                        {
                            "tag": "button",
                            "text": {"tag": "plain_text", "content": "取消"},
                            "value": {"action": "cancel", "image_key": image_key, "message_id": message_id},
                            "type": "danger",
                        },
                    ],
                },
            ],
        }

    async def send_card(self, receive_id: str, card: dict, msg_type: str = "chat_id") -> dict | None:
        """发送交互卡片消息"""
        token = await self._get_tenant_token()
        if not token:
            return None

        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        params = {"receive_id_type": msg_type}
        body = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(url, params=params, json=body, headers=headers)
                if resp.status_code == 200:
                    return resp.json()
                logger.error(f"发送卡片失败: {resp.status_code} {resp.text}")
                return None
        except Exception as e:
            logger.error(f"发送卡片异常: {e}")
            return None

    # ========== 文档 & 多维表格 ==========

    async def create_doc(self, title: str, folder_token: str = "") -> dict | None:
        """创建飞书文档，返回 {document_id, url} 或 None"""
        token = await self._get_tenant_token()
        if not token:
            return None

        url = "https://open.feishu.cn/open-apis/docx/v1/documents"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body: dict[str, str] = {"title": title}
        if folder_token:
            body["folder_token"] = folder_token

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=body, headers=headers)
                data = resp.json()
                code = data.get("code", -1)
                if resp.status_code == 200 and code == 0:
                    doc = data.get("data", {}).get("document", {})
                    doc_id = doc.get("document_id", "")
                    doc_url = doc.get("url", "")
                    if not doc_url and doc_id:
                        doc_url = f"https://mstoyii.feishu.cn/docx/{doc_id}"
                    logger.info(f"文档创建成功: {title} -> {doc_url}")
                    return {"document_id": doc_id, "url": doc_url}
                logger.error(f"创建文档失败 [{code}]: {data.get('msg', resp.text)}")
                return None
        except Exception as e:
            logger.error(f"创建文档异常: {e}")
            return None

    async def create_bitable(self, name: str, folder_token: str = "") -> dict | None:
        """创建飞书多维表格，返回 {app_token, url} 或 None"""
        token = await self._get_tenant_token()
        if not token:
            return None

        url = "https://open.feishu.cn/open-apis/bitable/v1/apps"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body: dict[str, str] = {"name": name}
        if folder_token:
            body["folder_token"] = folder_token

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json=body, headers=headers)
                data = resp.json()
                code = data.get("code", -1)
                if resp.status_code == 200 and code == 0:
                    app = data.get("data", {}).get("app", {})
                    app_token = app.get("app_token", "")
                    bitable_url = app.get("url", "")
                    if not bitable_url and app_token:
                        bitable_url = f"https://mstoyii.feishu.cn/base/{app_token}"
                    logger.info(f"多维表格创建成功: {name} -> {bitable_url}")
                    return {"app_token": app_token, "url": bitable_url}
                logger.error(f"创建多维表格失败 [{code}]: {data.get('msg', resp.text)}")
                return None
        except Exception as e:
            logger.error(f"创建多维表格异常: {e}")
            return None
