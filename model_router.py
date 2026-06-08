"""模型路由 + 降级链"""
import logging

from config import config
from providers import AIProvider, DeepSeekProvider, KimiProvider
from storage import models as storage

logger = logging.getLogger(__name__)

FALLBACK_MSG = "抱歉，AI 服务暂时不可用，请稍后再试。"

# System Prompt 缓存
_prompt_cache: dict[str, str] = {}


def _load_prompt(role: str) -> str:
    """加载 System Prompt 模板"""
    if role not in _prompt_cache:
        path = (
            __file__.replace("model_router.py", "")
            + f"prompts/{role}.md"
        )
        try:
            with open(path, "r", encoding="utf-8") as f:
                _prompt_cache[role] = f.read()
        except FileNotFoundError:
            _prompt_cache[role] = "你是一个有用的AI助手。"
    return _prompt_cache[role]


class ModelRouter:
    def __init__(self):
        self.deepseek: AIProvider = DeepSeekProvider(
            config.deepseek_api_key, config.deepseek_base_url
        )
        self.kimi: AIProvider = KimiProvider(
            config.kimi_api_key, config.kimi_base_url
        )

    def identify_role(self, sender_id: str) -> str:
        """根据发送者 ID 识别角色"""
        if sender_id in config.admin_users:
            return "family"
        elif sender_id in config.child_users:
            return "socratic"
        elif sender_id in config.tutor_users:
            return "teacher"
        return "family"

    async def route(
        self,
        sender_id: str,
        user_message: str,
        images: list[bytes] | None = None,
        needs_visual: bool = False,
    ) -> str:
        """
        路由消息到对应模型。
        纯文本/OCR → DeepSeek，视觉理解 → Kimi，失败走降级链。
        """
        role = self.identify_role(sender_id)
        system_prompt = _load_prompt(role)

        # 加载历史上下文
        history = await storage.get_context(sender_id)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # 保存用户消息
        await storage.save_message(sender_id, "user", user_message)

        reply = ""
        if needs_visual and images:
            reply = await self._try_kimi(messages, images)
        else:
            reply = await self._try_deepseek_with_fallback(messages, images)

        if not reply:
            reply = FALLBACK_MSG

        # 解析积分标记并存入数据库
        if reply:
            score_list, clean_reply = storage.parse_score_tags(reply)
            if score_list:
                for sc in score_list:
                    await storage.add_score(sender_id, sc["points"], role, sc["reason"])
                reply = clean_reply

        # 保存 AI 回复，清理旧记录
        if reply:
            await storage.save_message(sender_id, "assistant", reply)
        await storage.trim_history(sender_id)

        return reply

    async def _try_deepseek_with_fallback(
        self, messages: list[dict], images: list[bytes] | None
    ) -> str:
        try:
            return await self.deepseek.chat(messages, images)
        except Exception as e:
            logger.warning(f"DeepSeek 调用失败: {e}，降级到 Kimi")
            return await self._try_kimi(messages, images)

    async def _try_kimi(
        self, messages: list[dict], images: list[bytes] | None
    ) -> str:
        try:
            return await self.kimi.chat(messages, images)
        except Exception as e:
            logger.error(f"Kimi 调用也失败: {e}")
            return ""
