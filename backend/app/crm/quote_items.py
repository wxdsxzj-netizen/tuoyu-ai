"""Quote line identity and trusted cost input (O-06/O-07)."""
import uuid

from sqlalchemy import func, select

from app.core.errors import AppError
from app.models import Product, ProductSku


def line_id(quote, item, index):
    # Legacy JSON rows gain stable identities without mutating a GET response.
    return str(item.get("id") or uuid.uuid5(quote.id, f"quote-line:{index}"))


def resolve_sku(db, sku_id, code, currency):
    query = select(ProductSku).join(Product, Product.id == ProductSku.product_id).where(
        ProductSku.deleted_at.is_(None), Product.deleted_at.is_(None), Product.status == "active")
    if sku_id:
        sku = db.scalar(query.where(ProductSku.id == sku_id))
        if not sku:
            raise AppError("COMMON_NOT_FOUND")
        if code and code.casefold() != sku.sku.casefold():
            raise AppError("COMMON_VALIDATION", details={"sku": "SKU 编号与 ID 不一致"})
    elif code:
        candidates = db.scalars(query.where(func.lower(ProductSku.sku) == code.casefold()).limit(2)).all()
        if len(candidates) != 1:
            raise AppError("COMMON_VALIDATION", details={"sku": "SKU 不存在或编号不唯一，请选择有效 SKU"})
        sku = candidates[0]
    else:
        return None
    if sku.currency != currency:
        raise AppError("COMMON_VALIDATION", details={"currency": "报价币种必须与 SKU 成本和底价币种一致"})
    return sku


def apply_items(db, quote, rows, currency, *, can_edit_cost):
    previous = {line_id(quote, item, i): item for i, item in enumerate(quote.items or [])}
    result, seen = [], set()
    for row in rows:
        item = row.model_dump(mode="json")
        identity = item.get("id")
        if identity and identity not in previous:
            raise AppError("COMMON_VALIDATION", details={"items": "报价行不属于当前报价"})
        identity = identity or str(uuid.uuid4())
        if identity in seen:
            raise AppError("COMMON_VALIDATION", details={"items": "报价行 ID 不能重复"})
        seen.add(identity)
        old = previous.get(identity)
        if item.get("unit_cost") is not None and not can_edit_cost:
            raise AppError("COMMON_FORBIDDEN", details={"field": "unit_cost"})
        sku = resolve_sku(db, row.sku_id, row.sku, currency)
        if sku:
            if "unit" in row.model_fields_set and row.unit != sku.unit:
                raise AppError("COMMON_VALIDATION", details={"unit": "报价单位必须与 SKU 一致"})
            item.update(sku_id=str(sku.id), sku=sku.sku, unit=sku.unit, unit_cost=sku.cost_price)
        elif item.get("unit_cost") is None:
            # Preserve only the same existing custom product/currency. Clearing
            # a SKU or changing product identity must not reuse its trusted cost.
            same = old and not old.get("sku_id") and quote.currency == currency and all(
                item.get(k) == old.get(k) for k in ("name", "sku", "unit"))
            item["unit_cost"] = old.get("unit_cost") if same else None
        item["id"] = identity
        result.append(item)
    return result
