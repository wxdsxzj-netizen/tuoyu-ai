"""B3：发现（ICP 评分 / Lookalike / 语义搜索 / 排除规则 / 名单 / 保存搜索与日报）、背调补全（询盘/名片输入、风险处置、分享、PDF）、
触达补全（剧本模板 / A/B 统计 / 沉睡唤醒 / 域名规划 / DNSBL / 追踪 CNAME 与教程 / 单封时间线）、分析补全（切片 / 单位经济 / 早报 / 团队 / AI 效能）。"""  # noqa: E501
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.p5 import rep_out
from app.b3 import analytics as an
from app.b3 import discovery as disc
from app.b3 import outreach as ob
from app.b3 import research as rs
from app.core.audit import write_audit
from app.core.db import bypass_tenant, get_db, tenant_context, utcnow
from app.core.deps import CurrentUser, require
from app.core.errors import AppError
from app.models import (
    BlacklistCheck,
    Company,
    ExclusionRule,
    Icp,
    Inquiry,
    OutboundMessage,
    ProspectList,
    ReportShare,
    ResearchReport,
    SavedSearch,
    SendingDomain,
    Sequence,
    Tenant,
)
from app.research import service as research

router = APIRouter(prefix="/api/v1")
public = APIRouter(prefix="/api/v1/public")


def audit(db, cu, request, action, otype=None, oid=None, after=None):
    write_audit(db, tenant_id=cu.tenant_id, action=action, actor_id=cu.id, object_type=otype, object_id=oid, after=after, request=request)


def _get(db, m, i):
    o = db.get(m, i)
    if not o or getattr(o, "deleted_at", None):
        raise AppError("COMMON_NOT_FOUND")
    return o


def _mine(cu, o):
    if getattr(o, "owner_id", None) not in (None, cu.id) and "crm.all" not in cu.permissions:
        raise AppError("COMMON_NOT_FOUND")
    return o


def _range(days: int):
    now = utcnow()
    return now - timedelta(days=days), now


# ======================= 发现 =======================
@router.get("/discover/icp/{iid}", tags=["discover"])
def discover_icp(iid: uuid.UUID, limit: int = Query(50, le=200), include_excluded: bool = False,
                 cu: CurrentUser = Depends(require("company.read")), db: Session = Depends(get_db)):
    return disc.discover(db, _get(db, Icp, iid), limit=limit, include_excluded=include_excluded)


@router.get("/companies/{cid}/lookalike", tags=["discover"])
def lookalike(cid: uuid.UUID, limit: int = Query(20, le=100), cu: CurrentUser = Depends(require("company.read")), db: Session = Depends(get_db)):  # noqa: E501
    return disc.lookalike(db, _get(db, Company, cid), limit)


@router.get("/search/semantic", tags=["discover"])
def semantic(q: str = Query(min_length=2, max_length=300), limit: int = Query(30, le=200), cu: CurrentUser = Depends(require("company.read")),  # noqa: E501
             db: Session = Depends(get_db)):
    return disc.semantic_search(db, q, limit)


class RuleIn(BaseModel):
    rule_type: str = Field(pattern="^(" + "|".join(disc.RULE_TYPES) + ")$")
    value: str = Field("", max_length=200)
    note: str | None = None


@router.get("/exclusion-rules", tags=["discover"])
def list_rules(cu: CurrentUser = Depends(require("company.read")), db: Session = Depends(get_db)):
    return [{"id": str(r.id), "rule_type": r.rule_type, "value": r.value, "note": r.note} for r in disc.exclusions(db)]


@router.post("/exclusion-rules", tags=["discover"])
def add_rule(body: RuleIn, request: Request, cu: CurrentUser = Depends(require("compliance.manage")), db: Session = Depends(get_db)):
    if body.rule_type != "existing_customer" and not body.value.strip():
        raise AppError("COMMON_VALIDATION", details={"value": "必填"})
    r = ExclusionRule(rule_type=body.rule_type, value=body.value.strip().lower() or "*", note=body.note, created_by=cu.id)
    db.add(r)
    db.flush()
    audit(db, cu, request, "exclusion.add", "exclusion_rule", r.id, body.model_dump())
    db.commit()
    return {"id": str(r.id)}


@router.delete("/exclusion-rules/{rid}", tags=["discover"])
def del_rule(rid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("compliance.manage")), db: Session = Depends(get_db)):
    r = _get(db, ExclusionRule, rid)
    r.deleted_at = utcnow()
    audit(db, cu, request, "exclusion.delete", "exclusion_rule", r.id)
    db.commit()
    return {"ok": True}


# ---------------- 名单 ----------------
class ListIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str = Field("static", pattern="^(static|dynamic)$")
    object_type: str = Field("contact", pattern="^(contact|company)$")
    filters: dict = {}
    member_ids: list[uuid.UUID] = Field([], max_length=100000)


def _l_out(db, pl, with_members=False):
    d = {"id": str(pl.id), "name": pl.name, "kind": pl.kind, "object_type": pl.object_type, "filters": pl.filters,
         "owner_id": str(pl.owner_id) if pl.owner_id else None, "updated_at": pl.updated_at.isoformat()}
    ms = disc.list_members(db, pl, 2000)
    d["count"] = len(ms)
    if with_members:
        d["members"] = [{"id": str(m.id), "name": getattr(m, "full_name", None) or getattr(m, "name", None), "email": getattr(m, "email", None),  # noqa: E501
                         "title": getattr(m, "title", None), "email_status": getattr(m, "email_status", None)} for m in ms[:500]]
    return d


@router.get("/lists", tags=["lists"])
def get_lists(cu: CurrentUser = Depends(require("list.manage")), db: Session = Depends(get_db)):
    st = select(ProspectList).where(ProspectList.deleted_at.is_(None)).order_by(ProspectList.created_at.desc())
    if "crm.all" not in cu.permissions:
        st = st.where(ProspectList.owner_id == cu.id)
    return [_l_out(db, p) for p in db.scalars(st)]


@router.post("/lists", tags=["lists"])
def create_list(body: ListIn, request: Request, cu: CurrentUser = Depends(require("list.manage")), db: Session = Depends(get_db)):
    pl = ProspectList(name=body.name, kind=body.kind, object_type=body.object_type, filters=body.filters,
                      member_ids=[str(i) for i in dict.fromkeys(body.member_ids)], owner_id=cu.id, created_by=cu.id)
    db.add(pl)
    db.flush()
    out = _l_out(db, pl)
    audit(db, cu, request, "list.create", "list", pl.id, {"kind": pl.kind, "count": out["count"]})
    db.commit()
    return out


@router.get("/lists/{lid}", tags=["lists"])
def get_list(lid: uuid.UUID, cu: CurrentUser = Depends(require("list.manage")), db: Session = Depends(get_db)):
    return _l_out(db, _mine(cu, _get(db, ProspectList, lid)), True)


class MembersIn(BaseModel):
    add: list[uuid.UUID] = []
    remove: list[uuid.UUID] = []


@router.post("/lists/{lid}/members", tags=["lists"])
def edit_members(lid: uuid.UUID, body: MembersIn, request: Request, cu: CurrentUser = Depends(require("list.manage")), db: Session = Depends(get_db)):  # noqa: E501
    pl = _mine(cu, _get(db, ProspectList, lid))
    if pl.kind != "static":
        raise AppError("COMMON_INVALID_STATE", details={"list": "动态名单由条件决定成员"})
    cur = dict.fromkeys(pl.member_ids or [])
    for i in body.add:
        cur[str(i)] = None
    for i in body.remove:
        cur.pop(str(i), None)
    if len(cur) > 100000:
        raise AppError("LIST_TOO_LARGE")
    pl.member_ids = list(cur)
    audit(db, cu, request, "list.members", "list", pl.id, {"add": len(body.add), "remove": len(body.remove)})
    db.commit()
    return _l_out(db, pl)


@router.delete("/lists/{lid}", tags=["lists"])
def delete_list(lid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("list.manage")), db: Session = Depends(get_db)):
    pl = _mine(cu, _get(db, ProspectList, lid))
    pl.deleted_at = utcnow()
    audit(db, cu, request, "list.delete", "list", pl.id)
    db.commit()
    return {"ok": True}


class EnrollListIn(BaseModel):
    sequence_id: uuid.UUID


@router.post("/lists/{lid}:enroll", tags=["lists"])
def enroll_list(lid: uuid.UUID, body: EnrollListIn, request: Request, cu: CurrentUser = Depends(require("sequence.manage")),
                db: Session = Depends(get_db)):
    pl = _mine(cu, _get(db, ProspectList, lid))
    r = disc.enroll_list(db, pl, _get(db, Sequence, body.sequence_id), cu.id)
    audit(db, cu, request, "list.enroll", "list", pl.id, {"sequence": str(body.sequence_id), **r})
    db.commit()
    return r


# ---------------- 保存搜索 ----------------
class SavedIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    query: dict
    subscribed: bool = False


def _s_out(s):
    return {"id": str(s.id), "name": s.name, "query": s.query, "subscribed": s.subscribed, "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None}  # noqa: E501


@router.get("/saved-searches", tags=["discover"])
def list_saved(cu: CurrentUser = Depends(require("list.manage")), db: Session = Depends(get_db)):
    return [_s_out(s) for s in db.scalars(select(SavedSearch).where(SavedSearch.owner_id == cu.id, SavedSearch.deleted_at.is_(None)))]


@router.post("/saved-searches", tags=["discover"])
def add_saved(body: SavedIn, request: Request, cu: CurrentUser = Depends(require("list.manage")), db: Session = Depends(get_db)):
    if not (body.query.get("icp_id") or body.query.get("text")):
        raise AppError("COMMON_VALIDATION", details={"query": "需 icp_id 或 text"})
    s = SavedSearch(name=body.name, query=body.query, subscribed=body.subscribed, owner_id=cu.id, created_by=cu.id)
    db.add(s)
    db.flush()
    disc.run_saved(db, s)  # 记录当前结果作为基线，日报只推新增
    audit(db, cu, request, "saved_search.create", "saved_search", s.id, body.model_dump())
    db.commit()
    return _s_out(s)


@router.post("/saved-searches/{sid}:run", tags=["discover"])
def run_saved(sid: uuid.UUID, cu: CurrentUser = Depends(require("list.manage")), db: Session = Depends(get_db)):
    s = _get(db, SavedSearch, sid)
    if s.owner_id != cu.id:
        raise AppError("COMMON_NOT_FOUND")
    r = disc.run_saved(db, s)
    db.commit()
    return r


@router.delete("/saved-searches/{sid}", tags=["discover"])
def del_saved(sid: uuid.UUID, cu: CurrentUser = Depends(require("list.manage")), db: Session = Depends(get_db)):
    s = _get(db, SavedSearch, sid)
    if s.owner_id != cu.id:
        raise AppError("COMMON_NOT_FOUND")
    s.deleted_at = utcnow()
    db.commit()
    return {"ok": True}


# ======================= 背调补全 =======================
def _run(db, cu, request, target: dict, force=False, via=None):
    c = research.resolve(db, actor_id=cu.id, **target)
    r, cached = research.generate(db, c, user_id=cu.id, force=force)
    audit(db, cu, request, "research.run", "company", c.id, {"report": str(r.id), "cached": cached, "via": via})
    db.commit()
    return {**rep_out(db, r), "cached": cached, "via": via, "target": {k: str(v) for k, v in target.items()}}


@router.post("/research:from-inquiry/{iid}", tags=["research"])
def research_inquiry(iid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):
    inq = _get(db, Inquiry, iid)
    if inq.assignee_id not in (None, cu.id) and "crm.all" not in cu.permissions:
        raise AppError("COMMON_NOT_FOUND")
    return _run(db, cu, request, rs.target_from_inquiry(db, inq), via="inquiry")


@router.post("/research:from-card", tags=["research"])
async def research_card(request: Request, file: UploadFile = File(...), cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):  # noqa: E501
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise AppError("KB_DOC_TOO_LARGE")
    target, text = rs.target_from_card(data)
    out = _run(db, cu, request, target, via="card")
    out["ocr_text"] = text[:1000]
    return out


class RiskIn(BaseModel):
    action: str
    note: str | None = Field(None, max_length=1000)


@router.post("/research/{rid}/risks/{idx}", tags=["research"])
def risk_action(rid: uuid.UUID, idx: int, body: RiskIn, request: Request, cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):  # noqa: E501
    r = _get(db, ResearchReport, rid)
    if body.action == "blocked" and "compliance.manage" not in cu.permissions:
        raise AppError("COMMON_FORBIDDEN")
    ra = rs.act_on_risk(db, r, idx, body.action, body.note, cu.user)
    audit(db, cu, request, f"research.risk_{body.action}", "research", r.id, {"idx": idx, "note": body.note})
    db.commit()
    return ra


@router.get("/research/{rid}/pdf", tags=["research"])
def research_pdf(rid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):
    r = _get(db, ResearchReport, rid)
    data = rs.report_pdf(r, db.get(Company, r.company_id), cu.tenant.name)
    audit(db, cu, request, "research.export_pdf", "research", r.id)
    db.commit()
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="research-{r.version}.pdf"'})


class ShareIn(BaseModel):
    days: int = 7


@router.post("/research/{rid}:share", tags=["research"])
def share(rid: uuid.UUID, body: ShareIn, request: Request, cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):  # noqa: E501
    r = _get(db, ResearchReport, rid)
    tok = rs.create_share(db, r, body.days, cu.id)
    audit(db, cu, request, "research.share", "research", r.id, {"days": body.days})
    db.commit()
    return {"url": f"/share/research/{tok}", "token": tok, "days": body.days}


@router.post("/research/{rid}/shares:revoke", tags=["research"])
def revoke_shares(rid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("research.run")), db: Session = Depends(get_db)):
    n = 0
    for s in db.scalars(select(ReportShare).where(ReportShare.report_id == rid, ReportShare.revoked.is_(False))):
        s.revoked, n = True, n + 1
    audit(db, cu, request, "research.share_revoke", "research", rid, {"n": n})
    db.commit()
    return {"revoked": n}


@public.get("/research/{token}", tags=["public"])
def public_report(token: str, db: Session = Depends(get_db)):
    with bypass_tenant():
        s = rs.share_by_token(db, token)
        tid = s.tenant_id
    with tenant_context(tid):
        db.info["tenant_id"] = tid
        r = db.get(ResearchReport, s.report_id)
        c = db.get(Company, r.company_id)
        db.commit()
        # 分享页只给报告正文与证据，不含内部风险处置、点数、制裁细节
        return {"company": c.name, "domain": c.domain, "version": r.version, "sections": [x for x in r.sections if x.get("key") != "risks"],
                "evidence": r.evidence, "expires_at": s.expires_at.isoformat(), "tenant": (db.get(Tenant, tid).name if db.get(Tenant, tid) else "")}  # noqa: E501


# ======================= 触达补全 =======================
@router.get("/sequence-templates", tags=["sequences"])
def seq_templates(cu: CurrentUser = Depends(require("sequence.read"))):
    return ob.templates()


class FromTplIn(BaseModel):
    template_key: str
    name: str | None = None


@router.post("/sequences:from-template", tags=["sequences"])
def seq_from_template(body: FromTplIn, request: Request, cu: CurrentUser = Depends(require("sequence.manage")), db: Session = Depends(get_db)):  # noqa: E501
    s = ob.from_template(db, body.template_key, body.name or "", cu.id, cu.id)
    audit(db, cu, request, "sequence.from_template", "sequence", s.id, body.model_dump())
    db.commit()
    return {"id": str(s.id), "name": s.name, "steps": len(ob.PLAYBOOKS[body.template_key][2])}


@router.get("/sequences/{sid}/ab-stats", tags=["sequences"])
def ab_stats(sid: uuid.UUID, cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):
    return ob.ab_stats(db, _get(db, Sequence, sid))


@router.get("/dormant-contacts", tags=["sequences"])
def dormant(days: int = Query(180, ge=30, le=1095), cu: CurrentUser = Depends(require("sequence.manage")), db: Session = Depends(get_db)):
    cs = ob.dormant_contacts(db, days)
    return {"count": len(cs), "items": [{"id": str(c.id), "name": c.full_name, "email": c.email, "lead_status": c.lead_status} for c in cs[:200]]}  # noqa: E501


class ReviveIn(BaseModel):
    sequence_id: uuid.UUID
    days: int = Field(180, ge=30, le=1095)
    list_name: str | None = None


@router.post("/dormant-contacts:revive", tags=["sequences"])
def revive(body: ReviveIn, request: Request, cu: CurrentUser = Depends(require("sequence.manage")), db: Session = Depends(get_db)):
    cs = ob.dormant_contacts(db, body.days)
    pl = ProspectList(name=body.list_name or f"沉睡唤醒 {utcnow():%Y-%m-%d}", kind="static", member_ids=[str(c.id) for c in cs], owner_id=cu.id, created_by=cu.id)  # noqa: E501
    db.add(pl)
    db.flush()
    r = disc.enroll_list(db, pl, _get(db, Sequence, body.sequence_id), cu.id)
    audit(db, cu, request, "sequence.revive", "list", pl.id, {"dormant": len(cs), **r})
    db.commit()
    return {"list_id": str(pl.id), "dormant": len(cs), **r}


class PlanIn(BaseModel):
    main_domain: str = Field(min_length=3, max_length=253)
    daily_target: int = Field(ge=1, le=100000)


@router.post("/domains:plan", tags=["mailboxes"])
def plan(body: PlanIn, cu: CurrentUser = Depends(require("mailbox.read"))):
    return ob.domain_plan(body.main_domain, body.daily_target)


@router.post("/domains/{did}:blacklist-check", tags=["mailboxes"])
def bl_check(did: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("mailbox.manage")), db: Session = Depends(get_db)):
    d = _get(db, SendingDomain, did)
    r = ob.check_blacklists(db, d.domain)
    audit(db, cu, request, "domain.blacklist_check", "domain", d.id, {"listed": r.listed_on})
    db.commit()
    return {"domain": d.domain, "listed_on": r.listed_on, "checked": r.checked, "provider": r.provider, "paused": d.paused}


@router.get("/domains/{did}/blacklist-history", tags=["mailboxes"])
def bl_hist(did: uuid.UUID, cu: CurrentUser = Depends(require("mailbox.read")), db: Session = Depends(get_db)):
    d = _get(db, SendingDomain, did)
    return [{"at": r.created_at.isoformat(), "listed_on": r.listed_on, "provider": r.provider} for r in
            db.scalars(select(BlacklistCheck).where(BlacklistCheck.target == d.domain).order_by(BlacklistCheck.created_at.desc()).limit(30))]  # noqa: E501


@router.get("/domains/{did}/tracking-cname", tags=["mailboxes"])
def track_cname(did: uuid.UUID, cu: CurrentUser = Depends(require("mailbox.read")), db: Session = Depends(get_db)):
    from app.core.config import get_settings
    d = _get(db, SendingDomain, did)
    host = get_settings().tracking_host
    return ob.tracking_cname(d.domain, host)


@router.get("/dns-guides", tags=["mailboxes"])
def dns_guides(cu: CurrentUser = Depends(require("mailbox.read"))):
    return ob.PROVIDER_GUIDES


@router.get("/outbound/{mid}/timeline", tags=["mailboxes"])
def om_timeline(mid: uuid.UUID, cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):
    om = _get(db, OutboundMessage, mid)
    return {"to": om.to_email, "subject": om.subject, "status": om.status, "events": ob.message_timeline(db, om)}


@router.get("/outbound", tags=["mailboxes"])
def list_outbound(q: str | None = None, limit: int = Query(50, le=200), cu: CurrentUser = Depends(require("sequence.read")), db: Session = Depends(get_db)):  # noqa: E501
    st = select(OutboundMessage).order_by(OutboundMessage.created_at.desc())
    if q:
        st = st.where(OutboundMessage.to_email.ilike(f"%{q}%") | OutboundMessage.subject.ilike(f"%{q}%"))
    return [{"id": str(o.id), "to": o.to_email, "subject": o.subject, "status": o.status, "sent_at": o.sent_at.isoformat() if o.sent_at else None}  # noqa: E501
            for o in db.scalars(st.limit(limit))]


# ======================= 分析补全 =======================
def _owner(cu):
    return None if "crm.all" in cu.permissions else cu.id


@router.get("/analytics/slices", tags=["analytics"])
def slices(dim: str = Query("country", pattern="^(country|sequence|icp)$"), days: int = Query(90, le=730),
           cu: CurrentUser = Depends(require("analytics.read")), db: Session = Depends(get_db)):
    return an.funnel_slices(db, dim, *_range(days), owner_id=_owner(cu))


@router.get("/analytics/unit-economics", tags=["analytics"])
def unit_econ(days: int = Query(30, le=730), cu: CurrentUser = Depends(require("analytics.team")), db: Session = Depends(get_db)):
    from app.core.config import get_settings
    return an.unit_economics(db, *_range(days), credit_cny=get_settings().credit_price_cny)


@router.get("/analytics/team", tags=["analytics"])
def team(days: int = Query(30, le=730), cu: CurrentUser = Depends(require("analytics.team")), db: Session = Depends(get_db)):
    return an.team_performance(db, *_range(days))


@router.get("/analytics/ai-efficacy", tags=["analytics"])
def ai_eff(days: int = Query(30, le=730), cu: CurrentUser = Depends(require("analytics.team")), db: Session = Depends(get_db)):
    return an.ai_efficacy(db, *_range(days))


@router.get("/analytics/morning-brief", tags=["analytics"])
def brief(cu: CurrentUser = Depends(require("analytics.read")), db: Session = Depends(get_db)):
    return an.morning_brief(db, cu.user)


_ = datetime  # noqa
