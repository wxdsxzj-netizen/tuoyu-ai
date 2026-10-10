"""B2 知识库补全：产品库、HS 推荐、卖点、异议库、承诺边界、文档导入、成交客户导入、ICP 反查 / 复制 / 版本。"""
import csv
import io
import json
import re
import subprocess
import tempfile
import uuid
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import utcnow
from app.core.errors import AppError
from app.llm.gateway import get_gateway
from app.models import (
    Account,
    Company,
    Contact,
    DealRecord,
    DocImport,
    Icp,
    IcpVersion,
    ImportBatch,
    KbBoundary,
    KnowledgeItem,
    Objection,
    Product,
)

# ---------------- HS 编码推荐（M1-04）：规则表 + 大模型补充 ----------------
HS_TABLE = [
    (r"ball valve|球阀", "848180", "其他阀门（球阀归入 8481.80）"), (r"gate valve|闸阀", "848180", "闸阀归入 8481.80"),
    (r"check valve|止回阀|单向阀", "848130", "止回阀 8481.30"), (r"safety valve|relief valve|安全阀", "848140", "安全阀或溢流阀 8481.40"),
    (r"pressure.?reduc|减压阀", "848110", "减压阀 8481.10"), (r"valve|阀", "848180", "未列名阀门 8481.80"),
    (r"heat pump|热泵", "841861", "热泵（非空调）8418.61"), (r"air condition|空调", "841582", "空调 8415.82"),
    (r"solar panel|photovoltaic|光伏组件|太阳能板", "854143", "光伏组件 8541.43"), (r"inverter|逆变器", "850440", "静止式变流器 8504.40"),
    (r"lithium.*batter|锂电池", "850760", "锂离子蓄电池 8507.60"), (r"led.*(lamp|light)|led 灯", "853952", "LED 灯泡/灯管 8539.52"),
    (r"pump|泵", "841370", "其他离心泵 8413.70"), (r"bearing|轴承", "848210", "滚珠轴承 8482.10"),
    (r"fastener|bolt|screw|螺栓|螺丝", "731815", "螺钉螺栓 7318.15"), (r"steel pipe|钢管", "730630", "焊接钢管 7306.30"),
    (r"pipe fitting|管件", "730799", "钢铁管子附件 7307.99"), (r"cable|电缆", "854449", "其他电导体 8544.49"),
    (r"t-?shirt|t恤", "610910", "棉制针织 T 恤 6109.10"), (r"furniture|家具", "940360", "其他木家具 9403.60"),
]


def recommend_hs(text: str) -> list[dict]:
    low = (text or "").lower()
    out, seen = [], set()
    for rx, code, why in HS_TABLE:
        if re.search(rx, low) and code not in seen:
            out.append({"code": code, "reason": why, "source": "rule"})
            seen.add(code)
    try:
        res = get_gateway().chat([{"role": "system", "content": "你是报关专家。根据产品描述推荐 6 位 HS 编码 Top3，输出 JSON {\"items\":[{\"code\",\"reason\"}]}"},  # noqa: E501
                                  {"role": "user", "content": text[:2000]}], json_mode=True, temperature=0)
        for x in json.loads(res.content).get("items", []):
            c = re.sub(r"\D", "", str(x.get("code", "")))[:6]
            if len(c) == 6 and c not in seen:
                out.append({"code": c, "reason": str(x.get("reason", ""))[:200], "source": "llm"})
                seen.add(c)
    except Exception:  # noqa: BLE001
        pass
    return out[:3]


# ---------------- 卖点（M1-05）：事实型必须挂来源 ----------------
def gen_selling_points(db: Session, p: Product) -> list[dict]:
    pts = []
    for c in p.certifications or []:
        pts.append({"text": f"通过 {c} 认证", "kind": "fact", "source": "产品库·认证"})
    if p.moq:
        pts.append({"text": f"起订量低至 {p.moq}", "kind": "fact", "source": "产品库·MOQ"})
    if p.lead_time_days:
        pts.append({"text": f"交期 {p.lead_time_days} 天", "kind": "fact", "source": "产品库·交期"})
    for k, v in list((p.params or {}).items())[:2]:
        pts.append({"text": f"{k}：{v}", "kind": "fact", "source": "产品库·参数"})
    kb = db.scalars(select(KnowledgeItem).where(KnowledgeItem.status == "confirmed", KnowledgeItem.type.in_(("case", "certification")))
                    .limit(3)).all()
    for it in kb:
        pts.append({"text": it.title[:80], "kind": "fact", "source": it.source_ref or f"知识库#{it.id}"})
    pts.append({"text": f"{p.name_zh} 可按客户需求定制", "kind": "expressive", "source": None})
    return pts[:5] if len(pts) >= 3 else pts


def validate_points(pts: list[dict]) -> list[dict]:
    for x in pts:
        if x.get("kind") not in ("fact", "expressive") or not str(x.get("text", "")).strip():
            raise AppError("COMMON_VALIDATION", details={"selling_points": "kind 只能是 fact / expressive"})
        if x["kind"] == "fact" and not x.get("source"):
            raise AppError("KB_FACT_NEEDS_SOURCE", details={"text": x["text"]})
    return pts


# ---------------- 异议应答库（M1-06）：预置 40 条 ----------------
PRESET_OBJECTIONS = {
    "price": ["价格比其他供应商高", "能不能再便宜一点", "你们的价格没有竞争力", "预算有限", "别家报价低 15%", "运费太贵"],
    "moq": ["起订量太高", "能不能先下小单试试", "能否混批", "首单数量很小"],
    "lead_time": ["交期太长", "能否加急", "旺季能保证交期吗", "延期怎么赔偿"],
    "certification": ["有没有 CE 认证", "能提供检测报告吗", "有没有 UL / ISO 证书", "能否通过我们的验厂"],
    "payment": ["只接受 L/C", "能否 100% 发货后付款", "定金比例太高", "能否用 PayPal"],
    "quality": ["担心质量不稳定", "以前遇到过中国供应商质量问题", "能否先寄样", "质保期多长"],
    "trust": ["没听说过你们公司", "你们是工厂还是贸易商", "能否视频验厂", "有没有当地客户案例"],
    "logistics": ["能否做 DDP", "能否发到我们指定的货代", "包装能否定制"],
    "timing": ["现在不需要，明年再说", "已经有固定供应商", "要先和老板商量"],
    "service": ["售后怎么处理", "坏件如何赔付", "有没有本地库存", "能否提供安装指导"],
}


def seed_objections(db: Session) -> int:
    if db.scalar(select(func.count()).select_from(Objection).where(Objection.preset.is_(True))):
        return 0
    n = 0
    for cat, qs in PRESET_OBJECTIONS.items():
        for q in qs:
            db.add(Objection(category=cat, question=q, preset=True))
            n += 1
    db.flush()
    return n


def draft_answer(db: Session, o: Objection) -> str:
    kb = " ".join(i.title for i in db.scalars(select(KnowledgeItem).where(KnowledgeItem.status == "confirmed").limit(20)))
    prods = db.scalars(select(Product).where(Product.deleted_at.is_(None)).limit(5)).all()
    try:
        res = get_gateway().chat([{"role": "system", "content": "你是外贸业务经理。只依据给定资料，用英文写 2–3 句应答客户异议，不得承诺最低价或保证交期。"},  # noqa: E501
                                  {"role": "user", "content": json.dumps({"objection": o.question, "knowledge": kb[:2000],
                                                                          "products": [p.name_en or p.name_zh for p in prods]}, ensure_ascii=False)}])  # noqa: E501
        txt = res.content.strip()
    except Exception:  # noqa: BLE001
        txt = ""
    if not txt or txt.startswith("{"):
        facts = [f"MOQ {p.moq}" for p in prods if p.moq][:1] + [f"{', '.join(p.certifications)} certified" for p in prods if p.certifications][:1]  # noqa: E501
        txt = f"We understand your concern about \"{o.question}\". " + (f"For reference: {'; '.join(facts)}. " if facts else "") + \
              "Could we share more details or samples so you can evaluate?"
    return txt[:1500]


# ---------------- 承诺边界（M1-07） ----------------
def boundary(db: Session) -> KbBoundary:
    b = db.scalar(select(KbBoundary).where(KbBoundary.deleted_at.is_(None)))
    if not b:
        b = KbBoundary(can_say=[], cannot_say=[], forbidden_words=[], human_topics=["price", "payment", "contract"])
        db.add(b)
        db.flush()
    return b


def boundary_terms(db: Session) -> list[str]:
    b = db.scalar(select(KbBoundary).where(KbBoundary.deleted_at.is_(None)))
    return [x for x in ((b.cannot_say or []) + (b.forbidden_words or [])) if x] if b else []


# ---------------- 文档导入（M1-02） ----------------
KINDS = {"pdf", "docx", "pptx", "xlsx", "csv", "txt", "md", "png", "jpg", "jpeg"}
MAX_BYTES, MAX_FILES = 100 * 1024 * 1024, 50


def extract_text(name: str, data: bytes) -> tuple[str, str]:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in KINDS:
        raise AppError("KB_DOC_UNSUPPORTED", details={"file": name})
    if ext in ("png", "jpg", "jpeg"):
        return ext, ocr_image(data)
    if ext in ("txt", "md", "csv"):
        return ext, data.decode("utf-8", "ignore")
    if ext == "pdf":
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(data)
            f.flush()
            try:
                return ext, subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, timeout=60).stdout.decode("utf-8", "ignore")  # noqa: E501
            except (OSError, subprocess.TimeoutExpired):
                from pypdf import PdfReader
                return ext, "\n".join(pg.extract_text() or "" for pg in PdfReader(io.BytesIO(data)).pages)
    if ext == "docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        rows = [" | ".join(c.text for c in r.cells) for t in d.tables for r in t.rows]
        return ext, "\n".join([p.text for p in d.paragraphs] + rows)
    if ext == "pptx":
        from pptx import Presentation
        return ext, "\n".join(sh.text_frame.text for s in Presentation(io.BytesIO(data)).slides for sh in s.shapes if sh.has_text_frame)
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    return ext, "\n".join(" | ".join(str(c) for c in r if c is not None) for ws in wb.worksheets for r in ws.iter_rows(values_only=True))


SECTION_RX = [("certification", r"\b(CE|ISO\s?9001|ISO\s?14001|UL|RoHS|FDA|API\s?6D|WRAS|SGS)\b"),
              ("product", r"(?i)\b(model|型号|item no)\b"), ("case", r"(?i)(case study|案例|project|客户案例)"),
              ("faq", r"(?i)(faq|q:|问：|常见问题)")]


def import_doc(db: Session, name: str, data: bytes, actor) -> DocImport:
    if len(data) > MAX_BYTES:
        raise AppError("KB_DOC_TOO_LARGE", details={"file": name, "max_mb": 100})
    ext, text = extract_text(name, data)
    rec = DocImport(filename=name[:255], kind=ext, size_bytes=len(data), created_by=actor)
    db.add(rec)
    db.flush()
    if not text.strip():
        rec.status, rec.error = "unsupported", "图片未识别到文字（或 OCR 引擎未安装）" if ext in ("png", "jpg", "jpeg") else "未识别到文字"
        return rec
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=[A-Z一-龥].{0,40}[:：])", text) if len(p.strip()) >= 15][:200]
    n = 0
    for para in paras:
        t = next((k for k, rx in SECTION_RX if re.search(rx, para)), "company_profile")
        db.add(KnowledgeItem(type=t, title=para.split("\n")[0][:120], content=para[:4000], is_fact=True, source_ref=f"doc:{name}",
                             status="pending", data={"doc_import_id": str(rec.id)}, created_by=actor))
        n += 1
    rec.items_created = n
    db.flush()
    return rec


def ocr_image(data: bytes) -> str:
    """开源 Tesseract OCR（中文简体 + 英文）。引擎未安装时返回空串（标记 unsupported）。"""
    import shutil
    if not shutil.which("tesseract"):
        return ""
    with tempfile.NamedTemporaryFile(suffix=".img") as f:
        f.write(data)
        f.flush()
        try:
            r = subprocess.run(["tesseract", f.name, "-", "-l", "chi_sim+eng", "--psm", "3"], capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return r.stdout.decode("utf-8", "ignore") if r.returncode == 0 else ""


# ---------------- 成交客户导入（M1-09 / M1-15）：同时建公司（成交标记）、CRM 客户（customer）、成交记录 ----------------
DEAL_FIELDS = {"name": ["name", "公司", "客户名称", "company"], "country": ["country", "国家"], "website": ["website", "官网", "domain", "域名"],  # noqa: E501
               "category": ["category", "品类", "product"], "amount": ["amount", "成交额", "金额"], "currency": ["currency", "币种"],
               "deal_date": ["deal_date", "成交时间", "date", "日期"], "industry": ["industry", "行业"]}


def _map(header: list[str], mapping: dict | None) -> dict:
    if mapping:
        return mapping
    low = {h.strip().lower(): h for h in header}
    return {k: next((low[a.lower()] for a in al if a.lower() in low), None) for k, al in DEAL_FIELDS.items()}


def read_table(name: str, data: bytes) -> list[dict]:
    if name.lower().endswith(".xlsx"):
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True).worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        hdr = [str(h or "").strip() for h in rows[0]]
        return [{hdr[i]: ("" if v is None else str(v)) for i, v in enumerate(r) if i < len(hdr)} for r in rows[1:]]
    return list(csv.DictReader(io.StringIO(data.decode("utf-8-sig", "ignore"))))


def _domain(u: str | None) -> str | None:
    if not u:
        return None
    u = re.sub(r"^https?://", "", u.strip().lower()).split("/")[0]
    return u[4:] if u.startswith("www.") else (u or None)


def _country(c: str | None) -> str | None:
    if not c:
        return None
    c = c.strip()
    if len(c) == 2:
        return c.upper()
    try:
        import pycountry
        return pycountry.countries.lookup(c).alpha_2
    except LookupError:
        return {"德国": "DE", "美国": "US", "英国": "GB", "法国": "FR", "波兰": "PL", "澳大利亚": "AU", "加拿大": "CA", "意大利": "IT", "西班牙": "ES"}.get(c)  # noqa: E501


def import_deals(db: Session, name: str, data: bytes, actor, mapping: dict | None = None) -> ImportBatch:
    rows = read_table(name, data)
    if len(rows) > 100_000:
        raise AppError("COMMON_VALIDATION", details={"rows": "≤ 10 万行"})
    m = _map(list(rows[0].keys()) if rows else [], mapping)
    if not m.get("name"):
        raise AppError("COMMON_VALIDATION", details={"mapping": "缺少客户名称列"})
    batch = ImportBatch(kind="deals", created_by=actor)
    db.add(batch)
    db.flush()
    made = {"accounts": [], "companies": [], "deals": []}
    stats = {"rows": len(rows), "accounts_new": 0, "accounts_merged": 0, "deals": 0, "errors": []}
    for i, r in enumerate(rows, 2):
        g = lambda k: (r.get(m[k]) or "").strip() if m.get(k) else ""  # noqa: B023, E731
        nm = g("name")
        if not nm:
            stats["errors"].append({"row": i, "error": "缺少客户名称"})
            continue
        dom, ctry = _domain(g("website")), _country(g("country"))
        from app.data.upsert import upsert_company
        res = upsert_company(db, {"name": nm, "domain": dom, "country": ctry, "industry": g("industry") or None, "is_won_customer": True},
                             actor_id=actor, source="deal_import", force_new=True)
        comp = res.obj
        if res.action == "created":
            made["companies"].append(str(comp.id))
        comp.is_won_customer = True
        dom = comp.domain or dom
        acc = db.scalar(select(Account).where(Account.domain == dom, Account.deleted_at.is_(None))) if dom else None
        acc = acc or db.scalar(select(Account).where(func.lower(Account.name) == nm.lower(), Account.deleted_at.is_(None)))
        if acc:  # 与 CRM 判重合并（M1-15）
            stats["accounts_merged"] += 1
        else:
            acc = Account(name=nm, domain=dom, country=ctry, company_id=comp.id, owner_id=actor, source="deal_import", created_by=actor)
            db.add(acc)
            db.flush()
            made["accounts"].append(str(acc.id))
            stats["accounts_new"] += 1
        acc.stage = "customer"
        try:
            amt = int(round(float(re.sub(r"[^\d.]", "", g("amount")) or 0) * 100))
        except ValueError:
            amt = 0
        dd = None
        if g("deal_date"):
            try:
                dd = date.fromisoformat(g("deal_date")[:10])
            except ValueError:
                stats["errors"].append({"row": i, "error": "日期格式应为 YYYY-MM-DD"})
        d = DealRecord(account_id=acc.id, category=g("category") or None, amount_minor=amt, currency=(g("currency") or "USD")[:3].upper(),
                       deal_date=dd, batch_id=batch.id, created_by=actor)
        db.add(d)
        db.flush()
        made["deals"].append(str(d.id))
        stats["deals"] += 1
    batch.created_ids, batch.stats = made, stats
    db.flush()
    return batch


def undo_batch(db: Session, b: ImportBatch):
    """C-42：24 小时内整批撤销本批新建对象（被合并的已有对象不撤销；已被引用的跳过）。"""
    from app.models import Opportunity, Quote
    if b.status != "done" or b.created_at < utcnow() - timedelta(hours=24):
        raise AppError("IMPORT_UNDO_EXPIRED")
    now, skipped = utcnow(), []
    for did in b.created_ids.get("deals", []):
        d = db.get(DealRecord, uuid.UUID(did))
        if d:
            d.deleted_at = now
    for aid in b.created_ids.get("accounts", []):
        a = db.get(Account, uuid.UUID(aid))
        if not a:
            continue
        if db.scalar(select(func.count()).select_from(Opportunity).where(Opportunity.account_id == a.id)) or \
                db.scalar(select(func.count()).select_from(Quote).where(Quote.account_id == a.id)):
            skipped.append(aid)
            continue
        a.deleted_at = now
    for cid in b.created_ids.get("companies", []):
        c = db.get(Company, uuid.UUID(cid))
        if c and not db.scalar(select(func.count()).select_from(Contact).where(Contact.company_id == c.id, Contact.deleted_at.is_(None))):
            c.deleted_at = now
    b.status, b.undone_at = "undone", now
    b.stats = {**b.stats, "undo_skipped": skipped}
    return b


# ---------------- ICP 反查验收（M1-11） ----------------
def icp_reverse_check(db: Session, icp: Icp) -> dict:
    won = db.scalars(select(Company).where(Company.is_won_customer.is_(True), Company.deleted_at.is_(None))).all()
    def match(c):
        ok = True
        if icp.countries:
            ok &= (c.country or "") in icp.countries
        if icp.industries:
            ok &= any(i.lower() in (c.industry or "").lower() for i in icp.industries)
        return ok
    hit = [c for c in won if match(c)]
    ids = [c.id for c in hit]
    contacts = db.scalars(select(Contact).where(Contact.company_id.in_(ids), Contact.deleted_at.is_(None))).all() if ids else []
    with_contact = {c.company_id for c in contacts}
    roles = [r.lower() for r in icp.roles or []]
    key = {c.company_id for c in contacts if roles and any(r in (c.title or "").lower() for r in roles)}
    valid = [c for c in contacts if c.email_status == "valid"]
    pct = lambda a, b: round(a / b * 100, 1) if b else None  # noqa: E731
    return {"won_customers": len(won), "company_hits": len(hit), "company_hit_rate": pct(len(hit), len(won)),
            "with_contacts": len(with_contact), "key_person_hit_rate": pct(len(key), len(hit)) if roles else None,
            "contacts": len(contacts), "valid_email_rate": pct(len(valid), len(contacts)),
            "missed": [c.name for c in won if not match(c)][:20],
            "verdict": "通过" if won and len(hit) / len(won) >= 0.6 else "建议调整 ICP 条件", "checked_at": utcnow().isoformat()}


ICP_FIELDS = ("name", "description", "industries", "countries", "employee_ranges", "roles", "buying_traits", "exclusions", "status")


def icp_snapshot(db: Session, icp: Icp, actor=None) -> IcpVersion:
    n = (db.scalar(select(func.max(IcpVersion.version_no)).where(IcpVersion.icp_id == icp.id)) or 0) + 1
    v = IcpVersion(icp_id=icp.id, version_no=n, snapshot={k: getattr(icp, k) for k in ICP_FIELDS}, created_by=actor)
    db.add(v)
    db.flush()
    return v


def icp_copy(db: Session, icp: Icp, actor) -> Icp:
    base = f"{icp.name}（副本）"[:60]
    name, i = base, 2
    while db.scalar(select(Icp.id).where(Icp.name == name)):
        name, i = f"{base}{i}"[:64], i + 1
    c = Icp(**{k: getattr(icp, k) for k in ICP_FIELDS if k not in ("name", "status")}, name=name, status="draft", source="manual",
            owner_id=actor, created_by=actor)
    db.add(c)
    db.flush()
    return c


def icp_reach(db: Session, icp: Icp) -> int:
    q = select(func.count()).select_from(Company).where(Company.deleted_at.is_(None), Company.is_won_customer.is_(False))
    if icp.countries:
        q = q.where(Company.country.in_(icp.countries))
    return db.scalar(q) or 0
