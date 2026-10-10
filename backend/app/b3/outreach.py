"""B3 触达：剧本模板（M7-02）、条件分支（M7-01）、A/B（M7-05）、多语种自动选择（M7-04）、序列预算（M7-11）、
复制（M7-15）、沉睡唤醒（M7-10）、转介建联系人（M7-07）、域名规划（M6-03）、DNSBL（M6-09）、追踪策略（M6-11）、
追踪域名 CNAME 与教程（M6-02）、单封投递时间线（M6-16）。"""
import hashlib
import re
import uuid
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.models import (
    BlacklistCheck,
    Company,
    Contact,
    DeliveryEvent,
    Enrollment,
    InboundMessage,
    Mailbox,
    OutboundMessage,
    Sequence,
    SequenceStep,
)


# ---------------- 剧本模板 ≥ 20 ----------------
def _steps(*spec):
    return [{"step_no": i + 1, "wait_days": w, "purpose": p, "mode": "ai", "instructions": ins} for i, (w, p, ins) in enumerate(spec)]


PLAYBOOKS = {
    "distributor_intro": ("经销商开发（3 步）", "找经销商：产品线 + 区域代理政策", _steps((0, "first", "介绍产品线与代理政策，问是否负责该品类采购"), (3, "followup", "补充一个同区域案例"), (5, "breakup", "礼貌收尾，留联系方式"))),  # noqa: E501
    "importer_price": ("进口商比价切入", "强调交期与质量稳定", _steps((0, "first", "以交期稳定切入，问当前供应商交期"), (4, "followup", "给出认证与质检流程"), (6, "breakup", "收尾"))),  # noqa: E501
    "oem_brand": ("品牌商 OEM/ODM", "OEM 能力 + 打样", _steps((0, "first", "介绍 OEM/ODM 能力与起订量"), (3, "followup", "提供免费打样政策"), (5, "followup", "分享研发能力"), (7, "breakup", "收尾"))),  # noqa: E501
    "contractor_project": ("工程商项目跟进", "项目型采购", _steps((0, "first", "问在建项目与规格需求"), (4, "followup", "提供技术资料与认证"), (7, "breakup", "收尾"))),  # noqa: E501
    "retailer_chain": ("零售连锁采购", "包装与贴牌", _steps((0, "first", "介绍零售包装与贴牌方案"), (4, "followup", "季节性备货建议"), (6, "breakup", "收尾"))),  # noqa: E501
    "ecommerce_seller": ("电商卖家（Amazon 等）", "小批量 + 快速发货", _steps((0, "first", "低起订量与 FBA 发货支持"), (3, "followup", "图片与 listing 素材支持"), (5, "breakup", "收尾"))),  # noqa: E501
    "trade_show_follow": ("展会后跟进", "展会名片跟进", _steps((0, "first", "回顾展会交流内容"), (2, "followup", "发送报价所需信息清单"), (5, "followup", "邀请视频看厂"), (8, "breakup", "收尾"))),  # noqa: E501
    "sample_follow": ("寄样后跟进", "样品反馈", _steps((0, "first", "确认样品是否收到"), (3, "followup", "询问测试结果与改进点"), (7, "followup", "下单时间与数量"))),  # noqa: E501
    "quote_follow": ("报价后跟进", "报价未回复", _steps((0, "first", "确认报价是否收到，有无疑问"), (3, "followup", "可调整的条款（起订量/付款）"), (6, "breakup", "报价有效期提醒"))),  # noqa: E501
    "reactivate_dormant": ("沉睡客户唤醒", "老客户再激活", _steps((0, "first", "分享新品或改进"), (5, "followup", "询问近期采购计划"), (9, "breakup", "收尾"))),  # noqa: E501
    "repurchase": ("复购提醒", "按采购周期", _steps((0, "first", "按上次采购周期提醒补货"), (4, "followup", "新批次排产时间"))),
    "new_product": ("新品推广", "新品上市", _steps((0, "first", "介绍新品核心卖点"), (4, "followup", "早鸟价或样品政策"), (7, "breakup", "收尾"))),  # noqa: E501
    "certification_lead": ("认证切入（CE/UL 等）", "合规卖点", _steps((0, "first", "以认证资质切入"), (4, "followup", "提供检测报告"), (6, "breakup", "收尾"))),  # noqa: E501
    "competitor_switch": ("替换现有供应商", "供应商切换", _steps((0, "first", "询问对现有供应商的痛点"), (4, "followup", "第二供应商的价值"), (7, "breakup", "收尾"))),  # noqa: E501
    "price_increase_alt": ("涨价期替代方案", "成本压力", _steps((0, "first", "原材料上涨背景下的稳定价格方案"), (4, "followup", "年度框架协议"), (7, "breakup", "收尾"))),  # noqa: E501
    "local_stock": ("海外仓 / 本地库存", "快速交付", _steps((0, "first", "介绍海外仓现货"), (3, "followup", "本地交付时效"), (6, "breakup", "收尾"))),  # noqa: E501
    "decision_maker": ("高层决策人", "简短价值主张", _steps((0, "first", "两句话价值主张 + 一个问题"), (5, "breakup", "收尾"))),
    "technical_buyer": ("技术采购 / 工程师", "参数与选型", _steps((0, "first", "选型建议与关键参数"), (3, "followup", "提供图纸与 3D 模型"), (6, "breakup", "收尾"))),  # noqa: E501
    "referral_ask": ("请求转介", "找对人", _steps((0, "first", "询问谁负责该品类采购"), (4, "breakup", "收尾"))),
    "webinar_invite": ("线上看厂邀请", "视频验厂", _steps((0, "first", "邀请 15 分钟视频看厂"), (3, "followup", "提供可选时间"), (6, "breakup", "收尾"))),  # noqa: E501
    "inquiry_no_reply": ("询盘未回复追踪", "询盘后沉默", _steps((0, "first", "确认需求细节"), (2, "followup", "给出两个可选方案"), (5, "breakup", "收尾"))),  # noqa: E501
}


def templates() -> list[dict]:
    return [{"key": k, "name": n, "description": d, "steps": len(s)} for k, (n, d, s) in PLAYBOOKS.items()]


def from_template(db: Session, key: str, name: str, owner_id, actor) -> Sequence:
    if key not in PLAYBOOKS:
        raise AppError("COMMON_NOT_FOUND")
    n, _, steps = PLAYBOOKS[key]
    seq = Sequence(name=name or n, owner_id=owner_id, status="draft", template_key=key, language="auto", created_by=actor)
    db.add(seq)
    db.flush()
    for st in steps:
        db.add(SequenceStep(sequence_id=seq.id, tenant_id=seq.tenant_id, **st))
    db.flush()
    return seq


def copy_sequence(db: Session, seq: Sequence, actor) -> Sequence:
    c = Sequence(name=f"{seq.name}（副本）"[:200], owner_id=actor, status="draft", icp_id=seq.icp_id, mailbox_ids=list(seq.mailbox_ids or []),  # noqa: E501
                 autonomy="L1" if seq.autonomy == "L2" else seq.autonomy, language=seq.language, send_limit=seq.send_limit,
                 window_start=seq.window_start, window_end=seq.window_end, daily_cap=seq.daily_cap, credit_budget=seq.credit_budget,
                 tracking=dict(seq.tracking or {}), template_key=seq.template_key, created_by=actor)
    db.add(c)
    db.flush()
    for s in db.scalars(select(SequenceStep).where(SequenceStep.sequence_id == seq.id).order_by(SequenceStep.step_no)):
        db.add(SequenceStep(sequence_id=c.id, tenant_id=c.tenant_id, step_no=s.step_no, wait_days=s.wait_days, mode=s.mode,
                            subject_tpl=s.subject_tpl, body_tpl=s.body_tpl, instructions=s.instructions, purpose=s.purpose,
                            condition=s.condition, variants=s.variants))
    db.flush()
    return c


# ---------------- 条件分支 ----------------
CONDITIONS = ("always", "country_in", "country_not_in", "seniority_in", "title_contains", "email_status_in", "no_open_reply")


def validate_condition(c: dict | None):
    if not c:
        return
    if c.get("type") not in CONDITIONS:
        raise AppError("COMMON_VALIDATION", details={"condition": f"type 只能是 {CONDITIONS}"})


def condition_ok(cond: dict | None, contact: Contact, company: Company | None) -> bool:
    if not cond or cond.get("type") in ("always", "no_open_reply", None):
        return True  # 已回复者在收信时即停止，故“未回复”恒成立
    v = [str(x).lower() for x in cond.get("values") or []]
    t = cond["type"]
    if t == "country_in":
        return (company.country or "").lower() in v if company else False
    if t == "country_not_in":
        return (company.country or "").lower() not in v if company else True
    if t == "seniority_in":
        return (contact.seniority or "").lower() in v
    if t == "title_contains":
        return any(x in (contact.title or "").lower() for x in v)
    if t == "email_status_in":
        return (contact.email_status or "").lower() in v
    return True


# ---------------- A/B 与多语种 ----------------
def pick_variant(step: SequenceStep, enrollment_id) -> dict | None:
    vs = step.variants or []
    if not vs:
        return None
    h = int(hashlib.sha256(f"{enrollment_id}:{step.step_no}".encode()).hexdigest(), 16)
    return vs[h % len(vs)]


LANG_BY_COUNTRY = {"DE": "de", "AT": "de", "CH": "de", "FR": "fr", "BE": "fr", "ES": "es", "MX": "es", "AR": "es", "CO": "es", "CL": "es", "PE": "es",  # noqa: E501
                   "IT": "it", "PT": "pt", "BR": "pt", "PL": "pl", "NL": "nl", "RU": "ru", "TR": "tr", "JP": "ja", "KR": "ko", "SA": "ar", "AE": "ar",  # noqa: E501
                   "EG": "ar", "VN": "vi", "TH": "th", "ID": "id"}


def language_for(seq: Sequence, contact: Contact, company: Company | None) -> str:
    if seq.language and seq.language != "auto":
        return seq.language
    if contact.language:
        return contact.language[:2]
    return LANG_BY_COUNTRY.get((company.country if company else "") or "", "en")


def writer_inputs(seq: Sequence, step: SequenceStep, variant: dict | None, lang: str):
    sp = SimpleNamespace(**{k: getattr(step, k) for k in ("step_no", "purpose", "mode", "subject_tpl", "body_tpl", "instructions", "version", "id")})  # noqa: E501
    if variant:
        for k in ("subject_tpl", "body_tpl", "instructions"):
            if variant.get(k):
                setattr(sp, k, variant[k])
    sq = SimpleNamespace(id=seq.id, icp_id=seq.icp_id, language=lang, autonomy=seq.autonomy, owner_id=seq.owner_id)
    return sq, sp


def ab_stats(db: Session, seq: Sequence) -> list[dict]:
    from app.models import MessageDraft
    rows = db.execute(select(MessageDraft.step_no, MessageDraft.variant, MessageDraft.enrollment_id).join(
        Enrollment, Enrollment.id == MessageDraft.enrollment_id).where(Enrollment.sequence_id == seq.id, MessageDraft.status == "sent",
                                                                       MessageDraft.variant.is_not(None))).all()
    agg = {}
    for step_no, var, eid in rows:
        a = agg.setdefault((step_no, var), {"step_no": step_no, "variant": var, "sent": 0, "replied": 0})
        a["sent"] += 1
        if db.scalar(select(func.count()).select_from(DeliveryEvent).where(DeliveryEvent.enrollment_id == eid, DeliveryEvent.type == "reply")):  # noqa: E501
            a["replied"] += 1
    out = sorted(agg.values(), key=lambda x: (x["step_no"], x["variant"]))
    for a in out:
        a["reply_rate"] = round(a["replied"] / a["sent"] * 100, 1) if a["sent"] else None
    for step_no in {a["step_no"] for a in out}:
        grp = [a for a in out if a["step_no"] == step_no]
        if len(grp) > 1 and all(a["sent"] >= 50 for a in grp):  # 样本足够才判胜
            max(grp, key=lambda a: a["reply_rate"] or 0)["winner"] = True
    return out


def sent_today(db: Session, seq: Sequence, now) -> int:
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(select(func.count()).select_from(OutboundMessage).join(Enrollment, Enrollment.id == OutboundMessage.enrollment_id)
                     .where(Enrollment.sequence_id == seq.id, OutboundMessage.sent_at >= start)) or 0


# ---------------- 沉睡唤醒 ----------------
def dormant_contacts(db: Session, days: int = 180, limit: int = 500) -> list[Contact]:
    cut = utcnow() - timedelta(days=days)
    active = select(Enrollment.contact_id).where(Enrollment.created_at >= cut)
    replied = select(InboundMessage.from_email).where(InboundMessage.received_at >= cut)
    st = select(Contact).where(Contact.deleted_at.is_(None), Contact.email_status == "valid", Contact.id.notin_(active),
                               Contact.email_normalized.notin_(replied), Contact.lead_status.in_(("replied", "no_response", "customer", "contacting")))  # noqa: E501
    return db.scalars(st.limit(limit)).all()


# ---------------- 转介：自动建联系人 ----------------
def referral_contacts(db: Session, body: str, sender: str, company_id, actor=None) -> list[str]:
    from app.data.normalize import normalize_email
    made = []
    dom = sender.split("@")[-1].lower()
    for em in set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", body or "")):
        en = normalize_email(em)
        if not en or en == sender.lower() or en.split("@")[-1] != dom:
            continue
        if db.scalar(select(Contact.id).where(Contact.email_normalized == en, Contact.deleted_at.is_(None))):
            continue
        db.add(Contact(full_name=en.split("@")[0].replace(".", " ").title(), email=en, email_normalized=en, company_id=company_id,
                       source="referral", created_by=actor))
        made.append(en)
    db.flush()
    return made


# ---------------- 域名规划 ----------------
def domain_plan(main_domain: str, daily_target: int, per_mailbox: int = 30, per_domain_mailboxes: int = 3) -> dict:
    main = main_domain.lower().strip()
    base, _, tld = main.rpartition(".")
    mailboxes = max(1, -(-daily_target // per_mailbox))
    domains = max(1, -(-mailboxes // per_domain_mailboxes))
    cands = [f"{base}-mail.{tld}", f"get{base}.{tld}", f"{base}hq.{tld}", f"{base}-global.com", f"try{base}.com", f"{base}team.com"]
    return {"main_domain": main, "advice": ["主域名只用于官网与客户往来，不做冷邮件", "每个发信域名 2–3 个邮箱，每邮箱每天 ≤ 30 封冷邮件",
                                           "新域名预热 2–4 周后再满额发送", "每个发信域名都配 SPF / DKIM / DMARC，并把根路径跳转到主站",
                                           "额外准备 1 个备用域名，主力域名被拉黑时切换"],
            "mailboxes_needed": mailboxes, "domains_needed": domains, "backup_domains": 1,
            "candidates": cands[:domains + 2], "warmup_weeks": 3}


# ---------------- DNSBL ----------------
DNSBL_ZONES = ["zen.spamhaus.org", "b.barracudacentral.org", "bl.spamcop.net", "dnsbl.sorbs.net"]
DBL_ZONES = ["dbl.spamhaus.org", "multi.surbl.org"]
_dnsbl_resolver = None


def set_dnsbl_resolver(fn):
    global _dnsbl_resolver
    _dnsbl_resolver = fn


def _listed(name: str) -> bool:
    from app.core.config import get_settings
    if _dnsbl_resolver:
        return _dnsbl_resolver(name)
    if get_settings().dnsbl_provider != "dns":
        return False  # mock：不发起外部查询
    res = get_settings().dnsbl_resolver
    try:
        if res:
            import socket

            import dns.resolver
            r = dns.resolver.Resolver(configure=False)
            r.nameservers = [socket.gethostbyname(res)]
            try:
                ans = r.resolve(name, "A", lifetime=6)
            except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
                return False
            # 127.255.255.x = 查询被拒（公共解析器 / 超限），不算列入
            return any(not a.to_text().startswith("127.255.255.") for a in ans)
        from app.outreach.dns_check import _query
        return any(not a.startswith("127.255.255.") for a in _query(name, "A"))
    except Exception:  # noqa: BLE001
        return False


def check_blacklists(db: Session, target: str) -> BlacklistCheck:
    from app.core.config import get_settings
    is_ip = bool(re.fullmatch(r"\d+\.\d+\.\d+\.\d+", target))
    zones = DNSBL_ZONES if is_ip else DBL_ZONES
    q = ".".join(reversed(target.split("."))) if is_ip else target
    listed = [z for z in zones if _listed(f"{q}.{z}")]
    r = BlacklistCheck(target=target, target_type="ip" if is_ip else "domain", listed_on=listed, checked=zones,
                       provider="injected" if _dnsbl_resolver else get_settings().dnsbl_provider)
    db.add(r)
    db.flush()
    if listed and not is_ip:  # M6-07：黑名单触发熔断——该域名下发件箱暂停冷信 + 域名暂停
        from app.models import SendingDomain
        from app.outreach import health
        for mb in db.scalars(select(Mailbox).where(Mailbox.address.ilike(f"%@{target}"), Mailbox.status != "paused")):
            health.trip(db, mb, "blacklisted", len(listed))
        d = db.scalar(select(SendingDomain).where(SendingDomain.domain == target))
        if d:
            health.pause_domain(db, d.id, "blacklisted:" + ",".join(listed), len(listed))
    return r


def blacklist_tick(db: Session) -> int:
    from app.models import SendingDomain
    n = 0
    for d in db.scalars(select(SendingDomain).where(SendingDomain.deleted_at.is_(None))):
        if check_blacklists(db, d.domain).listed_on:
            n += 1
    return n


# ---------------- 追踪策略 ----------------
GDPR = {"DE", "AT", "FR", "IT", "ES", "NL", "BE", "PL", "SE", "DK", "FI", "IE", "PT", "CZ", "GR", "HU", "RO", "BG", "HR", "SK", "SI", "LT",
        "LV", "EE", "LU", "MT", "CY", "GB", "CH", "NO"}


def tracking_for(seq: Sequence, country: str | None) -> dict:
    t = {"open": False, "click": False, **(seq.tracking or {})}
    if (country or "") in GDPR:  # 欧洲收件人默认不做打开追踪
        t["open"] = False
    return t


def tracking_cname(domain: str, tracking_host: str) -> dict:
    from app.outreach.dns_check import _query
    name = f"track.{domain}"
    try:
        got = _query(name, "CNAME")
    except Exception as e:  # noqa: BLE001
        return {"name": name, "status": "error", "detail": str(e)[:120]}
    ok = any(g.rstrip(".").lower() == tracking_host.lower() for g in got)
    return {"name": name, "expected": tracking_host, "found": got, "status": "ok" if ok else ("missing" if not got else "mismatch")}


PROVIDER_GUIDES = {
    "aliyun": ["登录阿里云 → 云解析 DNS → 选择域名 → 添加记录", "记录类型选 TXT，主机记录填 @，值粘贴 SPF", "DKIM：主机记录填 <selector>._domainkey", "DMARC：主机记录填 _dmarc"],  # noqa: E501
    "tencent": ["登录腾讯云 DNSPod → 我的域名 → 记录管理 → 添加记录", "TXT @ 填 SPF；TXT <selector>._domainkey 填 DKIM；TXT _dmarc 填 DMARC"],  # noqa: E501
    "godaddy": ["My Products → DNS → Add New Record", "Type TXT, Name @, Value = SPF", "Name <selector>._domainkey = DKIM; Name _dmarc = DMARC"],  # noqa: E501
    "cloudflare": ["Dashboard → 域名 → DNS → Records → Add record", "TXT 记录如上；追踪域名 CNAME 需关闭橙色云（仅 DNS）"],
    "namecheap": ["Domain List → Manage → Advanced DNS → Add New Record", "TXT Record，Host 填 @ / <selector>._domainkey / _dmarc"],
}


# ---------------- 单封投递时间线 ----------------
def message_timeline(db: Session, om: OutboundMessage) -> list[dict]:
    ev = [{"at": om.created_at.isoformat(), "type": "queued", "detail": {"mailbox": str(om.mailbox_id)}}]
    if om.sent_at:
        ev.append({"at": om.sent_at.isoformat(), "type": "sent", "detail": {"message_id": om.message_id}})
    if om.enrollment_id:
        for e in db.scalars(select(DeliveryEvent).where(DeliveryEvent.enrollment_id == om.enrollment_id).order_by(DeliveryEvent.occurred_at)):  # noqa: E501
            if e.type == "sent" and e.message_id != om.id:
                continue
            ev.append({"at": e.occurred_at.isoformat(), "type": e.type, "reason": e.reason_code, "detail": e.detail})
    for m in db.scalars(select(InboundMessage).where(InboundMessage.in_reply_to == om.message_id)):
        ev.append({"at": m.received_at.isoformat(), "type": f"reply:{m.category}", "detail": {"subject": m.subject}})
    seen, out = set(), []
    for x in sorted(ev, key=lambda x: x["at"]):
        k = (x["type"], x["at"])
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


def uuid_or_none(v):
    try:
        return uuid.UUID(str(v))
    except (ValueError, TypeError):
        return None
