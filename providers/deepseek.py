from openai import AsyncOpenAI

from .base import AIProvider


class DeepSeekProvider(AIProvider):
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com"):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(
        self, messages: list[dict], images: list[bytes] | None = None
    ) -> str:
        response = await self.client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
            temperature=0.7,
        )
        return response.choices[0].message.content or ""
