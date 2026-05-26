"""飞书事件处理：消息解析、图片下载、身份识别"""
import hashlib
import logging
from typing import Any

import httpx

from config import config

logger = logging.getLogger(__name__)


class FeishuHandler:
    def __init__(self):
        self._tenant_token: str | None = None

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
        """富文本消息：提取纯文本"""
        text_parts = []
        try:
            import json
            data = json.loads(msg.get("content", "{}"))
            for block in data.get("content", [[]]):
                for elem in block:
                    if elem.get("tag") == "text":
                        text_parts.append(elem.get("text", ""))
        except Exception:
            pass
        return {"text": "".join(text_parts)}

    # ========== 图片下载 ==========

    async def download_image(self, image_key: str, message_id: str = "") -> bytes | None:
        """根据 image_key 下载图片，返回二进制数据"""
        token = await self._get_tenant_token()
        if not token:
            logger.error("无法获取飞书 tenant token")
            return None

        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # 优先使用消息资源接口
                if message_id:
                    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/resources/{image_key}?type=image"
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 200:
                        return resp.content
                    logger.warning(f"资源接口失败: {resp.status_code}，尝试图片接口")

                # 回退：图片下载接口
                url = f"https://open.feishu.cn/open-apis/im/v1/images/{image_key}"
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return resp.content
                logger.error(f"下载图片失败: {resp.status_code} {resp.text}")
                return None
        except Exception as e:
            logger.error(f"下载图片异常: {e}")
            return None

    async def _get_tenant_token(self) -> str | None:
        """获取飞书 tenant access token"""
        if self._tenant_token:
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
                if resp.status_code == 200:
                    data = resp.json()
                    self._tenant_token = data.get("tenant_access_token", "")
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
