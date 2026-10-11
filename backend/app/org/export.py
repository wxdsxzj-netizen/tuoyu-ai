"""M14-05 全量数据导出：ZIP（每类对象一份 JSON + 一份 CSV），带水印 README，7 天过期；敏感凭据字段不导出。"""
import csv
import io
import json
import os
import uuid
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

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
    from app.compliance.erasure import lock_tenant
    lock_tenant(db)
    db.refresh(ex)
    if ex.status != "pending":
        return ex
    ex.status = "running"
    db.flush()
    from app.org.export_files import managed_root, owned_paths, remove_paths
    counts = {}
    path = temporary = None
    try:
        root = managed_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = root / f"{ex.tenant_id}-{ex.id}.zip"
        remove_paths(owned_paths(ex.tenant_id, ex.id))
        temporary = root / f".{ex.tenant_id}-{ex.id}.{uuid.uuid4().hex}.tmp"
        # The savepoint makes real SQL errors recoverable before recording failed status.
        with db.begin_nested():
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            with os.fdopen(fd, "w+b") as stream, zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as z:
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
            os.replace(temporary, path)
            ex.status, ex.file_path, ex.size_bytes, ex.counts = "ready", str(path), path.stat().st_size, counts
            ex.expires_at = utcnow() + timedelta(days=7)
            db.flush()
    except Exception as exc:  # noqa: BLE001
        # Raw exceptions may contain query parameters or personal data.
        ex.status, ex.file_path, ex.error = "failed", None, f"Export failed ({type(exc).__name__})"
        # Failure to clean is surfaced, and the deterministic name remains discoverable by DSR.
        remove_paths(owned_paths(ex.tenant_id, ex.id))
    db.flush()
    return ex
