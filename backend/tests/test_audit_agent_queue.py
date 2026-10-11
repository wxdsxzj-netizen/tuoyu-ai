"""Agent queue regression uses PostgreSQL transactions and local deterministic tools."""
from sqlalchemy import select

from app.agent import credits, runner
from app.core.config import get_settings
from app.core.db import SessionLocal, tenant_context
from app.models import AgentTask, User
from app.tasks.celery_app import celery_app
from tests.conftest import register
from tests.test_p4 import _ctx_db


def setup_task(client, monkeypatch, tools=('company.search',)):
    monkeypatch.setattr(get_settings(), 'celery_eager', False)
    monkeypatch.setitem(celery_app.conf, 'task_always_eager', False)
    monkeypatch.setattr(celery_app, 'send_task', lambda *a, **k: None)
    h = register(client, 'queue-owner@example.com')
    tid = _ctx_db(client, h)
    with tenant_context(tid), SessionLocal() as db:
        credits.grant(db, 100, note="Synthetic queue test allocation")
        owner = db.scalar(select(User))
        task = AgentTask(input_text='Synthetic queued task', status='plan_pending', created_by=owner.id,
                         plan=[{'no': n, 'tool': tool, 'zh': tool, 'qty': 1, 'args': {}, 'approval': False} for n, tool in enumerate(tools, 1)],
                         estimate={'low': 1, 'mid': 10, 'high': 10, 'minutes': 1}, context={})
        db.add(task)
        db.commit()
        return h, tid, task.id


def test_confirm_commits_a_durable_queue_without_running_a_tool_in_http(client, monkeypatch):
    h, tid, task_id = setup_task(client, monkeypatch)
    calls = []
    monkeypatch.setitem(runner.EXEC, 'company.search', lambda *a: (calls.append('called') or {}, {}))
    result = client.post(f'/api/v1/agent/tasks/{task_id}:confirm', headers=h)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'queued'
    assert calls == []
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(AgentTask, task_id).status == 'queued'
