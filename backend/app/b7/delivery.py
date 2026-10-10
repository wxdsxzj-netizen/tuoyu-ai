"""种子测试（mock）、预热互发网络（模拟节点）、OAuth 发件箱连接桩、电话/WhatsApp 号码验证（mock）。"""
import hashlib
import random
import secrets
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import bypass_tenant, utcnow
from app.core.errors import AppError
from app.models import Mailbox, SeedInbox, SeedTest, WarmupExchange, WarmupPeer

OAUTH_PROVIDERS = {
    "google": {"auth_url": "https://accounts.google.com/o/oauth2/v2/auth", "scopes": ["gmail.send", "gmail.readonly"],
               "note": "需完成 Google 受限范围验证（CASA）后才能对真实账号生效；当前为连接桩。"},
    "microsoft": {"auth_url": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
                  "scopes": ["Mail.Send", "Mail.Read"], "note": "需完成 Microsoft 发布者验证；当前为连接桩。"},
}


def seed_inboxes(db: Session) -> int:
    samples = [("seed1@mock-gmail.test", "gmail", "US"), ("seed2@mock-outlook.test", "outlook", "DE"),
               ("seed3@mock-yahoo.test", "yahoo", "GB"), ("seed4@mock-gmail.test", "gmail", "SG"),
               ("seed5@mock-outlook.test", "outlook", "US")]
    n = 0
    with bypass_tenant():
        for addr, prov, reg in samples:
            if db.scalar(select(SeedInbox.id).where(SeedInbox.address == addr)):
                continue
            db.add(SeedInbox(address=addr, provider=prov, region=reg))
            n += 1
        db.flush()
    return n


def run_seed_test(db: Session, mailbox: Mailbox | None, subject: str, body: str, user_id) -> SeedTest:
    seed_inboxes(db)
    with bypass_tenant():
        boxes = list(db.scalars(select(SeedInbox).where(SeedInbox.active.is_(True))))
    if not boxes:
        raise AppError("COMMON_INVALID_STATE", details={"seed": "无可用种子邮箱"})
    t = SeedTest(mailbox_id=mailbox.id if mailbox else None, subject=subject, body=body, status="running", created_by=user_id)
    db.add(t)
    db.flush()
    rng = random.Random(hashlib.sha256(f"{subject}:{mailbox.id if mailbox else ''}".encode()).hexdigest())
    results = []
    inbox = 0
    for b in boxes:
        # mock：健康分越高进箱率越高
        health = (mailbox.health_score if mailbox else 80) or 80
        p_inbox = min(0.95, 0.4 + health / 200)
        folder = "inbox" if rng.random() < p_inbox else ("spam" if rng.random() < 0.7 else "missing")
        if folder == "inbox":
            inbox += 1
        results.append({"address": b.address, "provider": b.provider, "region": b.region, "folder": folder,
                        "latency_s": round(1 + rng.random() * 8, 1)})
    t.results, t.inbox_rate, t.status, t.finished_at = results, round(inbox / len(boxes), 3), "done", utcnow()
    db.flush()
    return t


def ensure_warmup_peers(db: Session) -> int:
    n = 0
    with bypass_tenant():
        for i in range(1, 21):
            addr = f"peer{i}@warmup-net.mock"
            if db.scalar(select(WarmupPeer.id).where(WarmupPeer.address == addr)):
                continue
            db.add(WarmupPeer(address=addr, domain="warmup-net.mock", region=["US", "DE", "SG", "JP"][i % 4],
                             reputation=0.6 + (i % 5) * 0.08))
            n += 1
        db.flush()
    return n


def warmup_tick(db: Session, mailbox: Mailbox, today: date | None = None) -> dict:
    """模拟与预热网络互发：每天按爬坡额度生成若干 simulated 交换记录。"""
    from app.outreach.health import warmup_day, warmup_quota
    today = today or date.today()
    if mailbox.status not in ("warming", "active"):
        return {"exchanges": 0}
    ensure_warmup_peers(db)
    day_no = warmup_day(mailbox, today) if mailbox.status == "warming" else 22
    quota = min(5, max(1, warmup_quota(day_no, mailbox.daily_cold_limit or 40) // 4))
    with bypass_tenant():
        peers = list(db.scalars(select(WarmupPeer).where(WarmupPeer.active.is_(True))))
    existing = db.scalar(select(WarmupExchange.id).where(WarmupExchange.mailbox_id == mailbox.id, WarmupExchange.day == today))
    if existing:
        return {"exchanges": 0, "note": "今日已跑过"}
    n = 0
    for peer in peers[:quota]:
        db.add(WarmupExchange(mailbox_id=mailbox.id, peer_id=peer.id, direction="out", status="simulated", day=today))
        db.add(WarmupExchange(mailbox_id=mailbox.id, peer_id=peer.id, direction="in", status="opened" if peer.reputation > 0.7 else "simulated", day=today))
        n += 2
    db.flush()
    return {"exchanges": n, "peers": quota}


def oauth_start(db: Session, provider: str, user_id, redirect_uri: str | None = None) -> dict:
    if provider not in OAUTH_PROVIDERS:
        raise AppError("COMMON_VALIDATION", details={"provider": list(OAUTH_PROVIDERS)})
    s = get_settings()
    meta = OAUTH_PROVIDERS[provider]
    state = secrets.token_urlsafe(24)
    # 无真实 client_id 时返回说明性连接桩
    client_id = getattr(s, f"{provider}_oauth_client_id", "") or ""
    stub = not bool(client_id)
    url = None
    if not stub:
        from urllib.parse import urlencode
        url = meta["auth_url"] + "?" + urlencode({"client_id": client_id, "response_type": "code", "scope": " ".join(meta["scopes"]),
                                                   "state": state, "redirect_uri": redirect_uri or "https://app.example/oauth/callback"})
    return {"provider": provider, "state": state, "authorize_url": url, "stub": stub, "scopes": meta["scopes"], "note": meta["note"],
            "next_steps": ["在云控制台创建 OAuth 应用并完成验证", "配置 TL_GOOGLE_OAUTH_CLIENT_ID / TL_MICROSOFT_OAUTH_CLIENT_ID",
                           "回调地址指向 /api/v1/mailboxes/oauth/{provider}:callback", "验证通过前可用「完成桩连接」创建 stub 发件箱用于流程演练"]}


def oauth_complete_stub(db: Session, provider: str, email: str, user_id) -> Mailbox:
    """流程演练：创建标记为 oauth stub 的发件箱，仍走本地 SMTP sink，不访问 Google/Microsoft。"""
    if provider not in OAUTH_PROVIDERS:
        raise AppError("COMMON_VALIDATION")
    email = email.lower().strip()
    mb = Mailbox(address=email, display_name=email.split("@")[0], provider=provider, owner_id=user_id,
                 smtp_host="localhost", smtp_port=1025, smtp_security="none", smtp_username=email,
                 imap_host="localhost", imap_port=1143, imap_username=email, auth_status="ok", status="active",
                 oauth_provider=provider, oauth_status="stub",
                 oauth_meta={"connected_at": utcnow().isoformat(), "mode": "stub", "note": OAUTH_PROVIDERS[provider]["note"]},
                 created_by=user_id)
    db.add(mb)
    db.flush()
    return mb


def verify_phone_mock(phone: str) -> dict:
    """电话验证 mock：E.164 格式 + 号段启发式。"""
    import re
    p = re.sub(r"[^\d+]", "", phone or "")
    if not re.match(r"^\+?[1-9]\d{6,14}$", p):
        return {"status": "invalid", "line_type": None, "whatsapp": False, "provider": "mock", "confidence": 0.9}
    wa = p.endswith(("0", "2", "4", "6", "8"))  # 确定性假结果
    return {"status": "valid", "line_type": "mobile" if len(p) >= 10 else "landline", "whatsapp": wa,
            "provider": "mock", "confidence": 0.75, "e164": p if p.startswith("+") else "+" + p}
