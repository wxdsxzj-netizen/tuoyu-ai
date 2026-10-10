"""B5 问数（M11-07）、自定义报表（M11-08）、月度复盘（M11-10）、效果学习与 ICP 迭代（M12-08 / E2E-06）。
问数只把自然语言解析成白名单内的“指标 × 维度 × 时间”规格，由代码聚合，不让大模型生成 SQL；业务员只能看自己的数据。"""
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.models import (
    Company,
    Contact,
    Conversation,
    Enrollment,
    Handoff,
    Icp,
    Inquiry,
    LearningReport,
    Message,
    MessageDraft,
    Opportunity,
    OutboundMessage,
    Sequence,
    User,
)

METRICS = {"sent": "发信数", "replies": "回复数", "reply_rate": "回复率", "inquiries": "询盘数", "handoffs_accepted": "交付接受数",
           "won": "赢单数", "revenue": "赢单金额"}
DIMS = {"none": "合计", "country": "国家", "owner": "负责人", "month": "月份", "grade": "询盘等级"}
KW = [("reply_rate", r"回复率|reply rate"), ("revenue", r"金额|收入|营收|revenue|销售额"), ("won", r"赢单|成交|won|deals?"),
      ("handoffs_accepted", r"交付|handoff|合格商机"), ("inquiries", r"询盘|inquir"), ("replies", r"回复|repl"), ("sent", r"发信|发送|发了|sent|emails?")]  # noqa: E501
DKW = [("country", r"国家|country|按国|各国"), ("owner", r"负责人|业务员|销售|成员|by (rep|owner|person)|每个人"), ("month", r"每月|按月|月度|monthly|by month|趋势"),  # noqa: E501
       ("grade", r"等级|grade|分级")]


def parse_question(text: str, now: datetime | None = None) -> dict:
    now = now or utcnow()
    t = text.lower()
    metric = next((m for m, rx in KW if re.search(rx, t)), None)
    dim = next((d for d, rx in DKW if re.search(rx, t)), "none")
    country = None
    if m := re.search(r"\b(us|de|gb|uk|fr|es|it|pl|mx|br|in|jp|au|ca|nl|tr|ae|sa)\b", t):
        country = {"uk": "GB"}.get(m.group(1), m.group(1).upper())
    for zh, cc in {"美国": "US", "德国": "DE", "英国": "GB", "法国": "FR", "西班牙": "ES", "意大利": "IT", "波兰": "PL", "墨西哥": "MX", "巴西": "BR",  # noqa: E501
                   "印度": "IN", "日本": "JP", "澳大利亚": "AU", "加拿大": "CA"}.items():
        if zh in text:
            country = cc
    days = 30
    if m := re.search(r"(?:近|最近|过去|last|past)\s*(\d+)\s*(天|日|days?)", t):
        days = int(m.group(1))
    elif m := re.search(r"(?:近|最近|过去|last|past)\s*(\d+)\s*(个月|months?)", t):
        days = int(m.group(1)) * 30
    elif re.search(r"今年|this year|ytd", t):
        days = (now - now.replace(month=1, day=1)).days + 1
    elif re.search(r"上个?月|last month", t):
        first = now.replace(day=1)
        prev = (first - timedelta(days=1)).replace(day=1)
        return {"metric": metric, "dim": dim, "since": prev.isoformat(), "until": first.isoformat(), "country": country, "source": "rules"}
    elif re.search(r"本月|这个月|this month", t):
        return {"metric": metric, "dim": dim, "since": now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(),
                "until": now.isoformat(), "country": country, "source": "rules"}
    return {"metric": metric, "dim": dim, "since": (now - timedelta(days=min(days, 730))).isoformat(), "until": now.isoformat(),
            "country": country, "source": "rules"}


def parse_with_llm(text: str) -> dict:
    spec = parse_question(text)
    try:
        from app.b5.ai import fence, guarded_chat
        res = guarded_chat([{"role": "system", "content": "Map the question to JSON {metric, dim, days, country}. metric in " + ",".join(METRICS) +  # noqa: E501
                             "; dim in " + ",".join(DIMS) + "."}, {"role": "user", "content": fence(text, "user_question", 500)}], json_mode=True, temperature=0)  # noqa: E501
        d = json.loads(res.content)
        if isinstance(d, dict) and d.get("metric") in METRICS:
            spec["metric"] = d["metric"]
            if d.get("dim") in DIMS:
                spec["dim"] = d["dim"]
            spec["source"] = f"{res.provider}:{res.model}"
    except Exception:  # noqa: BLE001 mock 返回非 JSON 或不可用 → 规则结果
        pass
    return spec


def _key(dim, dt, country, owner, grade, names):
    return {"none": "合计", "country": country or "未知", "owner": names.get(str(owner), "未分配") if owner else "未分配",
            "month": dt.strftime("%Y-%m") if dt else "未知", "grade": grade or "—"}[dim]


def run(db: Session, spec: dict, owner_id=None) -> dict:
    m, dim = spec.get("metric"), spec.get("dim", "none")
    if m not in METRICS or dim not in DIMS:
        raise AppError("COMMON_VALIDATION", details={"metric": list(METRICS), "dim": list(DIMS), "hint": "例如：近 90 天按国家的回复率"})
    since, until = datetime.fromisoformat(spec["since"]), datetime.fromisoformat(spec["until"])
    if dim == "grade" and m != "inquiries":
        raise AppError("COMMON_VALIDATION", details={"dim": "询盘等级只适用于询盘数"})
    cty = spec.get("country")
    names = {str(u.id): u.name for u in db.scalars(select(User))}
    agg: dict = defaultdict(float)
    den: dict = defaultdict(float)
    LIM = 50000

    def sent_rows():
        q = (select(OutboundMessage.sent_at, Company.country, Sequence.owner_id).join(Enrollment, Enrollment.id == OutboundMessage.enrollment_id)  # noqa: E501
             .join(Sequence, Sequence.id == Enrollment.sequence_id).outerjoin(Company, Company.id == Enrollment.company_id)
             .where(OutboundMessage.sent_at.between(since, until)))
        if owner_id:
            q = q.where(Sequence.owner_id == owner_id)
        return db.execute(q.limit(LIM)).all()

    def reply_rows():
        q = (select(Message.created_at, Company.country, Sequence.owner_id).join(Conversation, Conversation.id == Message.conversation_id)
             .join(Enrollment, Enrollment.id == Conversation.enrollment_id).join(Sequence, Sequence.id == Enrollment.sequence_id)
             .outerjoin(Company, Company.id == Enrollment.company_id).where(Message.direction == "in", Message.created_at.between(since, until)))  # noqa: E501
        if owner_id:
            q = q.where(Sequence.owner_id == owner_id)
        return db.execute(q.limit(LIM)).all()

    if m in ("sent", "reply_rate"):
        for dt, c, o in sent_rows():
            if not cty or c == cty:
                (den if m == "reply_rate" else agg)[_key(dim, dt, c, o, None, names)] += 1
    if m in ("replies", "reply_rate"):
        for dt, c, o in reply_rows():
            if not cty or c == cty:
                agg[_key(dim, dt, c, o, None, names)] += 1
    if m == "inquiries":
        q = select(Inquiry.created_at, Inquiry.country, Inquiry.assignee_id, Inquiry.grade).where(Inquiry.created_at.between(since, until))
        if owner_id:
            q = q.where(Inquiry.assignee_id == owner_id)
        for dt, c, o, g in db.execute(q.limit(LIM)):
            if not cty or c == cty:
                agg[_key(dim, dt, c, o, g, names)] += 1
    if m == "handoffs_accepted":
        q = (select(Handoff.decided_at, Company.country, Handoff.assignee_id).outerjoin(Contact, Contact.id == Handoff.contact_id)
             .outerjoin(Company, Company.id == Contact.company_id).where(Handoff.status == "accepted", Handoff.decided_at.between(since, until)))  # noqa: E501
        if owner_id:
            q = q.where(Handoff.assignee_id == owner_id)
        for dt, c, o in db.execute(q.limit(LIM)):
            if not cty or c == cty:
                agg[_key(dim, dt, c, o, None, names)] += 1
    if m in ("won", "revenue"):
        from app.models import Account
        q = (select(Opportunity.closed_at, Account.country, Opportunity.owner_id, Opportunity.amount_minor).join(Account, Account.id == Opportunity.account_id)  # noqa: E501
             .where(Opportunity.stage == "won", Opportunity.closed_at.between(since, until)))
        if owner_id:
            q = q.where(Opportunity.owner_id == owner_id)
        for dt, c, o, amt in db.execute(q.limit(LIM)):
            if not cty or c == cty:
                agg[_key(dim, dt, c, o, None, names)] += 1 if m == "won" else (amt or 0) / 100
    if m == "reply_rate":
        rows = [{"key": k, "value": round(agg.get(k, 0) / v * 100, 1) if v else None, "n": int(v)} for k, v in den.items()]
    else:
        rows = [{"key": k, "value": round(v, 2)} for k, v in agg.items()]
    rows.sort(key=lambda r: (r["key"] if dim == "month" else -(r["value"] or 0)))
    unit = "%" if m == "reply_rate" else ("元/外币原值" if m == "revenue" else "")
    total = None if m == "reply_rate" else round(sum(r["value"] or 0 for r in rows), 2)
    return {"spec": spec, "title": f"{METRICS[m]}" + ("" if dim == "none" else f" · 按{DIMS[dim]}") + (f" · {cty}" if cty else ""),
            "rows": rows, "total": total, "unit": unit, "scope": "仅本人数据" if owner_id else "全公司", "as_of": utcnow().isoformat()}


# ---------------- 效果学习 ----------------
def _rate(a, b):
    return round(a / b * 100, 1) if b else None


def learn(db: Session, days: int = 90, min_n: int = 20) -> LearningReport:
    """按国家 / 职级 / 行业 / A-B 变体统计“发出 → 回复 → 交付接受 → 赢单”，样本 ≥ min_n 才给结论；输出 ICP 调整建议（需人工应用）。"""
    now = utcnow()
    since = now - timedelta(days=days)
    sent = db.execute(select(Enrollment.id, Company.country, Company.industry, Contact.seniority, MessageDraft.variant)
                      .join(OutboundMessage, OutboundMessage.enrollment_id == Enrollment.id).join(Contact, Contact.id == Enrollment.contact_id)  # noqa: E501
                      .outerjoin(Company, Company.id == Enrollment.company_id).outerjoin(MessageDraft, MessageDraft.id == OutboundMessage.draft_id)  # noqa: E501
                      .where(OutboundMessage.sent_at >= since)).all()
    replied = {r[0] for r in db.execute(select(Conversation.enrollment_id).join(Message, Message.conversation_id == Conversation.id)
                                        .where(Message.direction == "in", Conversation.enrollment_id.is_not(None), Message.created_at >= since))}  # noqa: E501
    accepted = {r[0] for r in db.execute(select(Handoff.enrollment_id).where(Handoff.status == "accepted", Handoff.enrollment_id.is_not(None)))}  # noqa: E501
    dims = {"country": 1, "industry": 2, "seniority": 3, "variant": 4}
    out, seen = {}, set()
    for name, idx in dims.items():
        g = defaultdict(lambda: [set(), set(), set()])
        for row in sent:
            k = row[idx] or "未知"
            g[k][0].add(row[0])
            if row[0] in replied:
                g[k][1].add(row[0])
            if row[0] in accepted:
                g[k][2].add(row[0])
        out[name] = sorted([{"key": k, "sent": len(v[0]), "reply_rate": _rate(len(v[1]), len(v[0])), "accept_rate": _rate(len(v[2]), len(v[0])),  # noqa: E501
                             "significant": len(v[0]) >= min_n} for k, v in g.items()], key=lambda x: -(x["reply_rate"] or 0))
        seen |= {r[0] for r in sent}
    total_sent, total_rep = len(seen), len(replied & seen)
    base = _rate(total_rep, total_sent) or 0
    sugg = []
    for name in ("country", "industry"):
        for r in out[name]:
            if not r["significant"] or r["key"] == "未知":
                continue
            if r["reply_rate"] is not None and r["reply_rate"] >= base * 1.5 and r["reply_rate"] > 0:
                sugg.append({"id": f"add:{name}:{r['key']}", "type": "icp_add", "field": "countries" if name == "country" else "industries", "value": r["key"],  # noqa: E501
                             "why": f"{r['key']} 回复率 {r['reply_rate']}%，是平均 {base}% 的 {r['reply_rate'] / base:.1f} 倍（{r['sent']} 封）" if base else ""})  # noqa: E501
            if r["reply_rate"] is not None and base and r["reply_rate"] <= base * 0.4:
                sugg.append({"id": f"drop:{name}:{r['key']}", "type": "icp_deprioritize", "field": "countries" if name == "country" else "industries",  # noqa: E501
                             "value": r["key"], "why": f"{r['key']} 回复率 {r['reply_rate']}%，远低于平均 {base}%（{r['sent']} 封）"})
    for r in out["variant"]:
        if r["significant"] and r["key"] != "未知" and base and r["reply_rate"] and r["reply_rate"] >= base * 1.3:
            sugg.append({"id": f"variant:{r['key']}", "type": "variant_winner", "value": r["key"], "why": f"变体 {r['key']} 回复率 {r['reply_rate']}%"})  # noqa: E501
    lost = defaultdict(int)
    for (lr,) in db.execute(select(Opportunity.lost_reason).where(Opportunity.stage == "lost", Opportunity.closed_at >= since)):
        lost[lr or "未填"] += 1
    rep = LearningReport(kind="learning", period=f"{since:%Y-%m-%d}~{now:%Y-%m-%d}",
                         content={"days": days, "min_n": min_n, "sent": total_sent, "reply_rate": base, "dims": out, "lost_reasons": dict(lost),  # noqa: E501
                                  "suggestions": sugg, "note": "样本不足（< min_n）的分组只展示，不下结论；建议需人工应用到 ICP。"})
    db.add(rep)
    db.flush()
    return rep


def apply_suggestion(db: Session, rep: LearningReport, sid: str, icp: Icp, user_id) -> Icp:
    s = next((x for x in rep.content.get("suggestions", []) if x["id"] == sid), None)
    if not s or s["type"] not in ("icp_add", "icp_deprioritize"):
        raise AppError("COMMON_NOT_FOUND")
    vals = list(getattr(icp, s["field"]) or [])
    if s["type"] == "icp_add" and s["value"] not in vals:
        vals.append(s["value"])
    if s["type"] == "icp_deprioritize" and s["value"] in vals:
        vals.remove(s["value"])
    setattr(icp, s["field"], vals)
    rep.applied = list(rep.applied or []) + [{"id": sid, "icp_id": str(icp.id), "by": str(user_id), "at": utcnow().isoformat()}]
    return icp


# ---------------- 月度复盘 ----------------
def monthly_review(db: Session, period: str) -> LearningReport:
    y, mo = map(int, period.split("-"))
    start = datetime(y, mo, 1, tzinfo=utcnow().tzinfo)
    end = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=start.tzinfo)
    pstart = (start - timedelta(days=1)).replace(day=1)

    def kpis(a, b):
        sp = {"since": a.isoformat(), "until": b.isoformat(), "dim": "none"}
        g = lambda m: (run(db, {**sp, "metric": m})["total"] or 0)  # noqa: E731
        return {"sent": g("sent"), "replies": g("replies"), "inquiries": g("inquiries"), "handoffs_accepted": g("handoffs_accepted"),
                "won": g("won"), "revenue": g("revenue")}
    cur, prev = kpis(start, end), kpis(pstart, start)
    delta = {k: (round((cur[k] - prev[k]) / prev[k] * 100, 1) if prev[k] else None) for k in cur}
    top_c = run(db, {"metric": "inquiries", "dim": "country", "since": start.isoformat(), "until": end.isoformat()})["rows"][:5]
    lost = defaultdict(int)
    for (lr,) in db.execute(select(Opportunity.lost_reason).where(Opportunity.stage == "lost", Opportunity.closed_at.between(start, end))):
        lost[lr or "未填"] += 1
    recs = []
    if cur["sent"] and cur["replies"] / max(cur["sent"], 1) < 0.02:
        recs.append("回复率低于 2%：检查名单质量与首封切入点，优先用回复率最高的剧本模板")
    if cur["inquiries"] and not cur["handoffs_accepted"]:
        recs.append("有询盘但没有被接受的交付包：检查询盘分配与响应时限")
    if lost:
        recs.append(f"主要输单原因：{max(lost, key=lost.get)}，建议在异议应答库补充对应话术")
    if delta.get("sent") is not None and delta["sent"] < -30:
        recs.append("发信量环比下降超过 30%：检查发件箱熔断与序列暂停")
    rep = LearningReport(kind="monthly", period=period, content={"kpis": cur, "prev": prev, "mom_pct": delta, "top_countries": top_c,
                                                                 "lost_reasons": dict(lost), "recommendations": recs or ["各项指标平稳，保持当前节奏"]})  # noqa: E501
    db.add(rep)
    db.flush()
    return rep
