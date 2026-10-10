"""交付包（M7-09/13，SM-12）——核心指标：被业务员接受 / 拒绝。"""
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.crm.notify import managers, notify, users_with
from app.llm.gateway import LLMError, get_gateway
from app.models import Account, Company, Contact, Conversation, Enrollment, Handoff, Message, Opportunity, Sequence

HANDOFF_CATEGORIES = ("positive", "inquiry", "sample", "meeting", "referral")
REJECT_REASONS = ("not_genuine", "unclear", "not_icp", "duplicate", "existing_customer", "competitor", "other")
CAT_ZH = {"positive": "积极", "inquiry": "询价", "sample": "索样", "meeting": "约会议", "referral": "转介"}
SLA_HOURS, EXPIRE_DAYS, MAX_REASSIGN = 2, 7, 2

QTY = re.compile(r"\b(\d[\d,.]*\s*(k\s*)?(pcs|pieces|units|sets|tons?|mt|kg|containers?|cartons?|pallets?|rolls?|meters?|sqm))\b", re.I)
WHEN = re.compile(r"\b(by (the )?(end of )?(january|february|march|april|may|june|july|august|september|october|november|december|q[1-4]|"
                  r"next (month|quarter|week))|within \d+ (days|weeks|months)|in \d+ (weeks|months)|next (month|quarter|season)|asap)\b", re.I)  # noqa: E501
USE = re.compile(r"\b(for (our|a|the) [\w\s-]{3,40}?(project|stores?|clients?|customers?|market|distribution|production|line|brand)|"
                 r"resell|retail|wholesale|distribut\w+|oem|private label)\b", re.I)
PRODUCT = re.compile(r"\b(model|item|sku|part)\s*(no\.?|number|#)?\s*[:#]?\s*([A-Z0-9][\w-]{1,30})", re.I)
NEED_ZH = (("product", "品类"), ("quantity", "数量"), ("use", "用途"), ("timing", "时间"))


def extract_needs(db: Session, text: str) -> dict:
    """需求要点：品类 / 数量 / 用途 / 时间（规则抽取；产品名对照知识库）。"""
    from app.outreach.writer import knowledge
    t = text or ""
    needs = {"product": None, "quantity": None, "use": None, "timing": None}
    if (m := PRODUCT.search(t)):
        needs["product"] = m.group(0).strip()
    else:
        for it in knowledge(db)["by_type"].get("product", []):
            words = [w for w in re.split(r"\W+", it.title.lower()) if len(w) > 3]
            if words and any(w in t.lower() for w in words):
                needs["product"] = it.title
                break
    for k, rx in (("quantity", QTY), ("use", USE), ("timing", WHEN)):
        if (m := rx.search(t)):
            needs[k] = m.group(0).strip()
    return needs


def _summary(conv: Conversation, msgs: list[Message], needs: dict) -> str:
    convo = "\n".join(f"{'客户' if m.direction == 'in' else '我方'}: {m.body[-800:]}" for m in msgs[-6:])
    try:
        res = get_gateway().chat([{"role": "system", "content": "用 2-3 句中文总结这段外贸邮件往来中客户的意向与下一步，只依据原文。"},
                                  {"role": "user", "content": convo}], temperature=0.2, max_tokens=300)
        if res.provider != "mock":
            return res.content.strip()
    except LLMError:
        pass
    got = [f"{zh}：{needs[k]}" for k, zh in NEED_ZH if needs.get(k)]
    miss = [zh for k, zh in NEED_ZH if not needs.get(k)]
    return (f"客户回复类别：{CAT_ZH.get(conv.last_category or '', conv.last_category)}。" + ("已明确 " + "，".join(got) + "。" if got else "")  # noqa: E501
            + ("缺失：" + "、".join(miss) + "。" if miss else ""))


def _research(db, contact: Contact | None, acc: Account | None) -> dict:
    comp = contact.company if contact else (db.get(Company, acc.company_id) if acc and acc.company_id else None)
    r = {}
    if contact:
        r["contact"] = {"name": contact.full_name, "title": contact.title, "email": contact.email, "email_status": contact.email_status}
    if comp:
        r["company"] = {"name": comp.name, "domain": comp.domain, "country": comp.country, "industry": comp.industry,
                        "sanction_status": comp.sanction_status}
    return r


def create(db: Session, conv: Conversation, e: Enrollment | None, contact: Contact | None, category: str, now=None) -> Handoff:
    now = now or utcnow()
    msgs = list(db.scalars(select(Message).where(Message.conversation_id == conv.id, Message.direction != "note").order_by(Message.created_at)))  # noqa: E501
    needs = extract_needs(db, "\n".join(m.body for m in msgs if m.direction == "in"))
    ex = db.scalar(select(Handoff).where(Handoff.conversation_id == conv.id, Handoff.status == "pending"))
    if ex:  # 同一会话已有待领取交付包：刷新需求要点，不重复生成
        ex.needs, ex.category = needs, category
        db.flush()
        return ex
    seq = db.get(Sequence, e.sequence_id) if e else None
    acc = db.get(Account, conv.account_id) if conv.account_id else None
    assignee = (acc.owner_id if acc and acc.owner_id else None) or (seq.owner_id if seq else None) or conv.assignee_id
    try:
        summary = _summary(conv, msgs, needs)
    except Exception:  # noqa: BLE001 —— 生成失败仍创建交付包
        summary = "摘要生成失败"
    try:
        from app.inbox.service import suggest_replies
        sug = suggest_replies(db, conv)["drafts"][0]["body"]
    except Exception:  # noqa: BLE001
        sug = None
    h = Handoff(conversation_id=conv.id, enrollment_id=e.id if e else None, contact_id=contact.id if contact else None,
                account_id=acc.id if acc else None, assignee_id=assignee, category=category, needs=needs, summary=summary,
                research=_research(db, contact, acc), suggested_reply=sug, sla_due_at=now + timedelta(hours=SLA_HOURS) if assignee else None)  # noqa: E501
    db.add(h)
    db.flush()
    who = contact.full_name if contact and contact.full_name else conv.peer_email
    notify(db, "N-01", [assignee] if assignee else users_with(db, "handoff.accept"), f"新交付包：{who}（{CAT_ZH.get(category, category)}）",
           summary, object_type="handoff", object_id=h.id, action_url="/handoffs", level="warn")
    return h


def _can_act(user, h: Handoff) -> bool:
    return h.assignee_id is None or h.assignee_id == user.id or "crm.all" in user.role.permission_codes


def accept(db: Session, h: Handoff, user) -> Opportunity:
    """接受：建 / 更新客户 + 商机 + 判定 is_qualified（BR-7-11），一次事务；行锁防重复领取。"""
    h = db.scalar(select(Handoff).where(Handoff.id == h.id).with_for_update())
    if h.status != "pending":
        raise AppError("COMMON_ALREADY_TAKEN" if h.status == "accepted" else "COMMON_INVALID_STATE")
    if not _can_act(user, h):
        raise AppError("COMMON_ALREADY_TAKEN")
    conv = db.get(Conversation, h.conversation_id)
    contact = db.get(Contact, h.contact_id) if h.contact_id else None
    from app.inbox.service import ensure_account
    acc = db.get(Account, h.account_id) if h.account_id else ensure_account(db, contact, conv.peer_email, user.id)
    if acc is None:
        acc = Account(name=conv.peer_email or "未知客户", owner_id=user.id, source="inquiry")
        db.add(acc)
        db.flush()
    if acc.owner_id is None or acc.pool_status == "public":
        acc.owner_id, acc.pool_status, acc.protected_until = user.id, "private", utcnow() + timedelta(days=90)
    if acc.stage == "lead":
        acc.stage = "prospect"
    acc.last_valid_followup_at = utcnow()
    n = h.needs or {}
    qualified = sum(1 for v in n.values() if v) >= 2
    opp = Opportunity(account_id=acc.id, contact_id=h.contact_id, owner_id=user.id, handoff_id=h.id, source="ai_sdr",
                      name=f"{acc.name} - {n.get('product') or CAT_ZH.get(h.category or '', '商机')}"[:256],
                      products=[{"name": n.get("product"), "quantity": n.get("quantity")}] if n.get("product") else [],
                      is_qualified=qualified, qualified_by="ai" if qualified else None)
    db.add(opp)
    db.flush()
    h.status, h.decided_by, h.decided_at, h.opportunity_id, h.account_id, h.assignee_id = "accepted", user.id, utcnow(), opp.id, acc.id, user.id  # noqa: E501
    conv.account_id, conv.assignee_id = acc.id, user.id
    if contact and not contact.account_id:
        contact.account_id = acc.id
    db.flush()
    return opp


def reject(db: Session, h: Handoff, user, reason: str | None, note: str | None) -> None:
    if reason not in REJECT_REASONS or (reason == "other" and not (note or "").strip()):
        raise AppError("HANDOFF_REJECT_REASON_REQUIRED")
    h = db.scalar(select(Handoff).where(Handoff.id == h.id).with_for_update())
    if h.status != "pending":
        raise AppError("COMMON_INVALID_STATE")
    if not _can_act(user, h):
        raise AppError("COMMON_FORBIDDEN")
    h.status, h.reject_reason, h.reject_note, h.decided_by, h.decided_at = "rejected", reason, note, user.id, utcnow()
    db.flush()


def _next_assignee(db, h: Handoff):
    cands = [u for u in users_with(db, "handoff.accept") if u.id != h.assignee_id]
    if not cands:
        return None
    load = dict(db.execute(select(Handoff.assignee_id, func.count()).where(Handoff.status == "pending").group_by(Handoff.assignee_id)).all())  # noqa: E501
    # 优先改派给业务员（无 crm.all 的成员），其次按负载、轮转
    return min(cands, key=lambda u: ("crm.all" in u.role.permission_codes, load.get(u.id, 0), str(u.id)))


def sla_tick(db: Session, now=None) -> dict:
    """BR-7-05：2 小时未领取 → 提醒经理并改派；改派 2 次后进入共享池；7 天未处理自动失效（N-30）。"""
    now = now or utcnow()
    out = {"reassigned": 0, "pooled": 0, "expired": 0}
    for h in list(db.scalars(select(Handoff).where(Handoff.status == "pending"))):
        if h.created_at <= now - timedelta(days=EXPIRE_DAYS):
            h.status = "expired"
            notify(db, "N-30", managers(db), "交付包 7 天未处理已失效", h.summary or "", object_type="handoff", object_id=h.id, action_url="/handoffs")  # noqa: E501
            out["expired"] += 1
        elif h.sla_due_at and h.sla_due_at <= now and h.assignee_id:
            old = h.assignee_id
            if h.reassign_count >= MAX_REASSIGN:
                h.assignee_id, h.sla_due_at = None, None
                out["pooled"] += 1
                notify(db, "N-17", [old, *managers(db)], "交付包多次未领取，已进入团队共享池", h.summary or "", object_type="handoff",
                       object_id=h.id, action_url="/handoffs", level="warn")
                continue
            nxt = _next_assignee(db, h)
            h.reassign_count += 1
            h.assignee_id = nxt.id if nxt else None
            h.sla_due_at = now + timedelta(hours=SLA_HOURS) if nxt else None
            out["reassigned"] += 1
            notify(db, "N-17", [old, *managers(db)], "交付包 2 小时未领取，已改派", h.summary or "", object_type="handoff", object_id=h.id,
                   action_url="/handoffs", level="warn")
            if nxt:
                notify(db, "N-01", [nxt], "新交付包（改派）", h.summary or "", object_type="handoff", object_id=h.id, action_url="/handoffs", level="warn")  # noqa: E501
    db.flush()
    return out


def stats(db: Session, since=None, user_id=None) -> dict:
    """核心指标：接受 / 拒绝 / 接受率 / 合格商机数 / 拒绝原因分布。"""
    q = select(Handoff.status, func.count()).group_by(Handoff.status)
    if since:
        q = q.where(Handoff.created_at >= since)
    if user_id:
        q = q.where(Handoff.decided_by == user_id)
    by = dict(db.execute(q).all())
    decided = by.get("accepted", 0) + by.get("rejected", 0)
    oq = select(func.count()).select_from(Opportunity).where(Opportunity.is_qualified.is_(True), Opportunity.handoff_id.is_not(None))
    if since:
        oq = oq.where(Opportunity.created_at >= since)
    rq = select(Handoff.reject_reason, func.count()).where(Handoff.status == "rejected").group_by(Handoff.reject_reason)
    if since:
        rq = rq.where(Handoff.created_at >= since)
    return {"accepted": by.get("accepted", 0), "rejected": by.get("rejected", 0), "pending": by.get("pending", 0),
            "expired": by.get("expired", 0), "acceptance_rate": round(by.get("accepted", 0) / decided, 3) if decided else None,
            "qualified_opportunities": db.scalar(oq) or 0, "reject_reasons": dict(db.execute(rq).all())}
