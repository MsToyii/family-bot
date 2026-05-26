from abc import ABC, abstractmethod


class AIProvider(ABC):
    """AI 模型统一调用接口"""

    @abstractmethod
    async def chat(
        self, messages: list[dict], images: list[bytes] | None = None
    ) -> str:
        """发送消息到模型，返回回复文本"""
        ...
