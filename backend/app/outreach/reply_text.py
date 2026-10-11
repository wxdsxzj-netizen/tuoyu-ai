"""Extract authored reply text and structured delivery-status evidence."""
import re
from email.parser import Parser
from email.utils import parseaddr

from bs4 import BeautifulSoup

QUOTE_HEADER = re.compile(
    r"^(?:on\b.{0,300}\bwrote\s*:|在.{0,300}写道\s*[：:]|am\b.{0,300}schrieb.{0,100}:|"
    r"le\b.{0,300}écrit\s*:|el\b.{0,300}escribió\s*:|[-_]{2,}\s*"
    r"(?:original message|forwarded message|原始邮件|转发邮件).*)$", re.I)
REPLY_SUBJECT = re.compile(r"^\s*(?:re|fw|fwd|aw|wg|sv|tr|rv|回复|答复|转发)\s*[:：]", re.I)


def authored_text(body):
    text = body or ""
    if re.search(r"<(?:html|div|p|blockquote)\b", text, re.I):
        soup = BeautifulSoup(text, "html.parser")
        for node in soup.select("blockquote, .gmail_quote, .yahoo_quoted, .protonmail_quote, .moz-cite-prefix, [type=cite]"):
            if node.parent is not None:
                node.decompose()
        text = soup.get_text("\n")
    lines, result = text.replace("\r\n", "\n").split("\n"), []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if QUOTE_HEADER.match(stripped):
            if re.match(r"^(?:on\b|在|am\b|le\b|el\b)", stripped, re.I) and any(
                    tail.lstrip().startswith(">") for tail in lines[i + 1:]):
                continue  # retain authored inline replies between > quoted lines
            break
        if stripped == "--":
            break
        if re.match(r"^(?:from|发件人)\s*[:：]", stripped, re.I) and re.search(
                r"(?:^|\n)\s*(?:sent|to|subject|发送时间|收件人|主题)\s*[:：]", "\n".join(lines[i + 1:i + 7]), re.I):
            break
        if not stripped.startswith(">"):
            if re.match(r"^(?:unsubscribe|取消订阅|退订)\s*[:：]\s*https?://", stripped, re.I):
                continue  # link-only generated footer is not an authored request
            result.append(line)
    return "\n".join(result).strip()


def authored_subject(subject):
    # A replied-to subject belongs to the previous author. In particular,
    # "Re: Unsubscribe" is not a new unsubscribe request by itself.
    return "" if REPLY_SUBJECT.match(subject or "") else subject or ""


def mime_authored_text(msg, fallback):
    for part in body_parts(msg):
        if part.get_content_type() == "text/html" and not part.get_filename():
            payload = part.get_payload(decode=True)
            if payload:
                return authored_text(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
    return authored_text(fallback)


def body_parts(msg):
    """Do not classify attached/forwarded message bodies as the new reply."""
    if msg.get_filename() or msg.get_content_maintype() == "message":
        return
    if msg.is_multipart():
        for part in msg.get_payload():
            yield from body_parts(part)
    else:
        yield msg


def _recipient_status(part):
    if any(len(part.get_all(name, [])) != 1 for name in ("Final-Recipient", "Action", "Status")):
        return None
    raw = part.get("Final-Recipient", "")
    if ";" not in raw:
        return None
    scheme, address = raw.split(";", 1)
    recipient = (parseaddr(address.strip())[1] or address.strip()).lower()
    action = re.sub(r"\([^)]*\)", "", part.get("Action", "")).strip().lower()
    status = re.sub(r"\([^)]*\)", "", part.get("Status", "")).strip()
    if scheme.strip().lower() != "rfc822" or "@" not in recipient or not re.fullmatch(r"[245]\.\d{1,3}\.\d{1,3}", status):
        return None
    if action not in {"failed", "delayed", "delivered", "relayed", "expanded"}:
        return None
    return {"recipient": recipient, "action": action, "status": status}


def delivery_status(body, msg=None):
    parts = []
    if msg:
        for part in msg.walk():
            if part.get_content_type() == "message/delivery-status":
                payload = part.get_payload()
                if isinstance(payload, list):
                    parts.extend(payload)
    if not parts:
        parts = [Parser().parsestr(block) for block in re.split(r"\n\s*\n", (body or "").replace("\r\n", "\n"))]
    return [status for part in parts if (status := _recipient_status(part))]


def original_message_ids(msg):
    refs = []
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list):
                refs.extend(str(p.get("Message-ID")) for p in payload if p.get("Message-ID"))
    return refs
