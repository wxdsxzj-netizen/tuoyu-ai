"""Real PostgreSQL transactions; deterministic local tools, no provider/network calls."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.agent import credits, queue, runner
from app.core.db import SessionLocal, tenant_context, utcnow
from app.models import AgentControl, AgentStepExecution, AgentStepLog, AgentTask, Company, CreditReservation, User
from tests.test_audit_agent_queue import setup_task


def queued(client, monkeypatch, tools=("company.search",)):
    h, tid, task_id = setup_task(client, monkeypatch, tools)
    monkeypatch.setattr(queue, 'dispatch', lambda *a: None)
    result = client.post(f'/api/v1/agent/tasks/{task_id}:confirm', headers=h)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'queued'
    return h, tid, task_id


def inspect(tid, task_id):
    with tenant_context(tid), SessionLocal() as db:
        t = db.get(AgentTask, task_id)
        return t.status, t.current_step, t.pause_reason, t.context


def expire(tid, task_id):
    with tenant_context(tid), SessionLocal() as db:
        db.get(AgentTask, task_id).lease_until = utcnow() - timedelta(seconds=1)
        db.commit()


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_control_commits_during_a_blocked_tool_and_stops_before_next_step(client, monkeypatch, action):
    h, tid, task_id = queued(client, monkeypatch, ('company.search', 'sequence.create'))
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(db, task, step, user):
        credits.lock_wallet(db)
        entered.set()
        assert release.wait(8), 'control request was blocked by the tool transaction'
        return {'first': 'committed'}, {}

    monkeypatch.setitem(runner.EXEC, 'company.search', slow)
    monkeypatch.setitem(runner.EXEC, 'sequence.create', lambda *a: (calls.append('second') or {}, {}))
    with ThreadPoolExecutor(max_workers=3) as pool:
        worker = pool.submit(queue.run_task, tid, task_id)
        assert entered.wait(5)
        duplicate = pool.submit(queue.run_task, tid, task_id)
        duplicate.result(timeout=3)  # Live worker owns a physical session advisory lock.
        control = pool.submit(client.post, f'/api/v1/agent/tasks/{task_id}:{action}', headers=h)
        try:
            response = control.result(timeout=3)
            assert response.status_code == 200, response.text
            assert response.json()['control_requested'] == action
        finally:
            release.set()
        worker.result(timeout=5)
    state, cursor, _, context = inspect(tid, task_id)
    assert state == ('paused' if action == 'pause' else 'cancelled')
    assert cursor == 1 and context['first'] == 'committed'
    assert calls == []
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(AgentControl, task_id) is None
        held = db.scalar(select(CreditReservation).where(CreditReservation.task_id == task_id))
        assert held.remaining == (10 if action == 'pause' else 0)


def test_sql_failure_rolls_back_side_effects_before_logging_and_releasing(client, monkeypatch):
    _, tid, task_id = queued(client, monkeypatch)

    def broken(db, *args):
        db.add(Company(name='Must roll back', name_normalized='must roll back', domain='rollback.example'))
        db.flush()
        db.execute(text('SELECT 1 / 0'))

    monkeypatch.setitem(runner.EXEC, 'company.search', broken)
    queue.run_task(tid, task_id)
    assert inspect(tid, task_id)[:2] == ('failed', 1)
    with tenant_context(tid), SessionLocal() as db:
        assert db.scalar(select(Company.id).where(Company.domain == 'rollback.example')) is None
        log = db.scalar(select(AgentStepLog).where(AgentStepLog.task_id == task_id, AgentStepLog.status == 'failed'))
        assert log.output == {'error': 'DataError'}
        assert db.scalar(select(CreditReservation.remaining).where(CreditReservation.task_id == task_id)) == 0


class SimulatedWorkerCrash(BaseException):
    pass


def test_crash_keeps_completed_step_and_requires_evidence_before_external_retry(client, monkeypatch):
    h, tid, task_id = queued(client, monkeypatch, ('company.search', 'contact.find'))
    calls = []
    monkeypatch.setitem(runner.EXEC, 'company.search', lambda *a: (calls.append('first') or {'first': True}, {}))

    def uncertain(*a):
        calls.append('external')
        raise SimulatedWorkerCrash()

    monkeypatch.setitem(runner.EXEC, 'contact.find', uncertain)
    with pytest.raises(SimulatedWorkerCrash):
        queue.run_task(tid, task_id)
    assert inspect(tid, task_id)[:2] == ('running', 1)
    expire(tid, task_id)
    queue.run_task(tid, task_id)
    assert inspect(tid, task_id)[:3] == ('paused', 1, 'outcome_unknown')
    assert calls == ['first', 'external']
    assert client.post(f'/api/v1/agent/tasks/{task_id}:resume', headers=h).status_code == 409
    assert client.post(f'/api/v1/agent/tasks/{task_id}:reconcile-not-applied', headers=h,
                       json={'evidence': 'short'}).status_code == 400
    response = client.post(f'/api/v1/agent/tasks/{task_id}:reconcile-not-applied', headers=h,
                           json={'evidence': 'Local provider receipt confirms the operation was never applied.'})
    assert response.status_code == 200, response.text
    monkeypatch.setitem(runner.EXEC, 'contact.find', lambda *a: ({'recovered': True}, {}))
    queue.run_task(tid, task_id)
    assert inspect(tid, task_id)[:2] == ('completed', 2)
    assert calls == ['first', 'external']
    with tenant_context(tid), SessionLocal() as db:
        receipts = list(db.scalars(select(AgentStepExecution).where(AgentStepExecution.task_id == task_id)
                                  .order_by(AgentStepExecution.step_no)))
        assert [(r.state, r.attempt) for r in receipts] == [('completed', 1), ('completed', 2)]


def test_recovery_rechecks_creator_permissions_and_enforces_running_cap(client, monkeypatch):
    h, tid, task_id = queued(client, monkeypatch)
    calls = []
    monkeypatch.setitem(runner.EXEC, 'company.search', lambda *a: (calls.append('ran') or {}, {}))
    with tenant_context(tid), SessionLocal() as db:
        task = db.get(AgentTask, task_id)
        for _ in range(3):
            db.add(AgentTask(input_text='Other active task', status='running', created_by=task.created_by,
                             lease_until=utcnow() + queue.LEASE))
        db.commit()
    queue.run_task(tid, task_id)
    assert inspect(tid, task_id)[0] == 'queued' and not calls
    with tenant_context(tid), SessionLocal() as db:
        other = db.scalar(select(AgentTask).where(AgentTask.id != task_id))
        other.status = 'completed'
        db.get(User, db.get(AgentTask, task_id).created_by).is_active = False
        db.commit()
    queue.run_task(tid, task_id)
    assert inspect(tid, task_id)[0] == 'failed' and not calls


def test_broker_failure_retains_queue_for_periodic_recovery(client, monkeypatch):
    from app.tasks.jobs import agent_run_task, recover_agents_task
    h, tid, task_id = setup_task(client, monkeypatch)
    monkeypatch.setattr(agent_run_task, 'delay', lambda *a: (_ for _ in ()).throw(ConnectionError()))
    result = client.post(f'/api/v1/agent/tasks/{task_id}:confirm', headers=h)
    assert result.status_code == 200 and result.json()['status'] == 'queued'
    recovered = []
    monkeypatch.setattr(queue, 'dispatch', lambda tenant, task: recovered.append((tenant, task)))
    recover_agents_task()
    assert recovered == [(tid, task_id)]


def test_permission_revocation_is_checked_after_confirmation(client, monkeypatch):
    _, tid, task_id = queued(client, monkeypatch)
    calls = []
    monkeypatch.setitem(runner.EXEC, 'company.search', lambda *a: (calls.append('ran') or {}, {}))
    with tenant_context(tid), SessionLocal() as db:
        user = db.get(User, db.get(AgentTask, task_id).created_by)
        user.role.permissions = [p for p in user.role.permissions if p.code != 'company.read']
        db.commit()
    queue.run_task(tid, task_id)
    assert inspect(tid, task_id)[0] == 'failed' and not calls


def test_control_arriving_after_unknown_pause_is_drained_without_retry(client, monkeypatch):
    from app.tasks.jobs import recover_agents_task
    _, tid, task_id = queued(client, monkeypatch, ('contact.find',))
    calls = []
    monkeypatch.setitem(runner.EXEC, 'contact.find', lambda *a: (calls.append('ran') or {}, {}))
    with tenant_context(tid), SessionLocal() as db:
        task = db.get(AgentTask, task_id)
        task.status, task.pause_reason = 'paused', 'outcome_unknown'
        db.add(AgentStepExecution(task_id=task_id, step_no=1, tool='contact.find', state='unknown', attempt=1))
        queue.request_control(db, task, 'cancel', task.created_by)
        db.commit()
    recovered = []
    monkeypatch.setattr(queue, 'dispatch', lambda tenant, task: recovered.append((tenant, task)))
    recover_agents_task()
    assert recovered == [(tid, task_id)]
    queue.run_task(tid, task_id)
    assert inspect(tid, task_id)[0] == 'cancelled' and not calls
    with tenant_context(tid), SessionLocal() as db:
        assert db.get(AgentControl, task_id) is None
        assert db.scalar(select(AgentStepExecution.state).where(AgentStepExecution.task_id == task_id)) == 'unknown'


def test_resumed_task_still_waits_for_running_capacity(client, monkeypatch):
    h, tid, task_id = queued(client, monkeypatch)
    with tenant_context(tid), SessionLocal() as db:
        task = db.get(AgentTask, task_id)
        task.status, task.pause_reason = 'paused', 'user'
        for _ in range(3):
            db.add(AgentTask(input_text='Active slot', status='running', created_by=task.created_by, lease_until=utcnow() + queue.LEASE))
        db.commit()
    result = client.post(f'/api/v1/agent/tasks/{task_id}:resume', headers=h)
    assert result.status_code == 200 and result.json()['status'] == 'queued'
    queue.run_task(tid, task_id)
    assert inspect(tid, task_id)[0] == 'queued'
