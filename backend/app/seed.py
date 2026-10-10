"""创建演示租户与所有者：python -m app.seed --email owner@demo.com --password Passw0rd! --tenant 演示公司"""
import argparse

from sqlalchemy import select

from app.core.db import SessionLocal, bypass_tenant
from app.models import User
from app.services import create_tenant_with_owner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenant", default="演示外贸公司")
    ap.add_argument("--email", default="owner@demo.com")
    ap.add_argument("--name", default="所有者")
    ap.add_argument("--password", default="Demo@123456")
    a = ap.parse_args()
    db = SessionLocal()
    with bypass_tenant():
        if db.scalar(select(User).where(User.email == a.email)):
            print(f"已存在: {a.email}")
            return
    t, u = create_tenant_with_owner(db, tenant_name=a.tenant, owner_email=a.email, owner_name=a.name,
                                    password=a.password)
    print(f"已创建租户 {t.name} ({t.slug})，所有者 {u.email}")


if __name__ == "__main__":
    main()
