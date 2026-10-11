"""A configured token is not proof of a delivered social message."""
import uuid

import pytest
from sqlalchemy import func, select

from app.core.db import SessionLocal, tenant_context
from app.models import MetaMessage
from tests.conftest import register


@pytest.mark.parametrize("platform", ["facebook", "instagram"])
def test_token_channel_rejects_unimplemented_send_and_keeps_human_task_available(client, platform):
    auth = register(client, f"owner@{platform}-audit.example.com")
    me = client.get("/api/v1/auth/me", headers=auth).json()
    channel = client.post("/api/v1/meta/channels", headers=auth, json={
        "platform": platform, "page_id": "synthetic-page", "page_name": "Synthetic page", "access_token": "synthetic-not-a-real-token"})
    assert channel.status_code == 200
    cid = channel.json()["id"]
    body = {"channel_id": cid, "peer_id": "synthetic-recipient", "body": "Synthetic message"}
    response = client.post("/api/v1/meta/messages", headers=auth, json=body)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CHANNEL_META_UNAVAILABLE"
    assert channel.json()["status"] == "unavailable"
    with tenant_context(uuid.UUID(me["tenant"]["id"])), SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(MetaMessage)) == 0
    human = client.post("/api/v1/meta/messages", headers=auth, json={**body, "human_only": True})
    assert human.status_code == 200 and human.json()["status"] == "human_task"
    assert human.json()["human_task"] is True
