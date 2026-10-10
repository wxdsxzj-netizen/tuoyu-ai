"""平台管理员命令行（试点期唯一的开通渠道之一）。

  python -m app.admin create-tenant --tenant 演示外贸公司 --email owner@demo.com --name 张三 --password '至少8位'
  python -m app.admin list-tenants
  python -m app.admin sync-roles      # 权限点变更后，同步所有租户的系统角色
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
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
