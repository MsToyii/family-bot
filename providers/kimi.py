import base64

from openai import AsyncOpenAI

from .base import AIProvider


class KimiProvider(AIProvider):
    def __init__(self, api_key: str, base_url: str = "https://api.moonshot.cn/v1"):
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(
        self, messages: list[dict], images: list[bytes] | None = None
    ) -> str:
        user_content: list[dict] = []

        if images:
            for img in images:
                b64 = base64.b64encode(img).decode("utf-8")
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })

        # 找到最后一条 user 消息的文本内容
        if messages and messages[-1]["role"] == "user":
            text = messages[-1]["content"]
            if isinstance(text, str):
                user_content.insert(0, {"type": "text", "text": text})
            # 构建给模型的消息列表
            api_messages = list(messages[:-1])
            api_messages.append({"role": "user", "content": user_content})
        else:
            api_messages = list(messages)

        model = "moonshot-v1-8k-vision-preview" if images else "moonshot-v1-8k"
        response = await self.client.chat.completions.create(
            model=model,
            messages=api_messages,
            temperature=0.7,
        )
        return response.choices[0].message.content or ""
