import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # 飞书
    feishu_app_id: str = os.getenv("FEISHU_APP_ID", "")
    feishu_app_secret: str = os.getenv("FEISHU_APP_SECRET", "")
    feishu_verification_token: str = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
    feishu_encrypt_key: str = os.getenv("FEISHU_ENCRYPT_KEY", "")

    # DeepSeek
    deepseek_api_key: str = os.getenv("DEEPSEEK_API_KEY", "")
    deepseek_base_url: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    # Kimi
    kimi_api_key: str = os.getenv("KIMI_API_KEY", "")
    kimi_base_url: str = os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1")

    # 角色映射
    admin_users: list[str] = [u.strip() for u in os.getenv("ADMIN_USERS", "").split(",") if u.strip()]
    child_users: list[str] = [u.strip() for u in os.getenv("CHILD_USERS", "").split(",") if u.strip()]
    tutor_users: list[str] = [u.strip() for u in os.getenv("TUTOR_USERS", "").split(",") if u.strip()]

    # 服务器
    host: str = os.getenv("HOST", "0.0.0.0")
    port: int = int(os.getenv("PORT", "8080"))

    # 对话上下文轮数
    context_rounds: int = 5

    @property
    def is_configured(self) -> bool:
        return bool(
            self.feishu_app_id
            and self.feishu_app_secret
            and self.deepseek_api_key
        )


config = Config()
