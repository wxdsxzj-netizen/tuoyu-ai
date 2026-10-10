"""退订令牌（HMAC 签名，不落库）与页脚生成。RFC 8058 一键退订：List-Unsubscribe + List-Unsubscribe-Post。"""
import base64
import hashlib
import hmac
import json
import time
import uuid

from app.compliance.suppression import key_hash
from app.core.config import get_settings
from app.core.errors import AppError


def _secret() -> bytes:
    s = get_settings()
    return (s.unsubscribe_secret or hashlib.sha256(("unsub:" + s.jwt_secret).encode()).hexdigest()).encode()


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(tenant_id, email: str, contact_id=None) -> str:
    """令牌只携带租户、邮箱哈希、域名与联系人 ID，不含明文邮箱。"""
    payload = {"t": str(tenant_id), "h": key_hash("email", email), "d": email.split("@")[-1].lower(),
               "c": str(contact_id) if contact_id else None, "i": int(time.time())}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:20])
    return f"{body}.{sig}"


def parse_token(token: str) -> dict:
    try:
        body, sig = token.split(".", 1)
        expected = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()[:20])
        if not hmac.compare_digest(sig, expected):
            raise ValueError
        p = json.loads(_unb64(body))
        p["t"] = uuid.UUID(p["t"])
        return p
    except Exception:  # noqa: BLE001
        raise AppError("COMPLIANCE_UNSUBSCRIBE_INVALID") from None


def unsubscribe_url(tenant_id, email, contact_id=None) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/u/{make_token(tenant_id, email, contact_id)}"


def list_unsubscribe_headers(tenant_id, email, contact_id=None) -> dict:
    return {"List-Unsubscribe": f"<{unsubscribe_url(tenant_id, email, contact_id)}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}


def build_footer(settings_row, *, unsubscribe_link: str, lang: str = "en", gdpr: bool = False) -> dict:
    """CAN-SPAM / GDPR / CASL 必备页脚：发件人身份、实体地址、退订方式（+ 欧盟/英国的数据来源与权利说明）。"""
    missing = [k for k, v in (("identity", settings_row and settings_row.sender_legal_name),
                              ("address", settings_row and settings_row.postal_address)) if not v]
    name = (settings_row.sender_legal_name if settings_row else None) or ""
    addr = (settings_row.postal_address if settings_row else None) or ""
    priv = settings_row.privacy_email if settings_row else None
    if lang == "zh":
        lines = [f"发件人：{name}", f"地址：{addr}", f"不想再收到此类邮件？退订：{unsubscribe_link}"]
        if gdpr:
            lines.append("我们基于正当利益联系您；您的公开商务联系方式来源于公开渠道。您可随时行使访问、更正、删除与反对的权利"
                         + (f"，联系 {priv}" if priv else "") + "。")
    else:
        lines = [f"Sent by {name}", addr, f"Don't want these emails? Unsubscribe: {unsubscribe_link}"]
        if gdpr:
            lines.append("We contact you on the basis of legitimate interest; your business contact details were obtained from "
                         "public sources. You may access, correct, delete or object to processing at any time"
                         + (f" by writing to {priv}" if priv else "") + ".")
    text = "\n".join(x for x in lines if x)
    return {"text": text, "missing": missing}
