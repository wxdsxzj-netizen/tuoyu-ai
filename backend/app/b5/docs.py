"""附件处理（M8-09）与附件转单据（M10-29）：客户发来的询价单 / 采购单（PDF、Excel、Word、图片 OCR、CSV）→ 识别行项目 →
对照本公司产品库 SKU / 型号 → 生成报价草稿（价格按阶梯价，需人工核价后才能提交审批）。附件内容按不可信数据处理。"""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Product, ProductSku

QTY = re.compile(r"(?<![\w.])(\d{1,3}(?:[,\s]\d{3})+|\d+(?:\.\d+)?)\s*(k\b)?\s*(pcs|pieces|units|sets|pc|件|个|套|台|只|tons?|kg|m|meters?)?", re.I)  # noqa: E501
PRICE = re.compile(r"(?:usd|us\$|\$|eur|€|target|price|单价)\s*[:：]?\s*(\d+(?:\.\d+)?)", re.I)


def _catalog(db: Session):
    out = []
    for s in db.scalars(select(ProductSku).where(ProductSku.deleted_at.is_(None))):
        p = db.get(Product, s.product_id)
        if not p or p.deleted_at or p.status != "active":
            continue
        keys = {k.lower() for k in [s.sku, p.model if p else None] if k and len(k) >= 3}
        out.append((keys, s, p))
    for p in db.scalars(select(Product).where(Product.deleted_at.is_(None), Product.status == "active")):
        if p.model and not any(p.model.lower() in k for k, _, _ in out):
            out.append(({p.model.lower()}, None, p))
    return out


def unit_price(sku: ProductSku | None, p: Product | None, qty: float) -> float:
    if sku and sku.tier_prices:
        tiers = sorted(sku.tier_prices, key=lambda t: t.get("min_qty", 0))
        price = tiers[0].get("price", 0)
        for t in tiers:
            if qty >= t.get("min_qty", 0):
                price = t.get("price", price)
        return float(price)
    if p and p.price_max:
        return float(p.price_max)
    return 0.0


def parse_lines(db: Session, text: str) -> dict:
    from app.b5.ai import injection_hits
    cat = _catalog(db)
    items, unmatched = [], []
    for raw in re.split(r"[\r\n]+", text):
        line = raw.strip(" \t|;")
        if len(line) < 3:
            continue
        low = line.lower()
        hit = next(((s, p, k) for keys, s, p in cat for k in keys if re.search(r"(?<![\w-])" + re.escape(k) + r"(?![\w-])", low)), None)
        rest = low.replace(hit[2], " ") if hit else low
        qm = next((m for m in QTY.finditer(rest) if m.group(3) or hit), None)
        if not hit:
            if qm and qm.group(3) and re.search(r"[a-z\u4e00-\u9fff]{3,}", rest):
                unmatched.append(line[:200])
            continue
        s, p, _ = hit
        qty = float(qm.group(1).replace(",", "").replace(" ", "")) * (1000 if qm and qm.group(2) else 1) if qm else 1.0
        pm = PRICE.search(line)
        up = unit_price(s, p, qty)
        items.append({"name": (p.name_en or p.name_zh) if p else (s.sku if s else line[:60]),
                      "sku": s.sku if s else None, "sku_id": str(s.id) if s else None,
                      "qty": qty, "unit": (s.unit if s and s.unit else "pcs"), "unit_price": up,
                      "unit_cost": float(s.cost_price) if s and s.cost_price is not None else None,
                      "customer_target_price": float(pm.group(1)) if pm else None, "source_line": line[:200]})
    return {"items": items, "unmatched": unmatched[:30], "injection": injection_hits(text),
            "note": "价格按产品库阶梯价预填，必须人工核价；未匹配的行请手工添加。"}
