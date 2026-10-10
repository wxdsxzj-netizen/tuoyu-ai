"""AI 写信（M7-03）：经统一 LLM 网关生成；上下文 = 已确认知识库 + ICP + 收件人/公司事实。
模型不可用或输出不可解析时，降级为基于知识库的规则模板写作（不编造事实）。生成后 G2 质检，不通过自动重写 ≤ 2 次，仍不通过转人工。"""
import json
import logging
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.compliance.unsubscribe import build_footer
from app.llm.gateway import LLMError, get_gateway
from app.models import Company, Contact, Icp, KnowledgeItem, Sequence, SequenceStep, TenantCompliance
from app.outreach import qa

log = logging.getLogger("writer")
SYSTEM = (
    "You are an expert B2B export sales copywriter. Write a short, plain-text cold email in {lang}. "
    "Use ONLY facts from KNOWLEDGE and RECIPIENT; never invent certifications, numbers, customers or promises. "
    "No prices, payment terms, delivery promises or warranties unless stated in KNOWLEDGE. "
    "One clear question as call-to-action. {length}. No greeting cards, no emojis, no links, at most one exclamation mark. "
    "Do NOT add signature, address or unsubscribe text (the system appends them). "
    'Return JSON: {{"subject": str (<= 60 chars), "body": str, "facts_used": [str]}}.'
)


def knowledge(db: Session) -> dict:
    items = db.scalars(select(KnowledgeItem).where(KnowledgeItem.status == "confirmed", KnowledgeItem.deleted_at.is_(None))).all()
    by = {}
    for it in items:
        by.setdefault(it.type, []).append(it)
    forbidden = [it.title for it in by.get("boundary", []) if (it.data or {}).get("kind", "cannot_say") == "cannot_say"]
    text = "\n".join(f"[{it.type}] {it.title}: {it.content}" for t, its in by.items() if t != "boundary" for it in its)
    return {"by_type": by, "forbidden": forbidden, "text": text}


def recipient_facts(contact: Contact, company: Company | None) -> list[str]:
    f = []
    if company:
        f.append(f"company: {company.name}")
        if company.industry:
            f.append(f"industry: {company.industry}")
        if company.country:
            f.append(f"country: {company.country}")
        if getattr(company, "description", None):
            f.append(f"about: {company.description[:300]}")
    if contact.title:
        f.append(f"role: {contact.title}")
    return f


def _first_name(c: Contact) -> str:
    return (c.full_name or "").split()[0] if c.full_name and c.full_name != "[已删除]" else "there"


def render_template(tpl: str, contact: Contact, company: Company | None, kb: dict, sender: str) -> str:
    my = next((i.title for i in kb["by_type"].get("company_profile", [])), "our company")
    prod = next((i.title for i in kb["by_type"].get("product", [])), "our products")
    vals = {"first_name": _first_name(contact), "full_name": contact.full_name or "", "company": company.name if company else "your company",  # noqa: E501
            "title": contact.title or "", "my_company": my, "product": prod, "sender_name": sender, "country": (company.country if company else "") or ""}  # noqa: E501
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(vals.get(m.group(1), m.group(0))), tpl or "")


def heuristic(contact: Contact, company: Company | None, kb: dict, purpose: str, sender: str) -> dict:
    """规则写作：只引用知识库原文标题，不产生新事实。"""
    my = next((i.title for i in kb["by_type"].get("company_profile", [])), "our team")
    prods = [i.title for i in kb["by_type"].get("product", [])][:2]
    point = next((i.title for i in kb["by_type"].get("selling_point", [])), None)
    certs = [i.title for i in kb["by_type"].get("certification", [])][:2]
    comp = company.name if company else "your company"
    ind = (company.industry if company and company.industry else "your industry")
    prod_txt = " and ".join(prods) if prods else "our product range"
    facts = [f"company: {comp}"] + ([f"industry: {company.industry}"] if company and company.industry else [])
    if purpose == "first":
        lines = [f"Hi {_first_name(contact)},",
                 f"I noticed {comp} works in {ind}, so I thought a short note might be useful.",
                 f"We are {my}, and we supply {prod_txt} to buyers who need reliable quality and consistent lead times.",
                 *( [f"Customers usually choose us for {point.lower()}."] if point else []),
                 *( [f"Our products are certified to {', '.join(certs)}."] if certs else []),
                 "If it helps, I can send a short catalogue with specifications matched to your current range.",
                 f"Would it make sense to compare notes on what {comp} is sourcing this quarter?",
                 f"Best regards,\n{sender}"]
        subject = f"{prod_txt[:40]} for {comp}"[:60]
    elif purpose == "breakup":
        lines = [f"Hi {_first_name(contact)},", "I have reached out a few times and do not want to clutter your inbox.",
                 f"If sourcing {prod_txt} is not a priority for {comp} right now, no problem at all, I will close my notes here.",
                 "Should I check back in a few months instead?", f"Best regards,\n{sender}"]
        subject = f"Close the loop, {comp}?"[:60]
    else:
        lines = [f"Hi {_first_name(contact)},", f"Following up on my earlier note about {prod_txt}.",
                 f"Many buyers in {ind} ask us for samples before a first trial order, and we are happy to discuss what that would look like for {comp}.",  # noqa: E501
                 "Is this something worth a short call next week?", f"Best regards,\n{sender}"]
        subject = f"Re: {prod_txt[:40]} for {comp}"[:60]
    return {"subject": subject, "body": "\n\n".join(lines), "facts_used": facts, "model": "rules"}


def _llm(contact, company, kb, step, seq, sender, feedback=None) -> dict | None:
    length = "50-150 words" if step.purpose == "first" else "30-120 words"
    msgs = [{"role": "system", "content": SYSTEM.format(lang=seq.language or "en", length=length)},
            {"role": "user", "content": json.dumps({
                "KNOWLEDGE": kb["text"][:6000], "FORBIDDEN_PHRASES": kb["forbidden"],
                "RECIPIENT": recipient_facts(contact, company), "RECIPIENT_FIRST_NAME": _first_name(contact),
                "STEP_PURPOSE": step.purpose, "INSTRUCTIONS": step.instructions or "", "SENDER": sender,
                "ICP": _icp_text(seq), "FIX_THESE_ISSUES": feedback}, ensure_ascii=False)}]
    try:
        res = get_gateway().chat(msgs, json_mode=True, temperature=0.5)
        d = json.loads(res.content)
        if isinstance(d, dict) and d.get("subject") and d.get("body"):
            return {"subject": str(d["subject"])[:256], "body": str(d["body"]), "facts_used": list(d.get("facts_used") or []),
                    "model": f"{res.provider}:{res.model}"}
    except (LLMError, ValueError, TypeError) as e:
        log.info("LLM 写作降级：%s", e)
    return None


_icp_cache: dict = {}


def _icp_text(seq) -> str:
    return _icp_cache.get(seq.id, "")


def footer_for(db: Session, unsubscribe_link: str, gdpr: bool) -> dict:
    tc = db.scalar(select(TenantCompliance).limit(1))
    return build_footer(tc, unsubscribe_link=unsubscribe_link, lang=tc.footer_language if tc else "en", gdpr=gdpr)


def write(db: Session, *, seq: Sequence, step: SequenceStep, contact: Contact, sender: str, gdpr: bool) -> dict:
    """返回 {subject, body, facts_used, personalization, qa, rewrite_count, model, status}。"""
    kb = knowledge(db)
    company = contact.company
    if seq.icp_id:
        icp = db.get(Icp, seq.icp_id)
        if icp:
            _icp_cache[seq.id] = f"{icp.name}: {icp.description}; industries={icp.industries}; roles={icp.roles}"
    footer = footer_for(db, "https://unsubscribe.example/preview", gdpr)
    has_fact = bool(company and (company.industry or company.name))
    personalization = "medium" if has_fact else "degraded"
    rewrites, feedback, draft = 0, None, None
    for attempt in range(3):
        if step.mode == "template":
            draft = {"subject": render_template(step.subject_tpl or "", contact, company, kb, sender),
                     "body": render_template(step.body_tpl or "", contact, company, kb, sender),
                     "facts_used": recipient_facts(contact, company), "model": "template"}
        else:
            draft = _llm(contact, company, kb, step, seq, sender, feedback) or heuristic(contact, company, kb, step.purpose, sender)
        res = qa.check(draft["subject"], draft["body"], kb_text=kb["text"], forbidden=kb["forbidden"], facts_used=draft["facts_used"],
                       personalization=personalization, purpose=step.purpose, footer_missing=footer["missing"])
        if res["passed"] or step.mode == "template" or footer["missing"]:
            break
        rewrites = attempt + 1
        feedback = res["vetoes"] + res["issues"]
        if attempt == 0 and draft["model"] != "rules":
            continue
        if draft["model"] == "rules":
            break
    status = "pending_approval" if res["passed"] else "needs_edit"
    return {**draft, "personalization": personalization, "qa": res, "rewrite_count": min(rewrites, 2), "status": status}
