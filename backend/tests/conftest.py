import os

os.environ.setdefault("TL_DATABASE_URL", "postgresql+psycopg://tl:tl@localhost:5432/tradeleads_test")
os.environ["TL_CELERY_EAGER"] = "true"
os.environ["TL_LLM_PRIMARY"] = "mock"
os.environ["TL_LLM_FALLBACK"] = ""

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
    with engine.begin() as c:
        c.execute(text("TRUNCATE audit_logs, jobs, users, role_permissions, roles, tenants CASCADE"))


@pytest.fixture
def client():
    return TestClient(app)


def register(client, email, tenant="公司A", pw="Passw0rd!x"):
    r = client.post("/api/v1/auth/register", json={"tenant_name": tenant, "name": "老板", "email": email, "password": pw})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def add_user(client, h, email, role, pw="Passw0rd!x"):
    r = client.post("/api/v1/users", headers=h, json={"email": email, "name": email.split("@")[0], "password": pw, "role_code": role})
    assert r.status_code == 201, r.text
    tok = client.post("/api/v1/auth/login", json={"email": email, "password": pw}).json()["access_token"]
    return r.json(), {"Authorization": f"Bearer {tok}"}
