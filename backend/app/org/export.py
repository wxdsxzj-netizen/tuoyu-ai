"""M14-05 全量数据导出：ZIP（每类对象一份 JSON + 一份 CSV），带水印 README，7 天过期；敏感凭据字段不导出。"""
import csv
import io
import json
import os
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import utcnow
from app.models import (
    Account,
    AuditLog,
    Company,
    Contact,
    Conversation,
    DataExport,
    Handoff,
    Icp,
    Inquiry,
    KnowledgeItem,
    Message,
    Opportunity,
    ProformaInvoice,
    Quote,
    Sequence,
    Suppression,
    Task,
)

SCOPES = {
    "companies": Company, "contacts": Contact, "accounts": Account, "opportunities": Opportunity, "quotes": Quote,
    "proforma_invoices": ProformaInvoice, "conversations": Conversation, "messages": Message, "inquiries": Inquiry,
    "handoffs": Handoff, "sequences": Sequence, "tasks": Task, "knowledge_items": KnowledgeItem, "icps": Icp,
    "suppressions": Suppression, "audit_logs": AuditLog,
}
SECRET_COLS = {"password_hash", "smtp_password", "imap_password", "credentials", "totp_secret", "confirm_token", "token_hash"}


def _val(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (dict, list)) or v is None or isinstance(v, (int, float, bool, str)):
        return v
    return str(v)


def build(db: Session, ex: DataExport) -> DataExport:
    ex.status = "running"
    db.flush()
    root = os.path.join(get_settings().upload_dir, "exports")
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{ex.tenant_id}-{ex.id}.zip")
    counts = {}
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for name in ex.scope or list(SCOPES):
                m = SCOPES[name]
                cols = [c.name for c in m.__table__.columns if c.name not in SECRET_COLS]
                rows = [{c: _val(getattr(o, c, None)) for c in cols} for o in db.scalars(select(m))]
                counts[name] = len(rows)
                z.writestr(f"{name}.json", json.dumps(rows, ensure_ascii=False, indent=1))
                buf = io.StringIO()
                buf.write(f"# {ex.watermark}\n")
                w = csv.DictWriter(buf, fieldnames=cols)
                w.writeheader()
                for r in rows:
                    w.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in r.items()})
                z.writestr(f"{name}.csv", "\ufeff" + buf.getvalue())
            z.writestr("README.txt", f"拓域 AI 数据导出\n{ex.watermark}\n生成时间：{utcnow().isoformat()}\n对象数量：{json.dumps(counts, ensure_ascii=False)}\n"  # noqa: E501
                                     "说明：发件箱密码、令牌等凭据不包含在导出中。附件原文件请联系平台导出。\n")
        ex.status, ex.file_path, ex.size_bytes, ex.counts = "ready", path, os.path.getsize(path), counts
        ex.expires_at = utcnow() + timedelta(days=7)
    except Exception as e:  # noqa: BLE001
        ex.status, ex.error = "failed", str(e)[:500]
    db.flush()
    return ex
