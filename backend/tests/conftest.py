import os

os.environ.setdefault("TL_DATABASE_URL", "postgresql+psycopg://tl:tl@localhost:5432/tradeleads_test")
os.environ["TL_CELERY_EAGER"] = "true"
os.environ["TL_LLM_PRIMARY"] = "mock"
os.environ["TL_LLM_FALLBACK"] = ""
os.environ["TL_PLATFORM_ADMIN_TOKEN"] = "test-platform-token"
os.environ["TL_CRAWL_ALLOW_PRIVATE"] = "true"
os.environ["TL_UPLOAD_DIR"] = "/tmp/tuoyu-test-uploads"
os.environ["TL_VERIFY_CHECK_MX"] = "false"
os.environ["TL_VERIFIER_PROVIDER"] = "mock"
os.environ["TL_CONTACT_PROVIDER"] = "auto"
for _k in ("HUNTER_API_KEY", "TL_HUNTER_API_KEY", "MILLIONVERIFIER_API_KEY", "TL_MILLIONVERIFIER_API_KEY"):
    os.environ.pop(_k, None)

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from alembic import command
from app.core.db import engine
from app.main import app

HERE = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture(scope="session", autouse=True)
def migrate():
    cfg = Config(os.path.join(HERE, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(HERE, "alembic"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean():
    yield
    from app.compliance.verify import set_mx_resolver, set_verifier
    set_verifier(None)
    set_mx_resolver(None)
    with engine.begin() as c:
        c.execute(text("TRUNCATE email_verifications, global_bounces, global_suppressions, complaint_events, suppressions, "
                       "consent_records, lia_records, tenant_compliance, screening_hits, sanction_tokens, sanction_entries, "
                       "sanction_list_versions CASCADE"))
        c.execute(text("DELETE FROM region_rules WHERE submitted_by <> 'system-seed'"))
        c.execute(text("UPDATE region_rules SET status='published' WHERE submitted_by = 'system-seed'"))
        c.execute(text("TRUNCATE audit_logs, jobs, crawled_pages, crawl_jobs, knowledge_items, icps, contacts, companies, import_jobs, users, role_permissions, roles, tenants CASCADE"))


@pytest.fixture
def client():
    return TestClient(app)


def register(client, email, tenant="公司A", pw="Passw0rd!x"):
    """试点期没有自助注册：通过平台管理员服务开通租户，再登录。"""
    from app.core.db import SessionLocal
    from app.services import create_tenant_with_owner
    db = SessionLocal()
    create_tenant_with_owner(db, tenant_name=tenant, owner_email=email, owner_name="老板", password=pw)
    db.close()
    r = client.post("/api/v1/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def add_user(client, h, email, role, pw="Passw0rd!x"):
    r = client.post("/api/v1/users", headers=h, json={"email": email, "name": email.split("@")[0], "password": pw, "role_code": role})
    assert r.status_code == 201, r.text
    tok = client.post("/api/v1/auth/login", json={"email": email, "password": pw}).json()["access_token"]
    return r.json(), {"Authorization": f"Bearer {tok}"}
