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


@lru_cache
def get_settings() -> Settings:
    return Settings()
