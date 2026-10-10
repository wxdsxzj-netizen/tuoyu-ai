"""B2：知识库补全（产品库 / HS / 卖点 / 异议 / 承诺边界 / 文档导入 / 成交客户导入 / ICP 反查、复制、版本）
与 CRM 补全（分级规则 / 合并拆分 / 自定义字段 / 阶段配置 / 解锁申请 / 样品 / 订单回款 / 汇率 / 价格体系 / 客户导入撤销 / 会议 / 时间轴）
以及旧审批并入统一审批中心（批量处理）。"""
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.db import get_db
from app.core.deps import CurrentUser, get_current_user, require
from app.core.errors import AppError
from app.crm import b2 as crm2
from app.crm import service as crm
from app.kb2 import service as kb
from app.models import (
    Account,
    AccountMerge,
    DealRecord,
    DocImport,
    FieldDef,
    FxRate,
    Icp,
    IcpVersion,
    ImportBatch,
    Meeting,
    Objection,
    Opportunity,
    Product,
    ProductSku,
    Quote,
    SalesOrder,
    Sample,
)
from app.org import collab

router = APIRouter(prefix="/api/v1")


def audit(db, cu, request, action, otype=None, oid=None, after=None):
    write_audit(db, tenant_id=cu.tenant_id, action=action, actor_id=cu.id, object_type=otype, object_id=oid, after=after, request=request)


def _get(db, m, i):
    o = db.get(m, i)
    if not o or getattr(o, "deleted_at", None):
        raise AppError("COMMON_NOT_FOUND")
    return o


def _all(cu):
    return "crm.all" in cu.permissions


def _acc(db, aid, cu, write=False) -> Account:
    a = _get(db, Account, aid)
    if _all(cu) or a.owner_id == cu.id or (a.pool_status == "public" and not write):
        return a
    raise AppError("COMMON_NOT_FOUND")


def _cols(o, *names):
    out = {}
    for n in names:
        v = getattr(o, n)
        out[n] = str(v) if isinstance(v, uuid.UUID) else (v.isoformat() if isinstance(v, (date, datetime)) else v)
    return out


# ======================= 产品库 =======================
P_COLS = ("id", "name_zh", "name_en", "model", "category", "hs_code", "hs_candidates", "params", "certifications", "moq", "lead_time_days",
          "price_min", "price_max", "currency", "industries", "images", "selling_points", "translations", "description", "status", "updated_at")  # noqa: E501


class ProductIn(BaseModel):
    name_zh: str = Field(min_length=1, max_length=200)
    name_en: str | None = Field(None, max_length=200)
    model: str | None = Field(None, max_length=64)
    category: str | None = None
    hs_code: str | None = Field(None, pattern=r"^\d{6,10}$")
    params: dict = {}
    certifications: list[str] = []
    moq: int | None = Field(None, ge=0)
    lead_time_days: str | None = None
    price_min: float | None = None
    price_max: float | None = None
    currency: str = "USD"
    industries: list[str] = []
    images: list[str] = Field([], max_length=20)
    description: str | None = None
    translations: dict = {}
    status: str = Field("active", pattern="^(active|disabled)$")


def _p_out(p, cu):
    d = _cols(p, *P_COLS)
    d["sku_count"] = None
    return d


@router.get("/products", tags=["products"])
def list_products(q: str | None = None, cu: CurrentUser = Depends(require("product.read")), db: Session = Depends(get_db)):
    st = select(Product).where(Product.deleted_at.is_(None)).order_by(Product.created_at.desc())
    if q:
        like = f"%{q}%"
        st = st.where(Product.name_zh.ilike(like) | Product.name_en.ilike(like) | Product.model.ilike(like))
    return [_p_out(p, cu) for p in db.scalars(st.limit(500))]


@router.post("/products", tags=["products"])
def create_product(body: ProductIn, request: Request, cu: CurrentUser = Depends(require("product.manage")), db: Session = Depends(get_db)):
    if body.price_min and body.price_max and body.price_min > body.price_max:
        raise AppError("COMMON_VALIDATION", details={"price": "最低价不能高于最高价"})
    p = Product(**body.model_dump(), created_by=cu.id)
    db.add(p)
    db.flush()
    p.hs_candidates = kb.recommend_hs(" ".join(filter(None, [p.name_en, p.name_zh, p.category, p.description])))
    if not p.hs_code and p.hs_candidates:
        p.hs_code = None  # 推荐只作候选，需人工确认
    p.selling_points = kb.gen_selling_points(db, p)
    audit(db, cu, request, "product.create", "product", p.id, {"name": p.name_zh})
    db.commit()
    return _p_out(p, cu)


@router.get("/products/{pid}", tags=["products"])
def get_product(pid: uuid.UUID, cu: CurrentUser = Depends(require("product.read")), db: Session = Depends(get_db)):
    p = _get(db, Product, pid)
    d = _p_out(p, cu)
    d["skus"] = [crm2.sku_out(s, "price.floor" in cu.permissions) for s in db.scalars(select(ProductSku).where(ProductSku.product_id == p.id))]  # noqa: E501
    return d


@router.put("/products/{pid}", tags=["products"])
def update_product(pid: uuid.UUID, body: ProductIn, request: Request, cu: CurrentUser = Depends(require("product.manage")),
                   db: Session = Depends(get_db)):
    p = _get(db, Product, pid)
    old = {k: getattr(p, k) for k in ("hs_code", "moq", "price_min", "price_max")}
    for k, v in body.model_dump().items():
        if k == "translations":  # 人工锁定的译文不被覆盖
            cur = p.translations or {}
            v = {lang: (cur[lang] if cur.get(lang, {}).get("locked") else val) for lang, val in {**cur, **v}.items()}
        setattr(p, k, v)
    p.hs_candidates = kb.recommend_hs(" ".join(filter(None, [p.name_en, p.name_zh, p.category, p.description])))
    audit(db, cu, request, "product.update", "product", p.id, {"before": old})
    db.commit()
    return _p_out(p, cu)


@router.delete("/products/{pid}", tags=["products"])
def delete_product(pid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("product.manage")), db: Session = Depends(get_db)):
    from app.core.db import utcnow
    p = _get(db, Product, pid)
    p.deleted_at = utcnow()
    audit(db, cu, request, "product.delete", "product", p.id)
    db.commit()
    return {"ok": True}


class HsIn(BaseModel):
    text: str = Field(min_length=2, max_length=2000)


@router.post("/products:hs-recommend", tags=["products"])
def hs_recommend(body: HsIn, cu: CurrentUser = Depends(require("product.read"))):
    return {"items": kb.recommend_hs(body.text), "note": "仅供参考，以报关行确认为准"}


class PointsIn(BaseModel):
    selling_points: list[dict] = Field(max_length=10)


@router.put("/products/{pid}/selling-points", tags=["products"])
def set_points(pid: uuid.UUID, body: PointsIn, request: Request, cu: CurrentUser = Depends(require("product.manage")),
               db: Session = Depends(get_db)):
    p = _get(db, Product, pid)
    p.selling_points = kb.validate_points(body.selling_points)
    audit(db, cu, request, "product.selling_points", "product", p.id, {"n": len(p.selling_points)})
    db.commit()
    return p.selling_points


@router.post("/products/{pid}/selling-points:generate", tags=["products"])
def regen_points(pid: uuid.UUID, cu: CurrentUser = Depends(require("product.manage")), db: Session = Depends(get_db)):
    return kb.gen_selling_points(db, _get(db, Product, pid))


class SkuIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    spec: dict = {}
    unit: str = "pcs"
    cost_price: float | None = Field(None, ge=0)
    factory_price: float | None = Field(None, ge=0)
    floor_price: float | None = Field(None, ge=0)
    tier_prices: list[dict] = []
    customer_prices: dict = {}
    currency: str = "USD"


@router.post("/products/{pid}/skus", tags=["products"])
def add_sku(pid: uuid.UUID, body: SkuIn, request: Request, cu: CurrentUser = Depends(require("product.manage")), db: Session = Depends(get_db)):  # noqa: E501
    p = _get(db, Product, pid)
    if (body.cost_price is not None or body.floor_price is not None or body.customer_prices) and "price.floor" not in cu.permissions:
        raise AppError("COMMON_FORBIDDEN", details={"field": "cost/floor"})
    for t in body.tier_prices:
        if "min_qty" not in t or "price" not in t:
            raise AppError("COMMON_VALIDATION", details={"tier_prices": "需 min_qty 与 price"})
    s = ProductSku(product_id=p.id, **body.model_dump(), created_by=cu.id)
    db.add(s)
    audit(db, cu, request, "product.sku_add", "product", p.id, {"sku": body.sku})
    db.commit()
    return crm2.sku_out(s, "price.floor" in cu.permissions)


@router.get("/skus/{sid}/price", tags=["products"])
def sku_price(sid: uuid.UUID, qty: float = 1, account_id: uuid.UUID | None = None, cu: CurrentUser = Depends(require("product.read")),
              db: Session = Depends(get_db)):
    s = _get(db, ProductSku, sid)
    r = crm2.price_for(s, qty, account_id)
    if "price.floor" in cu.permissions:
        r["floor_price"] = s.floor_price
        r["below_floor"] = bool(s.floor_price and r["price"] is not None and r["price"] < s.floor_price)
    return r


# ======================= 异议库 / 承诺边界 =======================
@router.get("/objections", tags=["kb"])
def list_objections(cu: CurrentUser = Depends(require("product.read")), db: Session = Depends(get_db)):
    if kb.seed_objections(db):
        db.commit()
    return [_cols(o, "id", "category", "question", "answer", "status", "preset") for o in
            db.scalars(select(Objection).where(Objection.deleted_at.is_(None)).order_by(Objection.category, Objection.created_at))]


class ObjIn(BaseModel):
    category: str = Field(min_length=1, max_length=32)
    question: str = Field(min_length=2, max_length=300)
    answer: str | None = Field(None, max_length=3000)
    status: str = Field("draft", pattern="^(draft|confirmed)$")


@router.post("/objections", tags=["kb"])
def add_objection(body: ObjIn, request: Request, cu: CurrentUser = Depends(require("product.manage")), db: Session = Depends(get_db)):
    o = Objection(**body.model_dump(), created_by=cu.id)
    db.add(o)
    db.flush()
    audit(db, cu, request, "objection.create", "objection", o.id)
    db.commit()
    return _cols(o, "id", "category", "question", "answer", "status", "preset")


@router.put("/objections/{oid}", tags=["kb"])
def edit_objection(oid: uuid.UUID, body: ObjIn, request: Request, cu: CurrentUser = Depends(require("product.manage")),
                   db: Session = Depends(get_db)):
    o = _get(db, Objection, oid)
    if body.status == "confirmed" and not (body.answer or "").strip():
        raise AppError("COMMON_VALIDATION", details={"answer": "确认前需填写应答"})
    for k, v in body.model_dump().items():
        setattr(o, k, v)
    audit(db, cu, request, "objection.update", "objection", o.id, {"status": o.status})
    db.commit()
    return _cols(o, "id", "category", "question", "answer", "status", "preset")


@router.post("/objections/{oid}:draft", tags=["kb"])
def draft_objection(oid: uuid.UUID, cu: CurrentUser = Depends(require("product.manage")), db: Session = Depends(get_db)):
    o = _get(db, Objection, oid)
    o.answer, o.status = kb.draft_answer(db, o), "draft"
    db.commit()
    return _cols(o, "id", "category", "question", "answer", "status", "preset")


B_COLS = ("can_say", "cannot_say", "forbidden_words", "human_topics", "public_price_range", "lead_time_range")


class BoundaryIn(BaseModel):
    can_say: list[str] = Field([], max_length=100)
    cannot_say: list[str] = Field([], max_length=100)
    forbidden_words: list[str] = Field([], max_length=200)
    human_topics: list[str] = []
    public_price_range: bool = False
    lead_time_range: str | None = None


@router.get("/kb/boundary", tags=["kb"])
def get_boundary(cu: CurrentUser = Depends(require("product.read")), db: Session = Depends(get_db)):
    b = kb.boundary(db)
    db.commit()
    return _cols(b, *B_COLS)


@router.put("/kb/boundary", tags=["kb"])
def put_boundary(body: BoundaryIn, request: Request, cu: CurrentUser = Depends(require("product.manage")), db: Session = Depends(get_db)):
    b = kb.boundary(db)
    before = _cols(b, *B_COLS)
    for k, v in body.model_dump().items():
        setattr(b, k, [x.strip() for x in v if x.strip()] if isinstance(v, list) else v)
    audit(db, cu, request, "kb.boundary_update", "kb_boundary", b.id, {"before": before, "after": _cols(b, *B_COLS)})
    db.commit()
    return _cols(b, *B_COLS)


# ======================= 文档导入 / 成交客户导入 =======================
@router.post("/kb/documents", tags=["kb"])
async def upload_docs(request: Request, files: list[UploadFile] = File(...), cu: CurrentUser = Depends(require("kb.manage")),
                      db: Session = Depends(get_db)):
    if len(files) > kb.MAX_FILES:
        raise AppError("COMMON_VALIDATION", details={"files": "单次最多 50 个"})
    out = []
    for f in files:
        data = await f.read()
        try:
            r = kb.import_doc(db, f.filename or "file", data, cu.id)
            out.append(_cols(r, "id", "filename", "kind", "status", "items_created", "error"))
        except AppError as e:
            out.append({"filename": f.filename, "status": "failed", "error": e.code})
    audit(db, cu, request, "kb.documents_import", "doc_import", None, {"files": [o["filename"] for o in out]})
    db.commit()
    return out


@router.get("/kb/documents", tags=["kb"])
def list_docs(cu: CurrentUser = Depends(require("kb.read")), db: Session = Depends(get_db)):
    return [_cols(r, "id", "filename", "kind", "size_bytes", "status", "items_created", "error", "created_at")
            for r in db.scalars(select(DocImport).order_by(DocImport.created_at.desc()).limit(200))]


def _batch_out(b):
    return _cols(b, "id", "kind", "stats", "status", "created_at", "undone_at")


@router.post("/kb/deals:import", tags=["kb"])
async def import_deals(request: Request, file: UploadFile = File(...), cu: CurrentUser = Depends(require("kb.manage")),
                       db: Session = Depends(get_db)):
    b = kb.import_deals(db, file.filename or "deals.csv", await file.read(), cu.id)
    audit(db, cu, request, "kb.deals_import", "import_batch", b.id, b.stats)
    db.commit()
    return _batch_out(b)


@router.get("/import-batches", tags=["crm"])
def list_batches(cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    st = select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(100)
    if not _all(cu):
        st = st.where(ImportBatch.created_by == cu.id)
    return [_batch_out(b) for b in db.scalars(st)]


@router.post("/import-batches/{bid}:undo", tags=["crm"])
def undo_batch(bid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    b = _get(db, ImportBatch, bid)
    if b.created_by != cu.id and "crm.admin" not in cu.permissions:
        raise AppError("COMMON_FORBIDDEN")
    (kb.undo_batch if b.kind == "deals" else crm2.undo_accounts)(db, b)
    audit(db, cu, request, "import.undo", "import_batch", b.id, b.stats)
    db.commit()
    return _batch_out(b)


@router.get("/accounts/{aid}/deals", tags=["crm"])
def account_deals(aid: uuid.UUID, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    _acc(db, aid, cu)
    return [_cols(d, "id", "category", "amount_minor", "currency", "deal_date") for d in
            db.scalars(select(DealRecord).where(DealRecord.account_id == aid, DealRecord.deleted_at.is_(None)).order_by(DealRecord.deal_date))]  # noqa: E501


# ======================= ICP：反查 / 复制 / 版本 =======================
@router.post("/icps/{iid}:reverse-check", tags=["kb"])
def reverse_check(iid: uuid.UUID, cu: CurrentUser = Depends(require("icp.read")), db: Session = Depends(get_db)):
    i = _get(db, Icp, iid)
    r = kb.icp_reverse_check(db, i)
    r["reach_estimate"] = kb.icp_reach(db, i)
    return r


@router.post("/icps/{iid}:copy", tags=["kb"])
def copy_icp(iid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("icp.manage")), db: Session = Depends(get_db)):
    c = kb.icp_copy(db, _get(db, Icp, iid), cu.id)
    audit(db, cu, request, "icp.copy", "icp", c.id, {"from": str(iid)})
    db.commit()
    return {"id": str(c.id), "name": c.name}


@router.post("/icps/{iid}/versions", tags=["kb"])
def snapshot_icp(iid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("icp.manage")), db: Session = Depends(get_db)):
    v = kb.icp_snapshot(db, _get(db, Icp, iid), cu.id)
    audit(db, cu, request, "icp.snapshot", "icp", iid, {"version": v.version_no})
    db.commit()
    return {"version_no": v.version_no}


@router.get("/icps/{iid}/versions", tags=["kb"])
def icp_versions(iid: uuid.UUID, cu: CurrentUser = Depends(require("icp.read")), db: Session = Depends(get_db)):
    return [_cols(v, "id", "version_no", "snapshot", "created_at") for v in
            db.scalars(select(IcpVersion).where(IcpVersion.icp_id == iid).order_by(IcpVersion.version_no.desc()))]


@router.post("/icps/{iid}/versions/{no}:restore", tags=["kb"])
def restore_icp(iid: uuid.UUID, no: int, request: Request, cu: CurrentUser = Depends(require("icp.manage")), db: Session = Depends(get_db)):
    i = _get(db, Icp, iid)
    v = db.scalar(select(IcpVersion).where(IcpVersion.icp_id == iid, IcpVersion.version_no == no))
    if not v:
        raise AppError("COMMON_NOT_FOUND")
    kb.icp_snapshot(db, i, cu.id)  # 还原前先存当前版本
    for k, val in v.snapshot.items():
        if k != "name":
            setattr(i, k, val)
    audit(db, cu, request, "icp.restore", "icp", iid, {"version": no})
    db.commit()
    return {"ok": True}


# ======================= CRM 设置：分级 / 阶段 / 自定义字段 =======================
class CrmSettingsIn(BaseModel):
    grade_rules: dict | None = None
    stages: dict | None = None
    repurchase_days: int | None = Field(None, ge=7, le=730)


@router.get("/crm/settings", tags=["crm"])
def get_crm_settings(cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    s = crm2.settings(db)
    db.commit()
    return {"grade_rules": s.grade_rules, "stages": {k: {**crm2.DEFAULT_STAGES[k], **(s.stages or {}).get(k, {})} for k in crm2.STAGE_KEYS},
            "repurchase_days": s.repurchase_days}


@router.put("/crm/settings", tags=["crm"])
def put_crm_settings(body: CrmSettingsIn, request: Request, cu: CurrentUser = Depends(require("crm.admin")), db: Session = Depends(get_db)):
    s = crm2.settings(db)
    if body.grade_rules is not None:
        if set(body.grade_rules) - {"A", "B", "C", "D"}:
            raise AppError("COMMON_VALIDATION", details={"grade_rules": "只能 A/B/C/D"})
        s.grade_rules = body.grade_rules
    if body.stages is not None:
        crm2.set_stages(db, body.stages)
    if body.repurchase_days:
        s.repurchase_days = body.repurchase_days
    audit(db, cu, request, "crm.settings_update", "crm_settings", s.id, body.model_dump(exclude_none=True))
    db.commit()
    return get_crm_settings(cu, db)


@router.post("/crm/accounts:regrade", tags=["crm"])
def regrade(request: Request, cu: CurrentUser = Depends(require("crm.admin")), db: Session = Depends(get_db)):
    r = crm2.regrade_all(db)
    audit(db, cu, request, "crm.regrade", "account", None, r)
    db.commit()
    return r


class FieldIn(BaseModel):
    object_type: str
    key: str
    label: str = Field(min_length=1, max_length=64)
    field_type: str = "text"
    options: list[str] = []
    required: bool = False
    visible_roles: list[str] = []


@router.get("/crm/fields", tags=["crm"])
def list_fields(object_type: str, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    return [_cols(f, "id", "object_type", "key", "label", "field_type", "options", "required", "visible_roles")
            for f in crm2.fields(db, object_type, None if "crm.admin" in cu.permissions else cu.user.role.code)]


@router.post("/crm/fields", tags=["crm"])
def add_field(body: FieldIn, request: Request, cu: CurrentUser = Depends(require("crm.admin")), db: Session = Depends(get_db)):
    f = crm2.add_field(db, body.object_type, body.key, body.label, body.field_type, body.options, body.required, body.visible_roles, cu.id)
    audit(db, cu, request, "crm.field_add", "field_def", f.id, body.model_dump())
    db.commit()
    return _cols(f, "id", "object_type", "key", "label", "field_type", "options", "required", "visible_roles")


@router.delete("/crm/fields/{fid}", tags=["crm"])
def del_field(fid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("crm.admin")), db: Session = Depends(get_db)):
    from app.core.db import utcnow
    f = _get(db, FieldDef, fid)
    f.deleted_at, f.key = utcnow(), f"{f.key}__del{uuid.uuid4().hex[:6]}"[:40]
    audit(db, cu, request, "crm.field_delete", "field_def", f.id)
    db.commit()
    return {"ok": True}


class CustomIn(BaseModel):
    values: dict


@router.put("/accounts/{aid}/custom", tags=["crm"])
def set_acc_custom(aid: uuid.UUID, body: CustomIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):  # noqa: E501
    a = _acc(db, aid, cu, write=True)
    a.custom = crm2.validate_custom(db, "account", body.values, a.custom, cu.user.role.code)
    audit(db, cu, request, "account.custom_update", "account", a.id, body.values)
    db.commit()
    return crm2.visible_custom(db, "account", a.custom, cu.user.role.code)


@router.put("/opportunities/{oid}/custom", tags=["crm"])
def set_opp_custom(oid: uuid.UUID, body: CustomIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):  # noqa: E501
    o = _get(db, Opportunity, oid)
    if not _all(cu) and o.owner_id != cu.id:
        raise AppError("COMMON_NOT_FOUND")
    o.custom = crm2.validate_custom(db, "opportunity", body.values, o.custom, cu.user.role.code)
    audit(db, cu, request, "opportunity.custom_update", "opportunity", o.id, body.values)
    db.commit()
    return crm2.visible_custom(db, "opportunity", o.custom, cu.user.role.code)


# ======================= 合并 / 拆分 =======================
class MergeIn(BaseModel):
    master_id: uuid.UUID
    merged_id: uuid.UUID


@router.post("/accounts:merge", tags=["crm"])
def merge_accounts(body: MergeIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    """crm.admin 直接合并；其他人提交合并审批（统一审批中心）。"""
    m, o = _acc(db, body.master_id, cu, write=True), _acc(db, body.merged_id, cu)
    if "crm.admin" not in cu.permissions:
        r = collab.request_approval(db, "account_merge", object_type="account", object_id=m.id, title=f"合并客户：{o.name} → {m.name}",
                                    snapshot={"master_id": str(m.id), "merged_id": str(o.id)}, requester=cu.id)
        audit(db, cu, request, "account.merge_requested", "account", m.id, {"merged": str(o.id)})
        db.commit()
        return {"status": "pending_approval", "approval_id": str(r.id)}
    mg = crm2.merge(db, m, o, cu.id)
    audit(db, cu, request, "account.merge", "account", m.id, {"merged": str(o.id), "moved": {k: len(v) for k, v in mg.moved.items() if k != "_fields"}})  # noqa: E501
    db.commit()
    return {"status": "merged", "merge_id": str(mg.id)}


def _exec_merge(db, r, u):
    m, o = db.get(Account, uuid.UUID(r.snapshot["master_id"])), db.get(Account, uuid.UUID(r.snapshot["merged_id"]))
    mg = crm2.merge(db, m, o, u.id if u else None)
    write_audit(db, tenant_id=m.tenant_id, action="account.merge", actor_id=u.id if u else None, object_type="account", object_id=m.id,
                after={"merged": str(o.id), "approval": str(r.id), "merge_id": str(mg.id)})


collab.EXECUTORS["account_merge"] = _exec_merge


@router.get("/accounts/{aid}/merges", tags=["crm"])
def list_merges(aid: uuid.UUID, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    _acc(db, aid, cu)
    return [_cols(m, "id", "master_id", "merged_id", "status", "created_at") | {"moved": {k: len(v) for k, v in m.moved.items() if k != "_fields"}}  # noqa: E501
            for m in db.scalars(select(AccountMerge).where(AccountMerge.master_id == aid).order_by(AccountMerge.created_at.desc()))]


@router.post("/account-merges/{mid}:split", tags=["crm"])
def split_merge(mid: uuid.UUID, request: Request, cu: CurrentUser = Depends(require("crm.admin")), db: Session = Depends(get_db)):
    mg = _get(db, AccountMerge, mid)
    crm2.split(db, mg)
    audit(db, cu, request, "account.split", "account", mg.master_id, {"restored": str(mg.merged_id)})
    db.commit()
    return {"status": mg.status}


# ======================= 客户导入（字段映射） =======================
@router.post("/accounts:import", tags=["crm"])
async def import_accounts(request: Request, file: UploadFile = File(...), mapping: str | None = Form(None),
                          cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):
    import json
    rows = kb.read_table(file.filename or "a.csv", await file.read())
    if not rows:
        raise AppError("IMPORT_NO_ROWS")
    b = crm2.import_accounts(db, rows, json.loads(mapping) if mapping else None, cu.id)
    audit(db, cu, request, "account.import", "import_batch", b.id, b.stats)
    db.commit()
    return _batch_out(b)


# ======================= 解锁申请 =======================
class UnlockIn(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


@router.post("/quotes/{qid}:request-unlock", tags=["crm"])
def request_unlock(qid: uuid.UUID, body: UnlockIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):  # noqa: E501
    from app.models import ApprovalRequest
    q = _get(db, Quote, qid)
    _acc(db, q.account_id, cu, write=True)
    if q.status not in ("locked", "confirmed", "converted"):
        raise AppError("COMMON_INVALID_STATE")
    if db.scalar(select(ApprovalRequest.id).where(ApprovalRequest.approval_type == "quote_unlock", ApprovalRequest.object_id == str(q.id),
                                                  ApprovalRequest.status.in_(("pending", "escalated")))):
        raise AppError("CRM_UNLOCK_PENDING")
    r = collab.request_approval(db, "quote_unlock", object_type="quote", object_id=q.id, title=f"解锁报价 {q.quote_no} v{q.doc_version}",
                                snapshot={"reason": body.reason, "status": q.status}, requester=cu.id)
    audit(db, cu, request, "quote.unlock_requested", "quote", q.id, {"reason": body.reason})
    db.commit()
    return {"approval_id": str(r.id)}


def _exec_unlock(db, r, u):
    q = db.get(Quote, uuid.UUID(r.object_id))
    if q.status == "converted":
        q.status = "locked"
    nq = crm.new_version(db, q)
    nq.notes = f"解锁修改：{r.snapshot.get('reason')}"
    write_audit(db, tenant_id=q.tenant_id, action="quote.unlocked", actor_id=u.id if u else None, object_type="quote", object_id=q.id,
                after={"new_version": nq.doc_version, "new_id": str(nq.id)})


collab.EXECUTORS["quote_unlock"] = _exec_unlock


# ======================= 汇率 =======================
@router.get("/fx-rates", tags=["crm"])
def fx_rates(cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    from app.core.db import bypass_tenant
    with bypass_tenant():
        rows = db.scalars(select(FxRate).order_by(FxRate.rate_date.desc(), FxRate.currency).limit(60)).all()
    latest = {}
    for r in rows:
        latest.setdefault(r.currency, {"currency": r.currency, "cny_per_unit": r.cny_per_unit, "date": r.rate_date.isoformat(), "source": r.source})  # noqa: E501
    return list(latest.values())


@router.post("/fx-rates:refresh", tags=["crm"])
def fx_refresh(request: Request, cu: CurrentUser = Depends(require("crm.admin")), db: Session = Depends(get_db)):
    n = crm2.refresh_fx(db)
    audit(db, cu, request, "fx.refresh", "fx_rate", None, {"n": n})
    db.commit()
    return {"updated": n}


# ======================= 样品 =======================
S_COLS = ("id", "account_id", "opportunity_id", "items", "fee_type", "fee_minor", "courier", "tracking_no", "status", "feedback", "shipped_at",  # noqa: E501
          "received_at", "order_id", "created_at")


class SampleIn(BaseModel):
    account_id: uuid.UUID
    opportunity_id: uuid.UUID | None = None
    items: list[dict] = Field(min_length=1)
    fee_type: str = Field("free", pattern="^(free|paid|collect)$")
    fee_minor: int = Field(0, ge=0)


@router.get("/samples", tags=["orders"])
def list_samples(account_id: uuid.UUID | None = None, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    st = select(Sample).where(Sample.deleted_at.is_(None)).order_by(Sample.created_at.desc())
    if account_id:
        _acc(db, account_id, cu)
        st = st.where(Sample.account_id == account_id)
    elif not _all(cu):
        st = st.where(Sample.created_by == cu.id)
    return [_cols(s, *S_COLS) for s in db.scalars(st.limit(200))]


@router.post("/samples", tags=["orders"])
def add_sample(body: SampleIn, request: Request, cu: CurrentUser = Depends(require("order.manage")), db: Session = Depends(get_db)):
    a = _acc(db, body.account_id, cu, write=True)
    crm2.check_party(db, a)
    s = Sample(**body.model_dump(), created_by=cu.id)
    db.add(s)
    db.flush()
    audit(db, cu, request, "sample.create", "sample", s.id)
    db.commit()
    return _cols(s, *S_COLS)


class SampleMove(BaseModel):
    to: str
    courier: str | None = None
    tracking_no: str | None = None
    feedback: str | None = None


@router.post("/samples/{sid}:move", tags=["orders"])
def move_sample(sid: uuid.UUID, body: SampleMove, request: Request, cu: CurrentUser = Depends(require("order.manage")), db: Session = Depends(get_db)):  # noqa: E501
    s = _get(db, Sample, sid)
    _acc(db, s.account_id, cu, write=True)
    crm2.sample_move(s, body.to, body.model_dump())
    if body.to == "received":
        from datetime import timedelta

        from app.core.db import utcnow
        from app.org.collab import add_task
        add_task(db, title="样品已签收：跟进客户反馈", assignee_id=s.created_by, task_type="follow_up", due_at=utcnow() + timedelta(days=7),
                 object_type="account", object_id=s.account_id, source="sample")
    audit(db, cu, request, f"sample.{body.to}", "sample", s.id, body.model_dump(exclude_none=True))
    db.commit()
    return _cols(s, *S_COLS)


# ======================= 订单与回款 =======================
O_COLS = ("id", "order_no", "account_id", "opportunity_id", "quote_id", "currency", "total_minor", "status", "milestones", "payment_plan",
          "payments", "paid_minor", "shipped_at", "created_at")


def _o_out(o):
    d = _cols(o, *O_COLS)
    d["overdue"] = crm2.overdue_payments(o)
    return d


class OrderIn(BaseModel):
    account_id: uuid.UUID
    quote_id: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None
    currency: str = "USD"
    total_minor: int | None = Field(None, gt=0)
    payment_plan: list[dict] = []
    milestones: list[dict] = []


@router.get("/orders", tags=["orders"])
def list_orders(account_id: uuid.UUID | None = None, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    st = select(SalesOrder).where(SalesOrder.deleted_at.is_(None)).order_by(SalesOrder.created_at.desc())
    if account_id:
        _acc(db, account_id, cu)
        st = st.where(SalesOrder.account_id == account_id)
    elif not _all(cu):
        st = st.where(SalesOrder.created_by == cu.id)
    return [_o_out(o) for o in db.scalars(st.limit(200))]


@router.post("/orders", tags=["orders"])
def create_order(body: OrderIn, request: Request, cu: CurrentUser = Depends(require("order.manage")), db: Session = Depends(get_db)):
    a = _acc(db, body.account_id, cu, write=True)
    q = _get(db, Quote, body.quote_id) if body.quote_id else None
    if q and q.account_id != a.id:
        raise AppError("COMMON_VALIDATION")
    total = body.total_minor or (q.total_minor if q else None)
    if not total:
        raise AppError("COMMON_VALIDATION", details={"total_minor": "必填"})
    o = crm2.create_order(db, a, quote=q, total_minor=total, currency=q.currency if q else body.currency, plan=body.payment_plan,
                          milestones=body.milestones, actor=cu.id, opportunity_id=body.opportunity_id)
    audit(db, cu, request, "order.create", "order", o.id, {"order_no": o.order_no, "total_minor": total})
    db.commit()
    return _o_out(o)


class OrderMove(BaseModel):
    status: str = Field(pattern="^(producing|shipped|completed|cancelled)$")


@router.post("/orders/{oid}:status", tags=["orders"])
def order_status(oid: uuid.UUID, body: OrderMove, request: Request, cu: CurrentUser = Depends(require("order.manage")), db: Session = Depends(get_db)):  # noqa: E501
    from app.core.db import utcnow
    o = _get(db, SalesOrder, oid)
    _acc(db, o.account_id, cu, write=True)
    if o.status in ("completed", "cancelled"):
        raise AppError("COMMON_INVALID_STATE")
    if body.status == "completed" and o.paid_minor < o.total_minor:
        raise AppError("COMMON_INVALID_STATE", details={"paid": "未收齐款项不能完结"})
    o.status = body.status
    if body.status == "shipped":
        o.shipped_at = utcnow()
    audit(db, cu, request, "order.status", "order", o.id, {"status": body.status})
    db.commit()
    return _o_out(o)


class PayIn(BaseModel):
    amount_minor: int = Field(gt=0)
    ref: str | None = Field(None, max_length=128)


@router.post("/orders/{oid}/payments", tags=["orders"])
def add_payment(oid: uuid.UUID, body: PayIn, request: Request, cu: CurrentUser = Depends(require("order.manage")), db: Session = Depends(get_db)):  # noqa: E501
    o = _get(db, SalesOrder, oid)
    _acc(db, o.account_id, cu, write=True)
    crm2.add_payment(o, body.amount_minor, body.ref)
    audit(db, cu, request, "order.payment", "order", o.id, body.model_dump())
    db.commit()
    return _o_out(o)


# ======================= 会议 / 时间轴 =======================
class MeetingIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    start_at: datetime
    end_at: datetime
    attendees: list[str] = []
    minutes: str | None = None


@router.post("/accounts/{aid}/meetings", tags=["crm"])
def add_meeting(aid: uuid.UUID, body: MeetingIn, request: Request, cu: CurrentUser = Depends(require("crm.manage")), db: Session = Depends(get_db)):  # noqa: E501
    a = _acc(db, aid, cu, write=True)
    if body.end_at <= body.start_at:
        raise AppError("COMMON_VALIDATION", details={"end_at": "结束时间须晚于开始"})
    m = Meeting(account_id=a.id, **body.model_dump(), created_by=cu.id)
    db.add(m)
    a.last_valid_followup_at = body.start_at
    db.flush()
    audit(db, cu, request, "meeting.create", "meeting", m.id)
    db.commit()
    return _cols(m, "id", "title", "start_at", "end_at", "attendees", "minutes")


@router.get("/accounts/{aid}/timeline", tags=["crm"])
def account_timeline(aid: uuid.UUID, cu: CurrentUser = Depends(require("crm.read")), db: Session = Depends(get_db)):
    a = _acc(db, aid, cu)
    return crm2.timeline(db, a, "price.floor" in cu.permissions)


# ======================= 旧审批并入审批中心：批量处理 =======================
class LegacyBatchIn(BaseModel):
    type: str = Field(pattern="^(content|quote|agent|inquiry_review)$")
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)
    approve: bool
    comment: str | None = None
    confirm_send: bool = False


@router.post("/approvals-center/legacy:batch-decide", tags=["approvals"])
def legacy_batch(body: LegacyBatchIn, request: Request, cu: CurrentUser = Depends(get_current_user),
                 db: Session = Depends(get_db)):
    from app.agent import runner as agent
    from app.crm import inquiry as inq
    from app.models import AgentTask, Inquiry, MessageDraft
    from app.org.legacy_approvals import gate
    if body.approve and collab.APPROVAL_TYPES[body.type][2] and not body.confirm_send:
        raise AppError("COMMON_VALIDATION", details={"confirm_send": "发送类批量通过需二次确认"})
    res = {}
    for i in body.ids:
        try:
            with db.begin_nested():
                if body.type == "content":
                    d = _get(db, MessageDraft, i)
                    gate(db, "content", cu.user)
                    if d.status != "pending_approval":
                        raise AppError("COMMON_INVALID_STATE")
                    if body.approve:
                        from app.outreach import qa, writer
                        kbx = writer.knowledge(db)
                        r = qa.check(d.subject, d.body, kb_text=kbx["text"], forbidden=kbx["forbidden"], facts_used=d.facts_used or [],
                                     personalization=d.personalization, purpose="first", footer_missing=[])
                        if r["vetoes"]:
                            raise AppError("SEQ_CONTENT_QA_FAILED")
                    d.status, d.reviewed_by, d.review_note = ("approved" if body.approve else "rejected"), cu.id, body.comment
                    res[str(i)] = d.status
                elif body.type == "quote":
                    q = _get(db, Quote, i)
                    crm.review(db, q, cu.user, body.approve, body.comment)
                    res[str(i)] = q.status
                elif body.type == "agent":
                    t = _get(db, AgentTask, i)
                    gate(db, "agent", cu.user, t.created_by)
                    agent.decide(db, t, cu.user, body.approve, body.comment)
                    res[str(i)] = t.status
                else:
                    x = _get(db, Inquiry, i)
                    gate(db, "inquiry_review", cu.user)
                    inq.review(db, x, body.approve, body.comment, cu.user)
                    res[str(i)] = x.status
        except AppError as e:
            res[str(i)] = e.code
    audit(db, cu, request, "approval.legacy_batch", body.type, None, {"results": res, "approve": body.approve})
    db.commit()
    return res
