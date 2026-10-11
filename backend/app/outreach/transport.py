"""SMTP / IMAP 传输。安全护栏：outbound_allow_real=False（默认）时只允许连接本地测试邮件服务器，绝不外发真实邮件。"""
import imaplib
import re
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import get_settings
from app.core.errors import AppError
from app.outreach.crypto import decrypt


def _guard(host: str | None):
    s = get_settings()
    if not host:
        raise AppError("COMMON_REQUIRED", details={"field": "smtp_host"})
    if not s.outbound_allow_real and host.lower() not in {h.strip().lower() for h in s.outbound_sink_hosts.split(",")}:
        raise AppError("DELIVERY_SMTP_HOST_NOT_ALLOWED", details={"host": host})


class SmtpResult:
    def __init__(self, ok: bool, code: int | None = None, kind: str | None = None, detail: str = ""):
        self.ok, self.code, self.kind, self.detail = ok, code, kind, detail  # kind: hard_bounce / soft / auth / connection


def recipient_failure(code, detail):
    text = detail.decode(errors="replace") if isinstance(detail, bytes) else str(detail)
    status = re.search(r"\b([245]\.\d{1,3}\.\d{1,3})\b", text)
    permanent_address = status and status[1] in {"5.1.1", "5.1.2", "5.1.3", "5.1.6", "5.2.1"}
    kind = "hard_bounce" if code >= 500 and permanent_address else ("rejected" if code >= 500 else "soft")
    return SmtpResult(False, code, kind, text[:300])


def _connect(mb, timeout=20):
    _guard(mb.smtp_host)
    bearer = None
    if getattr(mb, 'oauth_provider', None) and mb.oauth_status != 'stub':
        from app.outreach import oauth
        oauth.validate_transport(mb, 'smtp')
        bearer = oauth.access_token(mb)
    port = mb.smtp_port or (465 if mb.smtp_security == "ssl" else 587)
    if mb.smtp_security == "ssl":
        c = smtplib.SMTP_SSL(mb.smtp_host, port, timeout=timeout, context=ssl.create_default_context())
    else:
        c = smtplib.SMTP(mb.smtp_host, port, timeout=timeout)
        if mb.smtp_security == "starttls":
            c.starttls(context=ssl.create_default_context())
    try:
        if bearer:
            c.ehlo_or_helo_if_needed()
            c.auth('XOAUTH2', lambda challenge=None: oauth.sasl(mb.address, bearer) if challenge is None else '')
        else:
            pw = decrypt(mb.smtp_password_enc)
            if mb.smtp_username and pw:
                c.login(mb.smtp_username, pw)
    except Exception:
        c.close()
        raise
    return c


def test_smtp(mb) -> dict:
    try:
        c = _connect(mb)
        c.noop()
        c.quit()
        return {"ok": True}
    except AppError:
        raise
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "reason": "AUTH_FAILED", "detail": str(e.smtp_error)[:200]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "CONNECTION_FAILED", "detail": f"{type(e).__name__}: {e}"[:200]}


def send(mb, msg: EmailMessage) -> SmtpResult:
    try:
        c = _connect(mb)
    except AppError:
        raise
    except smtplib.SMTPAuthenticationError as e:
        return SmtpResult(False, e.smtp_code, "auth", str(e.smtp_error)[:300])
    except Exception as e:  # noqa: BLE001
        return SmtpResult(False, None, "connection", f"{type(e).__name__}: {e}"[:300])
    try:
        refused = c.send_message(msg)
        if refused:
            code, text = next(iter(refused.values()))
            return recipient_failure(code, text)
        return SmtpResult(True, 250)
    except smtplib.SMTPRecipientsRefused as e:
        code, text = next(iter(e.recipients.values()))
        return recipient_failure(code, text)
    except smtplib.SMTPResponseException as e:
        # DATA / sender / policy rejection does not prove an invalid recipient.
        return SmtpResult(False, e.smtp_code, "rejected" if e.smtp_code >= 500 else "soft", str(e.smtp_error)[:300])
    except Exception as e:  # noqa: BLE001
        # A disconnect while sending DATA may happen after server acceptance.
        return SmtpResult(False, None, "unknown", f"{type(e).__name__}: {e}"[:300])
    finally:
        try:
            c.quit()
        except Exception:  # noqa: BLE001
            pass


def imap_connect(mb):
    s = get_settings()
    host = mb.imap_host
    if not s.outbound_allow_real and (host or "").lower() not in {h.strip().lower() for h in s.outbound_sink_hosts.split(",")}:
        raise AppError("DELIVERY_SMTP_HOST_NOT_ALLOWED", details={"host": host})
    bearer = None
    if getattr(mb, 'oauth_provider', None) and mb.oauth_status != 'stub':
        from app.outreach import oauth
        oauth.validate_transport(mb, 'imap')
        bearer = oauth.access_token(mb)
    port = mb.imap_port or (993 if mb.imap_ssl else 143)
    c = imaplib.IMAP4_SSL(host, port) if mb.imap_ssl else imaplib.IMAP4(host, port)
    try:
        if bearer:
            c.authenticate('XOAUTH2', lambda challenge: oauth.sasl(mb.address, bearer).encode() if not challenge else b'')
        else:
            c.login(mb.imap_username or mb.address, decrypt(mb.imap_password_enc) or "")
    except Exception:
        c.shutdown()
        raise
    return c
