"""发件箱凭据加密（Fernet / AES-128-CBC + HMAC）。生产必须配置 TL_CREDENTIALS_KEY 并妥善保管（可轮换：支持多把 Key，逗号分隔，第一把用于加密）。"""  # noqa: E501
import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.core.config import get_settings
from app.core.errors import AppError

log = logging.getLogger("crypto")


def _fernet() -> MultiFernet:
    s = get_settings()
    keys = [k.strip() for k in (s.credentials_key or "").split(",") if k.strip()]
    if not keys:
        log.warning("未配置 TL_CREDENTIALS_KEY，使用由 jwt_secret 派生的开发密钥")
        keys = [base64.urlsafe_b64encode(hashlib.sha256(("cred:" + s.jwt_secret).encode()).digest()).decode()]
    return MultiFernet([Fernet(k) for k in keys])


def encrypt(plain: str | None) -> str | None:
    return _fernet().encrypt(plain.encode()).decode() if plain else None


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise AppError("DELIVERY_AUTH_FAILED", details={"reason": "凭据无法解密（密钥已更换？）"}) from None
