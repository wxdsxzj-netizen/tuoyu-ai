"""报价单 / 形式发票 PDF（reportlab；内置 STSong-Light CID 字体支持中文，无需外部字体文件）。"""
import io

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
F = "STSong-Light"
S = ParagraphStyle("n", fontName=F, fontSize=9, leading=12)
H = ParagraphStyle("h", fontName=F, fontSize=16, leading=20, spaceAfter=6)

L = {
    "en": {"quote": "QUOTATION", "pi": "PROFORMA INVOICE", "no": "No.", "ver": "Version", "to": "To", "from": "From", "date": "Date",
           "valid": "Valid until", "terms": "Trade term", "item": "Item", "qty": "Qty", "price": "Unit price", "amount": "Amount",
           "subtotal": "Subtotal", "discount": "Discount", "freight": "Freight", "insurance": "Insurance", "total": "Total",
           "payment": "Payment terms", "delivery": "Delivery", "notes": "Notes", "bank": "Bank information", "beneficiary": "Beneficiary",
           "bank_name": "Bank", "account": "Account No.", "swift": "SWIFT", "address": "Bank address", "confirmed": "Confirmed by customer"},  # noqa: E501
    "zh": {"quote": "报价单", "pi": "形式发票", "no": "编号", "ver": "版本", "to": "客户", "from": "供应商", "date": "日期", "valid": "有效期至",  # noqa: E501
           "terms": "贸易术语", "item": "品名", "qty": "数量", "price": "单价", "amount": "金额", "subtotal": "小计", "discount": "折扣",
           "freight": "运费", "insurance": "保险", "total": "合计", "payment": "付款条件", "delivery": "交货", "notes": "备注",
           "bank": "收款银行", "beneficiary": "收款人", "bank_name": "银行", "account": "账号", "swift": "SWIFT", "address": "银行地址",
           "confirmed": "客户已确认"},
}


def _m(v: int) -> str:
    return f"{v / 100:,.2f}"


def _p(t) -> Paragraph:
    return Paragraph(str(t if t is not None else "").replace("&", "&amp;").replace("<", "&lt;"), S)


def _body(q, acc, seller, tc, title_key, extra_rows, no, bank=None) -> bytes:
    t = L.get(q.language, L["en"])
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
                            title=f"{t[title_key]} {no}")
    seller_name = (tc.sender_legal_name if tc and tc.sender_legal_name else seller)
    story = [Paragraph(t[title_key], H),
             Table([[_p(f"{t['no']}: {no}"), _p(f"{t['ver']}: {q.doc_version}"), _p(f"{t['date']}: {q.created_at:%Y-%m-%d}")],
                    [_p(f"{t['from']}: {seller_name}"), _p(f"{t['to']}: {acc.name}"),
                     _p(f"{t['valid']}: {q.valid_until or '-'}")],
                    [_p(f"{t['terms']}: {q.incoterm} {q.port or ''}"), _p(q.currency), _p("")]], colWidths=[62 * mm] * 3),
             Spacer(1, 6)]
    rows = [[_p(t["item"]), _p(t["qty"]), _p(t["price"]), _p(t["amount"])]]
    for it in q.items or []:
        rows.append([_p(f"{it.get('name')} {it.get('sku') or ''}"), _p(f"{it.get('qty'):g} {it.get('unit', '')}"),
                     _p(f"{it.get('unit_price'):,.4f}".rstrip("0").rstrip(".")), _p(_m(it.get("line_minor", 0)))])
    disc = q.subtotal_minor - (q.total_minor - q.freight_minor - q.insurance_minor)
    rows += [["", "", _p(t["subtotal"]), _p(_m(q.subtotal_minor))]]
    if disc:
        rows.append(["", "", _p(f"{t['discount']} {q.discount_pct:g}%"), _p("-" + _m(disc))])
    if q.freight_minor:
        rows.append(["", "", _p(t["freight"]), _p(_m(q.freight_minor))])
    if q.insurance_minor:
        rows.append(["", "", _p(t["insurance"]), _p(_m(q.insurance_minor))])
    rows.append(["", "", _p(t["total"]), _p(f"{q.currency} {_m(q.total_minor)}")])
    tbl = Table(rows, colWidths=[90 * mm, 25 * mm, 32 * mm, 33 * mm])
    tbl.setStyle(TableStyle([("GRID", (0, 0), (-1, len(q.items or []) ), 0.4, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),  # noqa: E501
                             ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [tbl, Spacer(1, 8)]
    for k, v in (("payment", q.payment_terms), ("delivery", q.delivery_terms), ("notes", q.notes), *extra_rows):
        if v:
            story.append(_p(f"{t.get(k, k)}: {v}"))
    if bank:
        story += [Spacer(1, 6), Paragraph(t["bank"], ParagraphStyle("b", parent=S, fontSize=11, leading=14))]
        for k in ("beneficiary", "bank_name", "account", "swift", "address"):
            v = bank.get({"account": "account_no", "address": "bank_address"}.get(k, k))
            if v:
                story.append(_p(f"{t[k]}: {v}"))
    if tc and tc.postal_address:
        story += [Spacer(1, 10), _p(f"{seller_name} · {tc.postal_address}")]
    doc.build(story)
    return buf.getvalue()


def quote_pdf(q, acc, seller, tc) -> bytes:
    extra = [("confirmed", f"{q.confirmed_by} <{q.confirmed_email}> {q.confirmed_at:%Y-%m-%d %H:%M} UTC")] if q.confirmed_at else []
    return _body(q, acc, seller, tc, "quote", extra, q.quote_no)


def pi_pdf(pi, q, acc, seller, tc) -> bytes:
    return _body(q, acc, seller, tc, "pi", [], pi.pi_no, bank=pi.bank_info)
