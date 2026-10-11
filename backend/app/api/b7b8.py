"""B7/B8：独立站、种子/预热、OAuth 桩、FB/IG、电话任务、OpenAPI/Webhook/MCP、IM、按商机付费、ERP、学院/反馈、子处理者、SLA。"""

import hashlib
import uuid
from datetime import date

from fastapi import APIRouter, Depends, File, Header, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.platform import platform_admin
from app.b7 import delivery, social
from app.b7 import site as site_mod
from app.b8 import platform as plat_mod
from app.core.audit import write_audit
from app.core.db import bypass_tenant, get_db, utcnow
from app.core.deps import CurrentUser, get_current_user, require
from app.core.errors import AppError
from app.models import (
    AcademyArticle,
    ApiKey,
    ErpConnection,
    FeedbackItem,
    ImChannel,
    ImMessage,
    LeadCharge,
    Mailbox,
    MetaChannel,
    MetaMessage,
    PhoneTask,
    Plan,
    RoadmapItem,
    SeedTest,
    Site,
    SitePage,
    SlaCredit,
    Subprocessor,
    SubprocessorNotice,
    WarmupExchange,
    WebhookDelivery,
    WebhookEndpoint,
)

router = APIRouter(prefix="/api/v1")
public = APIRouter(tags=["public"])
plat = APIRouter(prefix="/api/v1/platform", dependencies=[Depends(platform_admin)], tags=["platform"])


def audit(db, cu, request, action, otype=None, oid=None, after=None):
    write_audit(db, tenant_id=cu.tenant_id, action=action, actor_id=cu.id, object_type=otype, object_id=oid, after=after, request=request)


def _get(db, m, i):
    o = db.get(m, i)
    if not o or getattr(o, "deleted_at", None):
        raise AppError("COMMON_NOT_FOUND")
    return o


# ---------- 独立站 ----------
class SiteIn(BaseModel):
    slug: str = Field(min_length=3, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    lang: str = "en"


@router.get("/sites", tags=["site"])
def list_sites(cu: CurrentUser = Depends(require("kb.manage")), db: Session = Depends(get_db)):
    return [
        {"id": str(s.id), "slug": s.slug, "title": s.title, "status": s.status, "lang": s.lang}
        for s in db.scalars(select(Site).where(Site.deleted_at.is_(None)))
    ]


@router.post("/sites", tags=["site"])
def create_site(body: SiteIn, request: Request, cu: CurrentUser = Depends(require("kb.manage")), db: Session = Depends(get_db)):
    s = site_mod.create_site(db, slug=body.slug, title=body.title, lang=body.lang, user_id=cu.id)
    audit(db, cu, request, "site.create", "site", s.id, body.model_dump())
    db.commit()
    return {"id": str(s.id), "slug": s.slug, "preview": f"/s/{s.slug}/"}


@router.post("/sites/{sid}/products:generate", tags=["site"])
def gen_products(sid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("kb.manage")), db: Session = Depends(get_db)):
    s = _get(db, Site, sid)
    n = site_mod.generate_product_pages(db, s, cu.id)
    audit(db, cu, request, "site.gen_products", "site", s.id, {"n": n})
    db.commit()
    return {"pages": n}


@router.post("/sites/{sid}:publish", tags=["site"])
def publish_site(sid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("kb.manage")), db: Session = Depends(get_db)):
    s = site_mod.publish(db, _get(db, Site, sid))
    audit(db, cu, request, "site.publish", "site", s.id)
    db.commit()
    return {"status": s.status, "url": f"/s/{s.slug}/"}


@router.get("/sites/{sid}/pages", tags=["site"])
def site_pages(sid: uuid.UUID, cu: CurrentUser = Depends(require("kb.manage")), db: Session = Depends(get_db)):
    _get(db, Site, sid)
    return [
        {"id": str(p.id), "path": p.path, "title": p.title, "page_type": p.page_type}
        for p in db.scalars(select(SitePage).where(SitePage.site_id == sid, SitePage.deleted_at.is_(None)))
    ]


@public.get("/s/{slug}/robots.txt")
def robots(slug: str):
    return PlainTextResponse(site_mod.robots_txt(slug))


@public.get("/s/{slug}/sitemap.xml")
def sitemap(slug: str, db: Session = Depends(get_db)):
    return Response(site_mod.sitemap_xml(db, slug), media_type="application/xml")


@public.get("/s/{slug}/")
@public.get("/s/{slug}/{path:path}")
def public_site(slug: str, path: str = "", db: Session = Depends(get_db)):
    r = site_mod.render_page(db, slug, "/" + path if path else "/")
    return HTMLResponse(r["html"])


@router.post("/companies/{cid}/tech-fingerprint", tags=["site"])
async def tech_fp(cid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("company.manage")), db: Session = Depends(get_db)):
    from app.models import Company

    co = _get(db, Company, cid)
    html = ""
    if co.domain:
        try:
            from app.data.safe_http import safe_get

            r = safe_get(f"https://{co.domain}", timeout=6, max_bytes=50_000)
            html = r.text[:50000]
            fp = site_mod.fingerprint(html, dict(r.headers))
        except Exception:  # noqa: BLE001
            fp = site_mod.fingerprint("", {})
            fp["note"] = "fetch_failed"
    else:
        fp = {"technologies": [], "note": "no_domain"}
    co.tech_stack = fp
    audit(db, cu, request, "company.tech_fingerprint", "company", co.id, fp)
    db.commit()
    return fp


# ---------- 种子 / 预热 / OAuth ----------
class SeedIn(BaseModel):
    mailbox_id: uuid.UUID | None = None
    subject: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=5000)


@router.post("/seed-tests", tags=["deliverability"])
def seed_test(body: SeedIn, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, body.mailbox_id) if body.mailbox_id else None
    t = delivery.run_seed_test(db, mb, body.subject, body.body, cu.id)
    audit(db, cu, request, "seed.test", "seed_test", t.id, {"inbox_rate": t.inbox_rate})
    db.commit()
    return {"id": str(t.id), "inbox_rate": t.inbox_rate, "results": t.results}


@router.get("/seed-tests", tags=["deliverability"])
def list_seeds(cu: CurrentUser = Depends(require("mailbox.read")), db: Session = Depends(get_db)):
    return [
        {"id": str(t.id), "subject": t.subject, "inbox_rate": t.inbox_rate, "status": t.status, "at": t.created_at.isoformat()}
        for t in db.scalars(select(SeedTest).order_by(SeedTest.created_at.desc()).limit(20))
    ]


@router.post("/mailboxes/{mid}/warmup:tick", tags=["deliverability"])
def warmup_tick(mid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    mb = _get(db, Mailbox, mid)
    r = delivery.warmup_tick(db, mb)
    audit(db, cu, request, "mailbox.warmup_tick", "mailbox", mb.id, r)
    db.commit()
    return r


@router.get("/mailboxes/{mid}/warmup", tags=["deliverability"])
def warmup_log(mid: uuid.UUID, cu: CurrentUser = Depends(require("mailbox.read")), db: Session = Depends(get_db)):
    _get(db, Mailbox, mid)
    rows = db.scalars(select(WarmupExchange).where(WarmupExchange.mailbox_id == mid).order_by(WarmupExchange.day.desc()).limit(100))
    return [{"day": r.day.isoformat(), "direction": r.direction, "status": r.status} for r in rows]


@router.get("/mailboxes/oauth/{provider}:start", tags=["mailboxes"])
def oauth_start(provider: str, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    return delivery.oauth_start(db, provider, cu.id)


class OAuthStubIn(BaseModel):
    email: str = Field(min_length=5, max_length=254)


@router.post("/mailboxes/oauth/{provider}:complete-stub", tags=["mailboxes"])
def oauth_stub(
    provider: str, body: OAuthStubIn, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)
):
    mb = delivery.oauth_complete_stub(db, provider, body.email, cu.id)
    audit(db, cu, request, "mailbox.oauth_stub", "mailbox", mb.id, {"provider": provider})
    db.commit()
    return {"id": str(mb.id), "address": mb.address, "oauth_status": mb.oauth_status, "note": "stub：仍只发往本地 SMTP sink"}


@router.post("/mailboxes/oauth/{provider}:callback", tags=["mailboxes"])
def oauth_callback(provider: str):
    raise AppError(
        "DELIVERY_PROVIDER_UNAVAILABLE", details={"provider": provider, "reason": "OAuth 回调需配置 Client ID 并完成厂商验证后启用"}
    )


class VerifyIn(BaseModel):
    phone: str


@router.post("/contacts:phone-verify", tags=["compliance"])
def verify_phone(body: VerifyIn, cu: CurrentUser = Depends(require("contact.verify")), db: Session = Depends(get_db)):
    return delivery.verify_phone_mock(body.phone)


# ---------- 电话 / Meta ----------
class PhoneIn(BaseModel):
    phone: str
    script: str | None = None
    contact_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None


@router.post("/phone-tasks", tags=["phone"])
def phone_create(body: PhoneIn, request: Request, cu: CurrentUser = Depends(require("channel.linkedin")), db: Session = Depends(get_db)):
    t = social.create_phone_task(
        db, assignee_id=body.assignee_id or cu.id, phone=body.phone, script=body.script, contact_id=body.contact_id
    )
    audit(db, cu, request, "phone.task_create", "phone_task", t.id)
    db.commit()
    return {"id": str(t.id), "status": t.status, "note": "系统不会自动外呼"}


@router.get("/phone-tasks", tags=["phone"])
def phone_list(cu: CurrentUser = Depends(require("channel.linkedin")), db: Session = Depends(get_db)):
    q = select(PhoneTask).where(PhoneTask.deleted_at.is_(None), PhoneTask.status == "todo")
    if "channel.wa" not in cu.permissions:
        q = q.where(PhoneTask.assignee_id == cu.id)
    return [
        {"id": str(t.id), "phone": t.phone, "script": t.script, "status": t.status, "due_at": t.due_at.isoformat() if t.due_at else None}
        for t in db.scalars(q.limit(100))
    ]


class PhoneDone(BaseModel):
    status: str = Field(pattern="^(done|no_answer|wrong_number|skipped)$")
    note: str | None = None
    outcome: str | None = None


@router.post("/phone-tasks/{tid}:complete", tags=["phone"])
def phone_done(
    tid: uuid.UUID, body: PhoneDone, request: Request, cu: CurrentUser = Depends(require("channel.linkedin")), db: Session = Depends(get_db)
):
    t = _get(db, PhoneTask, tid)
    social.complete_phone(db, t, body.status, body.note, body.outcome)
    audit(db, cu, request, "phone.task_complete", "phone_task", t.id, body.model_dump())
    db.commit()
    return {"id": str(t.id), "status": t.status}


class MetaIn(BaseModel):
    platform: str = Field(pattern="^(facebook|instagram)$")
    page_id: str
    page_name: str
    access_token: str | None = None


@router.post("/meta/channels", tags=["meta"])
def meta_add(body: MetaIn, request: Request, cu: CurrentUser = Depends(require("channel.wa")), db: Session = Depends(get_db)):
    ch = MetaChannel(**body.model_dump(), status="active" if body.access_token else "mock", created_by=cu.id)
    db.add(ch)
    db.flush()
    audit(db, cu, request, "meta.channel_create", "meta_channel", ch.id)
    db.commit()
    return {"id": str(ch.id), "status": ch.status}


@router.get("/meta/channels", tags=["meta"])
def meta_list(cu: CurrentUser = Depends(require("channel.wa")), db: Session = Depends(get_db)):
    return [
        {"id": str(c.id), "platform": c.platform, "page_name": c.page_name, "status": c.status}
        for c in db.scalars(select(MetaChannel).where(MetaChannel.deleted_at.is_(None)))
    ]


class MetaSend(BaseModel):
    channel_id: uuid.UUID
    peer_id: str
    body: str
    human_only: bool = False


@router.post("/meta/messages", tags=["meta"])
def meta_send(body: MetaSend, request: Request, cu: CurrentUser = Depends(require("channel.wa")), db: Session = Depends(get_db)):
    ch = _get(db, MetaChannel, body.channel_id)
    m = social.send_meta(db, ch, body.peer_id, body.body, human_only=body.human_only)
    audit(db, cu, request, "meta.send", "meta_message", m.id, {"status": m.status})
    db.commit()
    return {"id": str(m.id), "status": m.status, "human_task": m.human_task}


# ---------- OpenAPI / Webhook / MCP / IM ----------
class KeyIn(BaseModel):
    name: str
    scopes: list[str] = ["read"]


@router.post("/developer/api-keys", tags=["developer"])
def create_key(body: KeyIn, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    k, raw = plat_mod.create_api_key(db, body.name, body.scopes, cu.id)
    audit(db, cu, request, "apikey.create", "api_key", k.id, {"prefix": k.prefix})
    db.commit()
    return {"id": str(k.id), "prefix": k.prefix, "key": raw, "note": "请立即保存，明文只显示一次"}


@router.get("/developer/api-keys", tags=["developer"])
def list_keys(cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    return [
        {"id": str(k.id), "name": k.name, "prefix": k.prefix, "scopes": k.scopes, "revoked": bool(k.revoked_at)}
        for k in db.scalars(select(ApiKey).where(ApiKey.deleted_at.is_(None)))
    ]


@router.delete("/developer/api-keys/{kid}", tags=["developer"])
def revoke_key(kid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    k = _get(db, ApiKey, kid)
    k.revoked_at = utcnow()
    audit(db, cu, request, "apikey.revoke", "api_key", k.id)
    db.commit()
    return {"ok": True}


class WhIn(BaseModel):
    url: str
    events: list[str] = []


@router.post("/developer/webhooks", tags=["developer"])
def add_wh(body: WhIn, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    w = plat_mod.create_webhook(db, body.url, body.events, cu.id)
    audit(db, cu, request, "webhook.create", "webhook_endpoint", w.id)
    db.commit()
    return {"id": str(w.id), "secret": w.secret, "events": w.events}


@router.get("/developer/webhooks", tags=["developer"])
def list_wh(cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    return [
        {"id": str(w.id), "url": w.url, "events": w.events, "enabled": w.enabled, "fail_count": w.fail_count}
        for w in db.scalars(select(WebhookEndpoint).where(WebhookEndpoint.deleted_at.is_(None)))
    ]


@router.post("/developer/webhooks/test", tags=["developer"])
def test_wh(request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    n = plat_mod.emit_webhook(db, "inquiry.created", {"demo": True, "by": str(cu.id)})
    db.commit()
    return {"deliveries": n}


@router.get("/developer/mcp", tags=["developer"])
def mcp(cu: CurrentUser = Depends(require("org.manage"))):
    return plat_mod.mcp_manifest()


@public.get("/openapi-public.json")
def openapi_public(request: Request):
    """精简 OpenAPI 子集（公司/询盘/Agent），需 API Key。"""
    return {
        "openapi": "3.0.3",
        "info": {"title": "TradeLeads Public API", "version": "1.0.0"},
        "paths": {
            "/api/v1/companies": {"get": {"summary": "List companies", "security": [{"ApiKey": []}]}},
            "/api/v1/inquiries": {"get": {"summary": "List inquiries"}},
            "/api/v1/agent/tasks": {"post": {"summary": "Create agent task"}},
        },
        "components": {"securitySchemes": {"ApiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}},
    }


class ImIn(BaseModel):
    provider: str = Field(pattern="^(wecom|dingtalk|feishu|slack)$")
    name: str
    config: dict = {}


@router.post("/im/channels", tags=["im"])
def im_add(body: ImIn, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    ch = ImChannel(provider=body.provider, name=body.name, config=body.config, status="mock", created_by=cu.id)
    db.add(ch)
    db.flush()
    audit(db, cu, request, "im.channel_create", "im_channel", ch.id)
    db.commit()
    return {"id": str(ch.id), "status": ch.status}


@router.get("/im/channels", tags=["im"])
def im_list(cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    return [
        {"id": str(c.id), "provider": c.provider, "name": c.name, "status": c.status}
        for c in db.scalars(select(ImChannel).where(ImChannel.deleted_at.is_(None)))
    ]


class ImMsgIn(BaseModel):
    user_ext_id: str = "u1"
    text: str


@router.post("/im/channels/{cid}/messages:inbound-mock", tags=["im"])
def im_in(cid: uuid.UUID, body: ImMsgIn, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    ch = _get(db, ImChannel, cid)
    r = plat_mod.im_inbound(db, ch, body.user_ext_id, body.text)
    audit(db, cu, request, "im.inbound_mock", "im_channel", ch.id)
    db.commit()
    return r


# ---------- 按商机付费 / ERP ----------
@router.get("/billing/lead-charges", tags=["billing"])
def lead_charges(cu: CurrentUser = Depends(require("billing.read")), db: Session = Depends(get_db)):
    return [
        {"id": str(c.id), "opportunity_id": str(c.opportunity_id), "amount_minor": c.amount_minor, "status": c.status}
        for c in db.scalars(select(LeadCharge).order_by(LeadCharge.created_at.desc()).limit(100))
    ]


class ErpIn(BaseModel):
    name: str
    kind: str = "csv"


@router.post("/erp/connections", tags=["erp"])
def erp_add(body: ErpIn, request: Request, cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    c = ErpConnection(name=body.name, kind=body.kind, created_by=cu.id)
    db.add(c)
    db.flush()
    audit(db, cu, request, "erp.connect", "erp_connection", c.id)
    db.commit()
    return {"id": str(c.id)}


@router.get("/erp/connections", tags=["erp"])
def erp_list(cu: CurrentUser = Depends(require("org.manage")), db: Session = Depends(get_db)):
    return [
        {"id": str(c.id), "name": c.name, "kind": c.kind, "status": c.status}
        for c in db.scalars(select(ErpConnection).where(ErpConnection.deleted_at.is_(None)))
    ]


@router.post("/erp/connections/{cid}/export", tags=["erp"])
def erp_export(
    cid: uuid.UUID,
    object_type: str = "account",
    request: Request = None,
    cu: CurrentUser = Depends(require("org.manage")),
    db: Session = Depends(get_db),
):
    conn = _get(db, ErpConnection, cid)
    job = plat_mod.erp_export_csv(db, conn, object_type, cu.id)
    audit(db, cu, request, "erp.export", "erp_job", job.id, {"rows": job.rows})
    db.commit()
    return {"id": str(job.id), "rows": job.rows, "file_name": job.file_name, "csv": job.detail.get("csv", "")[:20000]}


@router.post("/erp/connections/{cid}/import", tags=["erp"])
async def erp_import(
    cid: uuid.UUID,
    object_type: str = "product",
    file: UploadFile = File(...),
    request: Request = None,
    cu: CurrentUser = Depends(require("org.manage")),
    db: Session = Depends(get_db),
):
    conn = _get(db, ErpConnection, cid)
    text = (await file.read()).decode("utf-8-sig", "replace")
    job = plat_mod.erp_import_csv(db, conn, object_type, text, cu.id)
    audit(db, cu, request, "erp.import", "erp_job", job.id, {"rows": job.rows})
    db.commit()
    return {"id": str(job.id), "rows": job.rows, "errors": job.detail.get("errors")}


# ---------- 学院 / 反馈 / 路线图 / 子处理者 / SLA ----------
@router.get("/academy", tags=["success"])
def academy(db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    plat_mod.seed_academy(db)
    db.commit()
    with bypass_tenant():
        rows = list(db.scalars(select(AcademyArticle).where(AcademyArticle.published.is_(True))))
    return [{"slug": a.slug, "title": a.title, "category": a.category, "body_md": a.body_md} for a in rows]


@router.get("/roadmap", tags=["success"])
def roadmap(db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    plat_mod.seed_academy(db)
    db.commit()
    with bypass_tenant():
        rows = list(db.scalars(select(RoadmapItem).order_by(RoadmapItem.created_at)))
    return [{"id": str(r.id), "title": r.title, "status": r.status, "quarter": r.quarter, "votes": r.votes} for r in rows]


class FbIn(BaseModel):
    title: str
    body: str
    kind: str = "idea"


@router.post("/feedback", tags=["success"])
def feedback(body: FbIn, request: Request, cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    f = FeedbackItem(**body.model_dump(), created_by=cu.id)
    db.add(f)
    db.flush()
    audit(db, cu, request, "feedback.create", "feedback_item", f.id)
    db.commit()
    return {"id": str(f.id)}


@router.get("/feedback", tags=["success"])
def feedback_list(cu: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)):
    return [
        {"id": str(f.id), "title": f.title, "kind": f.kind, "status": f.status, "votes": f.votes}
        for f in db.scalars(select(FeedbackItem).order_by(FeedbackItem.created_at.desc()).limit(50))
    ]


@router.get("/subprocessors", tags=["success"])
def subprocessors(db: Session = Depends(get_db), cu: CurrentUser = Depends(get_current_user)):
    plat_mod.seed_academy(db)
    db.commit()
    with bypass_tenant():
        rows = list(db.scalars(select(Subprocessor).where(Subprocessor.active.is_(True))))
    return [{"name": s.name, "purpose": s.purpose, "location": s.location, "url": s.url} for s in rows]


@plat.post("/subprocessors/notices")
def sub_notice(body: dict, db: Session = Depends(get_db)):
    n = SubprocessorNotice(
        subprocessor_id=uuid.UUID(body["subprocessor_id"]) if body.get("subprocessor_id") else uuid.uuid4(),
        change=body.get("change", "added"),
        detail=body.get("detail", ""),
        effective_on=date.fromisoformat(body.get("effective_on", str(date.today()))),
    )
    db.add(n)
    db.flush()
    cnt = plat_mod.notify_subprocessor_change(db, n)
    db.commit()
    return {"id": str(n.id), "notified_tenants": cnt}


@plat.post("/sla:credit")
def sla_run(period: str | None = None, db: Session = Depends(get_db)):
    # run per tenant
    from app.core.db import tenant_context
    from app.models import Tenant

    out = {}
    with bypass_tenant():
        tids = [t.id for t in db.scalars(select(Tenant).where(Tenant.status == "active"))]
    for tid in tids:
        with tenant_context(tid):
            db.info["tenant_id"] = tid
            out[str(tid)] = len(plat_mod.sla_credit_tick(db, period))
            db.commit()
    return out


@plat.post("/seeds:init")
def init_seeds(db: Session = Depends(get_db)):
    return {"seed_inboxes": delivery.seed_inboxes(db), "warmup_peers": delivery.ensure_warmup_peers(db)}


# suppressions / products import quick
@router.post("/suppressions:import", tags=["compliance"])
async def import_supp(
    file: UploadFile = File(...),
    request: Request = None,
    cu: CurrentUser = Depends(require("compliance.manage")),
    db: Session = Depends(get_db),
):
    from app.compliance.suppression import add_suppression

    text = (await file.read()).decode("utf-8-sig", "replace")
    n = 0
    for line in text.splitlines():
        email = line.strip().split(",")[0].strip().lower()
        if "@" in email:
            add_suppression(db, key_type="email", value=email, reason="manual", source_ref="csv")
            n += 1
    audit(db, cu, request, "suppression.import", after={"n": n})
    db.commit()
    return {"imported": n}


_ = (Header, Query, hashlib, ImMessage, LeadCharge, MetaMessage, Plan, SlaCredit, WebhookDelivery, date)


# ---------- 公开 API（X-API-Key 鉴权） ----------
def api_key_auth(scope: str):
    def dep(request: Request, x_api_key: str | None = Header(None), db: Session = Depends(get_db)) -> ApiKey:
        from app.core.context import tenant_id_var

        k = plat_mod.verify_api_key(db, x_api_key or "")
        if not k:
            raise AppError("AUTH_API_KEY_INVALID")
        from app.models import Tenant

        tenant = db.get(Tenant, k.tenant_id)
        if not tenant or tenant.status != "active":
            raise AppError("AUTH_USER_DISABLED")
        request.state.idempotency_identity = f"api:{k.tenant_id}:{k.id}"
        request.state.idempotency_authorization = ",".join(sorted(k.scopes or []))
        if scope not in (k.scopes or []):
            raise AppError("COMMON_FORBIDDEN", details={"scope": scope})
        tenant_id_var.set(k.tenant_id)
        db.info["tenant_id"] = k.tenant_id
        k.last_used_at = utcnow()
        db.flush()
        return k

    return dep


@public.get("/api/v1/open/companies", tags=["open-api"])
def open_companies(q: str | None = Query(None), k: ApiKey = Depends(api_key_auth("read")), db: Session = Depends(get_db)):
    from app.models import Company

    stmt = select(Company).where(Company.deleted_at.is_(None))
    if q:
        stmt = stmt.where(Company.name.ilike(f"%{q}%"))
    rows = [{"id": str(c.id), "name": c.name, "domain": c.domain, "country": c.country} for c in db.scalars(stmt.limit(100))]
    db.commit()
    return {"items": rows}


@public.post("/api/v1/open/feedback", tags=["open-api"])
def open_feedback(body: FbIn, k: ApiKey = Depends(api_key_auth("write")), db: Session = Depends(get_db)):
    f = FeedbackItem(**body.model_dump(), created_by=k.created_by)
    db.add(f)
    db.commit()
    return {"id": str(f.id)}
