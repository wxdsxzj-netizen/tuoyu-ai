from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="TL_", extra="ignore", populate_by_name=True)

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

    # P6：点数 / Agent / 询盘评分（均可通过环境变量调整）
    fx_provider: str = "mock"  # mock：内置参考汇率，不访问外网
    pilot_grant_credits: float = 0.0  # 新租户首次使用时自动发放的点数；默认 0 = 只能由平台管理员手动充值
    agent_overrun_basis: str = "high"  # 自动暂停的参照：high（预估上限，默认）| mid（预估中位数，PRD 原文）
    agent_overrun_ratio: float = 1.0  # 实际消耗 > 参照 × 比例 时自动暂停
    inq_w_authenticity: float = 0.40
    inq_w_clarity: float = 0.35
    inq_w_fit: float = 0.25
    inq_website_bonus: int = 25  # 企业邮箱域名有可访问官网且与发件公司吻合时的加分（公司不在数据中枢时生效）
    inq_website_check: bool = True
    inq_d_auth_threshold: int = 40  # 真实性低于此分一律 D
    log_json: bool = False  # 结构化日志（生产建议 true）
    metrics_token: str = ""  # /metrics 访问令牌；为空则只允许内网（反向代理不转发 /metrics）

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

    # 联系人数据源：auto（有 Hunter Key 用 hunter，否则 mock）| mock | apollo | hunter | snov
    contact_provider: str = "auto"
    apollo_api_key: str = ""
    hunter_api_key: str = Field("", validation_alias=AliasChoices("TL_HUNTER_API_KEY", "HUNTER_API_KEY"))
    hunter_base_url: str = "https://api.hunter.io/v2"
    hunter_timeout_s: float = 25.0
    hunter_max_retries: int = 3
    snov_client_id: str = ""
    snov_client_secret: str = ""

    # 邮箱验证：auto（有 MillionVerifier Key 用它，否则有 Hunter Key 用 hunter，否则 mock）| mock | millionverifier | hunter
    verifier_provider: str = "auto"
    millionverifier_api_key: str = Field("", validation_alias=AliasChoices("TL_MILLIONVERIFIER_API_KEY", "MILLIONVERIFIER_API_KEY"))
    millionverifier_base_url: str = "https://api.millionverifier.com/api/v3/"
    verify_cache_hours: int = 24
    verify_check_mx: bool = True
    reverify_days: int = 7

    # 退订链接
    public_base_url: str = "http://localhost:8000"
    unsubscribe_secret: str = ""  # 为空时使用 jwt_secret 派生

    # 发件箱 / 发送
    dns_doh_url: str = ""  # 可选：DNS-over-HTTPS（如 https://dns.google/resolve），用于 UDP DNS 受限的环境
    credentials_key: str = ""  # Fernet key（urlsafe base64 32 字节）；为空时由 jwt_secret 派生（仅开发）
    outbound_allow_real: bool = False  # False：只允许发往本地测试 SMTP（localhost / mailpit），绝不外发
    outbound_sink_hosts: str = "localhost,127.0.0.1,::1,mailpit,mailhog"
    send_interval_min_s: int = 60
    send_interval_max_s: int = 300
    company_daily_limit: int = 3
    platform_cold_hard_cap: int = 80
    health_min_sample: int = 50

    # 制裁名单
    sanctions_csl_url: str = "https://data.trade.gov/downloadable_consolidated_screening_list/v1/consolidated.csv"
    sanctions_ofac_sdn_url: str = "https://sanctionslistservice.ofac.treas.gov/api/publicationpreview/exports/sdn.csv"
    sanctions_ofac_alt_url: str = "https://sanctionslistservice.ofac.treas.gov/api/publicationpreview/exports/alt.csv"
    sanctions_threshold: float = 0.85
    sanctions_stale_hours: int = 72


DEV_JWT = "change-me-in-production-please-32b+"


def production_problems(s: "Settings") -> list[str]:
    """TL_ENV=prod 时启动前自检：缺少或使用默认值的关键密钥直接拒绝启动。"""
    p = []
    if len(s.jwt_secret) < 32 or s.jwt_secret == DEV_JWT:
        p.append("TL_JWT_SECRET 未设置或太短（≥ 32 位）")
    if not s.credentials_key:
        p.append("TL_CREDENTIALS_KEY 未设置（发件箱密码加密密钥）")
    else:
        try:
            from cryptography.fernet import Fernet
            for k in s.credentials_key.split(","):
                Fernet(k.strip().encode())
        except Exception:  # noqa: BLE001
            p.append("TL_CREDENTIALS_KEY 不是有效的 Fernet 密钥")
    if len(s.unsubscribe_secret) < 16:
        p.append("TL_UNSUBSCRIBE_SECRET 未设置或太短（≥ 16 位）")
    if not s.public_base_url.startswith("https://"):
        p.append("TL_PUBLIC_BASE_URL 必须是 https:// 地址")
    if s.crawl_allow_private:
        p.append("TL_CRAWL_ALLOW_PRIVATE 生产必须为 false")
    if s.celery_eager:
        p.append("TL_CELERY_EAGER 生产必须为 false")
    if s.llm_primary == "mock":
        p.append("警告：TL_LLM_PRIMARY=mock，AI 功能为模拟结果")
    return p


@lru_cache
def get_settings() -> Settings:
    return Settings()
