"""工具注册中心（BR-12-03/19）：每个工具声明所属 Agent、计费动作、所需权限、最低审批要求、是否可回滚。
执行全部复用现有模块（数据中枢、联系人数据源、邮箱验证、背调、序列引擎）。"""
from sqlalchemy import select

from app.agent import credits
from app.models import Company, Contact, Enrollment, Sequence, SequenceStep

LEVELS = ["L0", "L1", "L2", "L3", "L4"]
REGISTRY = {
    "company.search": {"agent": "拓客", "action": "SEARCH", "perm": "company.read", "zh": "在数据中枢筛选目标公司", "approve_upto": None, "rollback": False},  # noqa: E501
    "contact.find": {"agent": "拓客", "action": "EMAIL_FIND", "perm": "contact.enrich", "zh": "补全关键人与邮箱", "approve_upto": "L0", "rollback": False},  # noqa: E501
    "email.verify": {"agent": "拓客", "action": "EMAIL_VERIFY", "perm": "contact.verify", "zh": "验证邮箱", "approve_upto": "L0", "rollback": False},  # noqa: E501
    "research.report": {"agent": "背调", "action": "RESEARCH", "perm": "research.run", "zh": "AI 背调重点公司", "approve_upto": "L0", "rollback": False},  # noqa: E501
    "sequence.create": {"agent": "写作", "action": "SEQUENCE", "perm": "sequence.manage", "zh": "创建开发信序列草稿（需序列审批才会发送）", "approve_upto": None, "rollback": True},  # noqa: E501
    "list.enroll": {"agent": "投递", "action": "SEQUENCE", "perm": "sequence.manage", "zh": "名单批次加入序列（G1 数据闸门）", "approve_upto": "L2", "rollback": True},  # noqa: E501
}


def needs_approval(tool: str, level: str) -> bool:
    """BR-12-04 / 8.7：对外动作（名单入序列）L0–L2 必须审批；扣费动作仅 L0 需审批（L1 起预算内自动）。"""
    up = REGISTRY[tool]["approve_upto"]
    return up is not None and LEVELS.index(level) <= LEVELS.index(up)


def _ctx(task):
    return dict(task.context or {})


def t_company_search(db, task, step, user):
    a = step["args"]
    q = select(Company).where(Company.deleted_at.is_(None), Company.sanction_status.not_in(("pending", "confirmed")),
                              Company.is_won_customer.is_(False))
    if a.get("country"):
        q = q.where(Company.country == a["country"])
    if a.get("industry"):
        kw = f"%{a['industry']}%"
        q = q.where(Company.industry.ilike(kw) | Company.description.ilike(kw) | Company.name.ilike(kw))
    rows = list(db.scalars(q.order_by(Company.contact_count.desc(), Company.created_at).limit(step["qty"])))
    credits.consume(db, "SEARCH", len(rows), task_id=task.id, held=True, actor_id=user.id)
    return {"company_ids": [str(r.id) for r in rows]}, {"found": len(rows), "wanted": step["qty"],
                                                         "note": None if len(rows) >= step["qty"] else "数据中枢内符合条件的公司不足（外部公司库未接入）"}  # noqa: E501


def t_contact_find(db, task, step, user):
    from app.core.db import utcnow
    from app.data.providers import get_provider
    from app.data.upsert import upsert_contact
    roles = step["args"].get("roles") or ["purchasing"]
    out, found, failed = [], 0, 0
    prov = get_provider()
    for cid in _ctx(task).get("company_ids", []):
        c = db.get(Company, cid)
        have = list(db.scalars(select(Contact).where(Contact.company_id == c.id, Contact.deleted_at.is_(None), Contact.email.is_not(None))))
        best = next((x for x in have if any(r.split()[0] in (x.title or "").lower() for r in roles)), have[0] if have else None)
        if best:
            out.append(str(best.id))
            continue
        if not c.domain:
            failed += 1
            continue
        try:
            people = prov.search_people(domain=c.domain, titles=roles, limit=1)
        except Exception:  # noqa: BLE001
            people = []
        p = next((p for p in people if p.email), None)
        if not p:
            failed += 1
            credits.consume(db, "EMAIL_FIND", 1, task_id=task.id, held=True, object_type="company", object_id=c.id, result="failed", actor_id=user.id)  # noqa: E501
            continue
        r = upsert_contact(db, {"full_name": p.full_name, "title": p.title, "email": p.email, "email_confidence": p.email_confidence,
                                "country": p.country or c.country, "source_fetched_at": utcnow()}, company=c, actor_id=user.id, source=prov.name)  # noqa: E501
        db.flush()
        credits.consume(db, "EMAIL_FIND", 1, task_id=task.id, held=True, object_type="contact", object_id=r.obj.id, actor_id=user.id)
        out.append(str(r.obj.id))
        found += 1
    return {"contact_ids": out}, {"contacts": len(out), "newly_found": found, "not_found": failed}


def t_email_verify(db, task, step, user):
    from app.compliance.verify import apply_to_contact, verify_email
    keep, stats = [], {}
    for cid in _ctx(task).get("contact_ids", []):
        c = db.get(Contact, cid)
        if c.email_status in ("valid", "accept_all") and c.email_verified_at:
            res_status = c.email_status
        else:
            res = verify_email(db, c.email)
            apply_to_contact(c, res)
            res_status = res.status
            credits.consume(db, "EMAIL_VERIFY", 1, task_id=task.id, held=True, object_type="contact", object_id=c.id,
                            result="failed" if res.status == "unknown" else "success", actor_id=user.id)
        stats[res_status] = stats.get(res_status, 0) + 1
        if res_status in ("valid", "accept_all"):
            keep.append(cid)
    return {"contact_ids": keep}, {"verified": stats, "kept": len(keep)}


def t_research(db, task, step, user):
    from app.research import service as rs
    ids = []
    if step["args"].get("targets"):
        comps = [rs.resolve(db, domain=d, actor_id=user.id) for d in step["args"]["targets"]]
    else:
        comps = [db.get(Company, x) for x in _ctx(task).get("company_ids", [])[: step["args"].get("top", 5)]]
    cached = insufficient = 0
    for c in comps:
        rep, hit = rs.generate(db, c, user_id=user.id, task_id=task.id, held=True)
        ids.append(str(rep.id))
        cached += hit
        insufficient += rep.status == "insufficient"
    return {"report_ids": ids}, {"reports": len(ids), "cached": cached, "insufficient": insufficient}


def t_sequence_create(db, task, step, user):
    from app.api.p3 import DEFAULT_STEPS
    lvl = "L2" if task.effective_autonomy in ("L2", "L3", "L4") else "L1"
    s = Sequence(name=f"Agent：{task.input_text[:40]}", autonomy=lvl, owner_id=user.id, created_by=user.id)
    db.add(s)
    db.flush()
    for st in DEFAULT_STEPS:
        db.add(SequenceStep(sequence_id=s.id, **st.model_dump()))
    db.flush()
    return {"sequence_id": str(s.id)}, {"sequence": s.name, "status": s.status, "note": "序列为草稿，提交并经审批后才会发信"}


def t_list_enroll(db, task, step, user):
    from app.core.errors import AppError
    from app.outreach import engine
    s = db.get(Sequence, _ctx(task)["sequence_id"])
    ok, blocked, eids, reasons = 0, 0, [], {}
    for cid in _ctx(task).get("contact_ids", []):
        c = db.get(Contact, cid)
        try:
            r = engine.enroll(db, s, c, user.id)
        except AppError as e:
            r = {"enrolled": False, "blocks": [{"code": e.code}]}
        for b in r.get("blocks") or []:
            reasons[b.get("code", "?")] = reasons.get(b.get("code", "?"), 0) + 1
        if r.get("enrolled"):
            ok += 1
            e = db.scalar(select(Enrollment).where(Enrollment.sequence_id == s.id, Enrollment.contact_id == c.id))
            if e:
                eids.append(str(e.id))
        else:
            blocked += 1
    return {"enrollment_ids": eids}, {"enrolled": ok, "blocked_by_g1": blocked, "block_reasons": reasons}


EXEC = {"company.search": t_company_search, "contact.find": t_contact_find, "email.verify": t_email_verify,
        "research.report": t_research, "sequence.create": t_sequence_create, "list.enroll": t_list_enroll}
