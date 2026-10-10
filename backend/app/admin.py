"""平台管理员命令行（试点期唯一的开通渠道之一）。

  python -m app.admin create-tenant --tenant 演示外贸公司 --email owner@demo.com --name 张三 --password '至少8位'
  python -m app.admin list-tenants
  python -m app.admin sync-roles      # 权限点变更后，同步所有租户的系统角色
  python -m app.admin grant-credits --tenant <slug> --amount 1000 --note "试点充值"
  python -m app.admin credits --tenant <slug>                       # 余额与最近账本
  python -m app.admin create-invoice --tenant <slug> --amount 2999.00 --currency CNY --credits 3000 --period 2026-10 --desc "SOHO 月费"
  python -m app.admin mark-invoice --tenant <slug> --no INV-202610-0001 --status paid --method 银行转账 --ref 流水号
"""
import argparse
import sys

from sqlalchemy import select

from app.core.db import SessionLocal, bypass_tenant
from app.core.errors import AppError
from app.models import Tenant, User
from app.services import create_tenant_with_owner, sync_system_roles


def main(argv=None):
    ap = argparse.ArgumentParser(prog="app.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create-tenant")
    c.add_argument("--tenant", required=True)
    c.add_argument("--email", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--password", required=True)
    c.add_argument("--slug")
    sub.add_parser("list-tenants")
    sub.add_parser("sync-roles")
    g = sub.add_parser("grant-credits")
    g.add_argument("--tenant", required=True)
    g.add_argument("--amount", type=float, required=True)
    g.add_argument("--note", required=True)
    g.add_argument("--correction", action="store_true", help="更正条目（可为负数）")
    g.add_argument("--operator", default="cli")
    cr = sub.add_parser("credits")
    cr.add_argument("--tenant", required=True)
    ci = sub.add_parser("create-invoice")
    ci.add_argument("--tenant", required=True)
    ci.add_argument("--amount", required=True, help="金额（元，两位小数）")
    ci.add_argument("--currency", default="CNY")
    ci.add_argument("--credits", type=float, default=0)
    ci.add_argument("--period")
    ci.add_argument("--desc", default="")
    ci.add_argument("--operator", default="cli")
    mi = sub.add_parser("mark-invoice")
    mi.add_argument("--tenant", required=True)
    mi.add_argument("--no", required=True)
    mi.add_argument("--status", choices=["paid", "void"], required=True)
    mi.add_argument("--method")
    mi.add_argument("--ref")
    mi.add_argument("--operator", default="cli")
    a = ap.parse_args(argv)
    db = SessionLocal()
    try:
        if a.cmd == "create-tenant":
            if len(a.password) < 8:
                print("密码至少 8 位", file=sys.stderr)
                return 2
            try:
                t, u = create_tenant_with_owner(db, tenant_name=a.tenant, owner_email=a.email, owner_name=a.name,
                                                password=a.password, slug=a.slug, actor_type="platform")
            except AppError as e:
                print(f"失败：{e.message} {e.details}", file=sys.stderr)
                return 1
            print(f"已创建租户 {t.name}（{t.slug}），所有者 {u.email}")
        elif a.cmd == "list-tenants":
            with bypass_tenant():
                for t in db.scalars(select(Tenant).order_by(Tenant.created_at)):
                    n = len(db.scalars(select(User.id).where(User.tenant_id == t.id)).all())
                    print(f"{t.slug}\t{t.name}\t{t.status}\t成员 {n}")
        elif a.cmd == "sync-roles":
            print(f"已同步 {sync_system_roles(db)} 个租户")
        elif a.cmd in ("grant-credits", "credits", "create-invoice", "mark-invoice"):
            from decimal import Decimal

            from app.agent import credits
            from app.api import billing
            from app.core.db import tenant_context
            try:
                t = billing.tenant_by(db, a.tenant)
                if a.cmd == "grant-credits":
                    billing.do_grant(db, t, a.amount, a.note, a.operator, "correction" if a.correction else "grant")
                elif a.cmd == "create-invoice":
                    i = billing.create_invoice(db, t, amount_minor=int(Decimal(a.amount) * 100), currency=a.currency, credits_=a.credits,
                                               period=a.period, description=a.desc, note=None, operator=a.operator)
                    print(f"已登记发票 {i.invoice_no}（待付款）")
                elif a.cmd == "mark-invoice":
                    i = billing.mark_invoice(db, t, a.no, a.status, method=a.method, ref=a.ref, operator=a.operator)
                    print(f"发票 {i.invoice_no} → {i.status}")
            except AppError as e:
                print(f"失败：{e.message} {e.details}", file=sys.stderr)
                return 1
            with tenant_context(t.id):
                print(f"{t.slug} 当前余额：{credits.balance(db)} 点")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
