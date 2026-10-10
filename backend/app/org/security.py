"""B1 登录安全：密码策略（M14-13）、失败锁定、TOTP 双因素（RFC 6238）、会话与设备（M14-03）。"""
import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import bypass_tenant, utcnow
from app.core.errors import AppError
from app.models import User, UserSession
from app.outreach.crypto import decrypt, encrypt

MAX_FAILS, LOCK_MIN = 5, 15


def check_password(pw: str):
    classes = sum(bool(re.search(rx, pw)) for rx in (r"[A-Za-z]", r"\d", r"[^A-Za-z0-9]"))
    if len(pw) < 10 or classes < 2:
        raise AppError("AUTH_PASSWORD_WEAK")


# ---------------- 失败锁定 ----------------
def ensure_not_locked(u: User):
    if u.locked_until and u.locked_until > utcnow():
        raise AppError("AUTH_LOCKED", details={"locked_until": u.locked_until.isoformat()})


def record_failure(u: User) -> bool:
    u.failed_logins = (u.failed_logins or 0) + 1
    if u.failed_logins >= MAX_FAILS:
        u.locked_until, u.failed_logins = utcnow() + timedelta(minutes=LOCK_MIN), 0
        return True
    return False


# ---------------- TOTP ----------------
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, t: float | None = None, step: int = 30) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = int((t or time.time()) // step)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def totp_verify(secret: str, code: str, window: int = 1) -> bool:
    code = (code or "").strip()
    now = time.time()
    return any(hmac.compare_digest(totp_code(secret, now + i * 30), code) for i in range(-window, window + 1))


def otpauth_uri(u: User, secret: str) -> str:
    return f"otpauth://totp/Tuoyu:{u.email}?secret={secret}&issuer=Tuoyu&digits=6&period=30"


def set_secret(u: User, secret: str | None):
    u.totp_secret = encrypt(secret) if secret else None


def get_secret(u: User) -> str | None:
    return decrypt(u.totp_secret) if u.totp_secret else None


def mfa_token(u: User) -> str:
    """登录第一步通过后签发的短期票据（5 分钟），只能用于提交验证码。"""
    import jwt
    s = get_settings()
    return jwt.encode({"sub": str(u.id), "typ": "mfa", "exp": int(time.time()) + 300}, s.jwt_secret, algorithm=s.jwt_alg)


def read_mfa_token(tok: str) -> uuid.UUID:
    import jwt
    s = get_settings()
    try:
        p = jwt.decode(tok, s.jwt_secret, algorithms=[s.jwt_alg])
        assert p.get("typ") == "mfa"
        return uuid.UUID(p["sub"])
    except Exception:  # noqa: BLE001
        raise AppError("AUTH_MFA_INVALID") from None


# ---------------- 会话 ----------------
def open_session(db: Session, u: User, ip: str | None, ua: str | None) -> tuple[UserSession, bool]:
    """返回 (会话, 是否新 IP)。新 IP 触发异地登录提醒。"""
    with bypass_tenant():
        known = db.scalar(select(UserSession.id).where(UserSession.user_id == u.id, UserSession.ip == ip).limit(1)) if ip else True
        had_any = db.scalar(select(UserSession.id).where(UserSession.user_id == u.id).limit(1))
    s = UserSession(tenant_id=u.tenant_id, user_id=u.id, ip=ip, user_agent=(ua or "")[:256],
                    expires_at=utcnow() + timedelta(minutes=get_settings().access_token_minutes))
    db.add(s)
    db.flush()
    return s, bool(had_any and not known)


def touch(db: Session, sid: uuid.UUID, uid: uuid.UUID):
    s = db.get(UserSession, sid)
    if not s or s.user_id != uid or s.revoked_at is not None:
        raise AppError("AUTH_SESSION_REVOKED")
    now = utcnow()
    if (now - s.last_seen_at).total_seconds() > 60:
        s.last_seen_at = now


def revoke_all(db: Session, uid: uuid.UUID, reason: str, except_sid: uuid.UUID | None = None) -> int:
    q = update(UserSession).where(UserSession.user_id == uid, UserSession.revoked_at.is_(None))
    if except_sid:
        q = q.where(UserSession.id != except_sid)
    with bypass_tenant():
        return db.execute(q.values(revoked_at=utcnow(), revoke_reason=reason)).rowcount
