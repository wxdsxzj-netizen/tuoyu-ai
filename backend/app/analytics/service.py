"""经营看板（M11-01 全链路漏斗、M11-04 投递健康）。口径见 PRD 16.3；分母为 0 时比率为 None（前端显示“—”）。"""
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.models import (
    BreakerEvent,
    Company,
    Contact,
    Conversation,
    Enrollment,
    Handoff,
    Inquiry,
    Mailbox,
    MailboxMetricDaily,
    Message,
    Opportunity,
    OutboundMessage,
    Quote,
    SendingDomain,
    Sequence,
)


def _n(db, q) -> int:
    return int(db.scalar(q) or 0)


def _steps(rows: list[tuple[str, str, int]]) -> list[dict]:
    out, prev, first = [], None, rows[0][2] if rows else 0
    for key, zh, v in rows:
        out.append({"key": key, "label": zh, "value": v, "rate": (round(v / prev, 4) if prev else None) if prev is not None else None,
                    "overall": round(v / first, 4) if first else None})
        prev = v
    return out


def acquisition_funnel(db: Session, since: datetime, until: datetime, owner_id=None) -> list[dict]:
    c = select(func.count()).select_from(Company).where(Company.created_at.between(since, until), Company.deleted_at.is_(None))
    ct = select(func.count()).select_from(Contact).where(Contact.deleted_at.is_(None), Contact.email.is_not(None), Contact.created_at.between(since, until))  # noqa: E501
    cv = select(func.count()).select_from(Contact).where(Contact.deleted_at.is_(None), Contact.email_status.in_(("valid", "accept_all")),
                                                         Contact.email_verified_at.between(since, until))
    en = select(func.count()).select_from(Enrollment).join(Sequence, Sequence.id == Enrollment.sequence_id).where(Enrollment.created_at.between(since, until))  # noqa: E501
    dl = select(func.count()).select_from(OutboundMessage).join(Enrollment, Enrollment.id == OutboundMessage.enrollment_id).join(
        Sequence, Sequence.id == Enrollment.sequence_id).where(OutboundMessage.kind == "cold", OutboundMessage.sent_at.between(since, until),  # noqa: E501
                                                              OutboundMessage.status.not_in(("bounced", "failed")))
    rp = select(func.count(func.distinct(Message.conversation_id))).select_from(Message).join(Conversation, Conversation.id == Message.conversation_id).join(  # noqa: E501
        Enrollment, Enrollment.id == Conversation.enrollment_id).join(Sequence, Sequence.id == Enrollment.sequence_id).where(
        Message.direction == "in", Message.created_at.between(since, until))
    seq_ho = Handoff.enrollment_id.is_not(None)  # 获客漏斗只算序列触达产生的交付包；陌生询盘计入询盘漏斗
    hq = select(func.count()).select_from(Handoff).where(seq_ho, Handoff.created_at.between(since, until))
    ha = select(func.count()).select_from(Handoff).where(seq_ho, Handoff.status == "accepted", Handoff.decided_at.between(since, until))
    qt = select(func.count()).select_from(Quote).where(Quote.created_at.between(since, until), Quote.deleted_at.is_(None))
    won = select(func.count()).select_from(Opportunity).where(Opportunity.stage == "won", Opportunity.closed_at.between(since, until))
    if owner_id:
        c = c.where(Company.owner_id == owner_id)
        ct = ct.where(Contact.created_by == owner_id)
        cv = cv.where(Contact.created_by == owner_id)
        en, dl, rp = (q.where(Sequence.owner_id == owner_id) for q in (en, dl, rp))
        hq, ha = hq.where(Handoff.assignee_id == owner_id), ha.where(Handoff.assignee_id == owner_id)
        qt = qt.where(Quote.created_by == owner_id)
        won = won.where(Opportunity.owner_id == owner_id)
    return _steps([("candidates", "候选公司", _n(db, c)), ("enriched", "补全", _n(db, ct)), ("verified", "验证有效", _n(db, cv)),
                   ("enrolled", "进入序列", _n(db, en)), ("delivered", "送达", _n(db, dl)), ("replied", "回复", _n(db, rp)),
                   ("qualified", "合格回复", _n(db, hq)), ("accepted", "交付接受", _n(db, ha)), ("quoted", "报价", _n(db, qt)),
                   ("won", "赢单", _n(db, won))])


def inquiry_funnel(db: Session, since: datetime, until: datetime, owner_id=None) -> dict:
    base = select(Inquiry).where(Inquiry.created_at.between(since, until), Inquiry.status != "merged")
    if owner_id:
        base = base.where(Inquiry.assignee_id == owner_id)
    rows = list(db.scalars(base))
    opp_ids = [r.opportunity_id for r in rows if r.opportunity_id]
    quoted = _n(db, select(func.count(func.distinct(Quote.opportunity_id))).where(Quote.opportunity_id.in_(opp_ids))) if opp_ids else 0
    won = _n(db, select(func.count()).select_from(Opportunity).where(Opportunity.id.in_(opp_ids), Opportunity.stage == "won")) if opp_ids else 0  # noqa: E501
    steps = _steps([("arrived", "询盘到达", len(rows)), ("scored", "评分", sum(r.score is not None for r in rows)),
                    ("assigned", "分配", sum(r.assigned_at is not None for r in rows)),
                    ("responded", "首次人工响应", sum(r.first_response_at is not None for r in rows)),
                    ("opportunity", "商机", len(opp_ids)), ("quoted", "报价", quoted), ("won", "赢单", won)])
    grades = {g: sum(r.grade == g for r in rows) for g in "ABCD"}
    from app.crm.inquiry import _tz, business_minutes_between
    tz = _tz(db)
    resp = [r for r in rows if r.first_response_at and r.assigned_at]
    mins = [business_minutes_between(r.assigned_at, r.first_response_at, tz) for r in resp]
    met = [m <= (r.sla_minutes or 10 ** 9) for m, r in zip(mins, resp, strict=True)]
    return {"steps": steps, "grades": grades, "avg_first_response_min": round(sum(mins) / len(mins), 1) if mins else None,
            "sla_met_rate": round(sum(met) / len(met), 4) if met else None}


def deliverability(db: Session, days: int = 30, owner_id=None) -> dict:
    today = utcnow().date()
    d0 = today - timedelta(days=days - 1)
    mbs = list(db.scalars(select(Mailbox).where(Mailbox.deleted_at.is_(None), *( [Mailbox.owner_id == owner_id] if owner_id else []))))
    ids = [m.id for m in mbs]
    mets = list(db.scalars(select(MailboxMetricDaily).where(MailboxMetricDaily.mailbox_id.in_(ids), MailboxMetricDaily.day >= d0))) if ids else []  # noqa: E501
    rate = lambda a, b: round(a / b, 4) if b else None  # noqa: E731
    per = []
    for m in mbs:
        ms = [x for x in mets if x.mailbox_id == m.id]
        sent = sum(x.sent_cold + x.sent_reply for x in ms)
        b, c, r = sum(x.hard_bounces for x in ms), sum(x.complaints for x in ms), sum(x.replies for x in ms)
        per.append({"id": str(m.id), "address": m.address, "health_score": m.health_score, "health_level": m.health_level, "status": m.status,  # noqa: E501
                    "sent": sent, "bounce_rate": rate(b, sent), "complaint_rate": rate(c, sent), "reply_rate": rate(r, sent),
                    "daily_cold_limit": m.daily_cold_limit})
    series = []
    for i in range(days):
        d: date = d0 + timedelta(days=i)
        ms = [x for x in mets if x.day == d]
        sent = sum(x.sent_cold + x.sent_reply for x in ms)
        series.append({"day": d.isoformat(), "sent": sent, "bounce_rate": rate(sum(x.hard_bounces for x in ms), sent),
                       "complaint_rate": rate(sum(x.complaints for x in ms), sent), "reply_rate": rate(sum(x.replies for x in ms), sent)})
    doms = [{"id": str(d.id), "domain": d.domain, "spf": d.spf_status, "dkim": d.dkim_status, "dmarc": d.dmarc_status, "mx": d.mx_status,
             "paused": d.paused, "checked_at": d.last_checked_at} for d in db.scalars(select(SendingDomain).where(SendingDomain.deleted_at.is_(None)))]  # noqa: E501
    evs = [{"scope": e.scope, "scope_id": str(e.scope_id), "level": e.level, "metric": e.trigger_metric, "value": e.trigger_value,
            "at": e.created_at, "recovered_at": e.recovered_at}
           for e in db.scalars(select(BreakerEvent).where(BreakerEvent.created_at >= datetime.combine(d0, datetime.min.time()).replace(
               tzinfo=utcnow().tzinfo)).order_by(BreakerEvent.created_at.desc()).limit(50))]
    tot = sum(p["sent"] for p in per)
    return {"mailboxes": per, "series": series, "domains": doms, "breaker_events": evs, "totals": {
        "sent": tot, "bounce_rate": rate(sum(x.hard_bounces for x in mets), tot), "complaint_rate": rate(sum(x.complaints for x in mets), tot),  # noqa: E501
        "reply_rate": rate(sum(x.replies for x in mets), tot)}, "inbox_placement": None}
