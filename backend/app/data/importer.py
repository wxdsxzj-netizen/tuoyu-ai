"""CSV / Excel 导入（C-40 / C-41）：字段映射 → 逐行校验 → 判重 → 写入；疑似重复默认不导入（BR-8-32）。"""
import csv
import io
from datetime import datetime

from sqlalchemy.orm import Session

from app.data.normalize import is_valid_email, normalize_country, normalize_domain
from app.data.upsert import upsert_company, upsert_contact

# 目标字段：(是否必填, 表头同义词)
FIELDS = {
    "companies": {
        "name": (True, ["name", "company", "company name", "公司", "公司名称", "企业名称", "客户名称"]),
        "domain": (False, ["domain", "website", "url", "web", "官网", "网站", "域名"]),
        "country": (False, ["country", "国家", "国家/地区", "地区"]),
        "city": (False, ["city", "城市"]),
        "address": (False, ["address", "地址"]),
        "phone": (False, ["phone", "tel", "telephone", "电话"]),
        "industry": (False, ["industry", "行业", "品类", "category"]),
        "employee_range": (False, ["employees", "employee range", "size", "规模", "员工数", "员工规模"]),
        "description": (False, ["description", "简介", "描述", "备注"]),
    },
    "contacts": {
        "full_name": (True, ["name", "full name", "contact", "contact name", "姓名", "联系人"]),
        "email": (False, ["email", "e-mail", "邮箱", "电子邮件"]),
        "title": (False, ["title", "job title", "position", "职位", "职务"]),
        "phone": (False, ["phone", "mobile", "tel", "电话", "手机"]),
        "linkedin_url": (False, ["linkedin", "linkedin url", "领英"]),
        "company_name": (False, ["company", "company name", "公司", "公司名称"]),
        "company_domain": (False, ["domain", "website", "company website", "官网", "域名"]),
        "country": (False, ["country", "国家"]),
    },
    "won_customers": {
        "name": (True, ["name", "company", "company name", "客户", "客户名称", "公司名称", "公司"]),
        "country": (True, ["country", "国家", "国家/地区"]),
        "domain": (False, ["domain", "website", "官网", "网站"]),
        "industry": (False, ["industry", "category", "品类", "行业"]),
        "employee_range": (False, ["employees", "size", "规模"]),
        "won_amount_usd": (False, ["amount", "won amount", "成交额", "成交金额", "金额(usd)", "成交额(usd)"]),
        "won_at": (False, ["won at", "date", "成交时间", "成交日期"]),
    },
}


def read_rows(path: str) -> tuple[list[str], list[list[str]]]:
    raw = open(path, "rb").read()
    if path.lower().endswith(".xlsx"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        rows = [["" if v is None else str(v) for v in r] for r in wb.active.iter_rows(values_only=True)]
    else:
        for enc in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ValueError("无法识别文件编码，请保存为 UTF-8 CSV")
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return [], []
    return [h.strip() for h in rows[0]], rows[1:]


def auto_mapping(import_type: str, headers: list[str]) -> dict[str, str]:
    """返回 {目标字段: 表头}。"""
    m = {}
    lower = {h.lower().strip(): h for h in headers}
    for f, (_, syns) in FIELDS[import_type].items():
        for s in [f, *syns]:
            if s.lower() in lower and lower[s.lower()] not in m.values():
                m[f] = lower[s.lower()]
                break
    return m


def missing_required(import_type: str, mapping: dict) -> list[str]:
    return [f for f, (req, _) in FIELDS[import_type].items() if req and f not in mapping]


def _parse_date(v: str):
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y.%m.%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(v.strip(), fmt)
        except ValueError:
            pass
    return None


def validate_row(import_type: str, rec: dict) -> tuple[dict, list[tuple[str, str, str]]]:
    """返回 (清洗后的值, [(字段, 错误码, 提示)])。"""
    errs, out = [], {}
    for f, (req, _) in FIELDS[import_type].items():
        v = (rec.get(f) or "").strip()
        if req and not v:
            errs.append((f, "COMMON_REQUIRED", "必填"))
            continue
        if not v:
            continue
        if f in ("name", "full_name", "company_name") and len(v) > 256:
            errs.append((f, "COMMON_LENGTH", "超过 256 字符"))
        elif f == "email":
            if not is_valid_email(v):
                errs.append((f, "COMMON_EMAIL_FORMAT", f"邮箱格式不正确：{v}"))
            else:
                out[f] = v.strip()
            continue
        elif f == "country":
            c = normalize_country(v)
            if not c:
                errs.append((f, "COMMON_ENUM", f"无法识别的国家/地区：{v}（请用 ISO 代码，如 DE、US）"))
            out[f] = c
            continue
        elif f in ("domain", "company_domain"):
            d = normalize_domain(v)
            if not d:
                errs.append((f, "COMMON_URL_FORMAT", f"网址格式不正确：{v}"))
            out[f] = d
            continue
        elif f == "won_amount_usd":
            try:
                n = float(v.replace(",", "").replace("$", ""))
                if n < 0:
                    raise ValueError
                out[f] = n
            except ValueError:
                errs.append((f, "COMMON_AMOUNT", f"成交额须为 ≥ 0 的数字：{v}"))
            continue
        elif f == "won_at":
            d = _parse_date(v)
            if not d:
                errs.append((f, "COMMON_DATETIME", f"日期格式不正确：{v}（如 2025-03-01）"))
            out[f] = d
            continue
        out[f] = v[:512]
    return out, errs


def run_import(db: Session, job, *, actor_id=None) -> None:
    headers, rows = read_rows(job.file_path)
    mapping = job.mapping or auto_mapping(job.type, headers)
    job.mapping = mapping
    idx = {f: headers.index(h) for f, h in mapping.items() if h in headers}
    job.total = len(rows)
    errors = []
    counts = {"created": 0, "merged": 0, "skipped": 0, "failed": 0}
    for n, row in enumerate(rows, start=2):  # 第 1 行是表头
        rec = {f: (row[i] if i < len(row) else "") for f, i in idx.items()}
        vals, errs = validate_row(job.type, rec)
        if errs:
            counts["failed"] += 1
            errors += [{"row": n, "field": f, "code": c, "message": m} for f, c, m in errs]
            continue
        sp = db.begin_nested()
        try:
            res = _write(db, job.type, vals, actor_id)
            if job.dry_run:
                sp.rollback()
            else:
                sp.commit()
        except Exception as e:  # noqa: BLE001
            sp.rollback()
            counts["failed"] += 1
            errors.append({"row": n, "field": "", "code": "COMMON_INTERNAL", "message": str(e)[:300]})
            continue
        if res.action == "suspect":
            counts["skipped"] += 1
            errors.append({"row": n, "field": "", "code": "IMPORT_SUSPECTED_DUPLICATE",
                           "message": f"疑似重复，未导入：{res.reason}"})
        else:
            counts[res.action] += 1
    for k, v in counts.items():
        setattr(job, k, v)
    job.errors = errors[:5000]
    job.status = "completed" if counts["failed"] == 0 and counts["skipped"] == 0 else "partial"


def _write(db, import_type, vals, actor_id):
    if import_type == "companies":
        return upsert_company(db, vals, actor_id=actor_id, source="import")
    if import_type == "won_customers":
        vals["is_won_customer"] = True
        return upsert_company(db, vals, actor_id=actor_id, source="import")
    company = None
    cname, cdom = vals.pop("company_name", None), vals.pop("company_domain", None)
    if cname or cdom:
        cres = upsert_company(db, {"name": cname or cdom, "domain": cdom, "country": vals.get("country")},
                              actor_id=actor_id, source="import")
        company = cres.obj if cres.action != "suspect" else cres.obj  # 疑似重复时挂到已有公司
    return upsert_contact(db, vals, company=company, actor_id=actor_id, source="import")
