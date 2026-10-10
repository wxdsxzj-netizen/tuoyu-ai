from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TL_", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+psycopg://tl:tl@localhost:5432/tradeleads"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = "change-me-in-production-please-32b+"
    jwt_alg: str = "HS256"
    access_token_minutes: int = 120
    cors_origins: str = "http://localhost:5173"
    celery_eager: bool = False

    # LLM 网关：mock | qwen | deepseek
    llm_primary: str = "mock"
    llm_fallback: str = ""
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    qwen_model: str = "qwen-plus"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-chat"
    llm_timeout_s: float = 60.0

    # 平台管理员（开通租户）。为空则禁用 HTTP 接口，只能用命令行
    platform_admin_token: str = ""

    # 官网解析
    crawl_max_pages: int = 30
    crawl_timeout_s: float = 20.0
    crawl_allow_private: bool = False  # 仅测试用：允许抓取内网地址
    crawl_user_agent: str = "TuoyuBot/0.1 (+knowledge-base; contact: admin)"

    # 导入
    upload_dir: str = "/tmp/tuoyu-uploads"
    import_max_rows: int = 100_000
    import_max_bytes: int = 20 * 1024 * 1024

    # 联系人数据源：mock | apollo | hunter | snov
    contact_provider: str = "mock"
    apollo_api_key: str = ""
    hunter_api_key: str = ""
    snov_client_id: str = ""
    snov_client_secret: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
