"""Persisted research answers must not mistake gateway mock echoes for evidence."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.b5 import ai
from app.core.db import SessionLocal, tenant_context, utcnow
from app.models import Company, ResearchQuestion, ResearchReport
from tests.conftest import register
from tests.test_p5 import _ctx_db


def report(db, *, bad_ref=False, inferred=False):
    co = Company(name='Synthetic valves', name_normalized='synthetic valves')
    db.add(co)
    db.flush()
    r = ResearchReport(company_id=co.id, expires_at=utcnow() + timedelta(days=1), sections=[{'title': 'Products', 'items': [
        {'text': 'Valves supplied in Europe', 'evidence': ['E1']},
        {'text': 'Valves supplied in America', 'evidence': ['E404' if bad_ref else 'E2'], 'inferred': inferred}]}],
        evidence=[{'id': 'E1', 'title': 'First source'}, {'id': 'E2', 'title': 'Second source'}])
    db.add(r)
    db.flush()
    return r


@pytest.mark.parametrize(('provider', 'content'), [
    ('mock', 'ECHO QUESTION FACTS E1 E2'),
    ('synthetic', 'Only the first fact【E1】'),
    ('synthetic', 'Both sources E1 E2 without citation markers'),
    ('synthetic', 'Fact one【E1】. Fact two【E2】. Invented fact【E404】'),
])
def test_untrusted_model_output_falls_back_to_complete_report_facts(client, monkeypatch, provider, content):
    h = register(client, 'qa-owner@example.com')
    tid = _ctx_db(client, h)
    monkeypatch.setattr(ai, 'guarded_chat', lambda *a, **kw: SimpleNamespace(provider=provider, model='synthetic', content=content))
    with tenant_context(tid), SessionLocal() as db:
        q = ai.ask_report(db, report(db), 'Valves', None)
        db.commit()
        assert q.answer != content
        assert 'Valves supplied in Europe【E1】' in q.answer
        assert 'Valves supplied in America【E2】' in q.answer
        assert q.model == 'rules' and q.grounded
        assert db.scalar(select(ResearchQuestion).where(ResearchQuestion.id == q.id)).citations == ['E1', 'E2']


@pytest.mark.parametrize('kind', ['bad_ref', 'inferred'])
def test_every_selected_fact_needs_resolvable_evidence_and_no_inference(client, monkeypatch, kind):
    h = register(client, 'qa-owner@example.com')
    tid = _ctx_db(client, h)
    monkeypatch.setattr(ai, 'guarded_chat', lambda *a, **kw: SimpleNamespace(provider='mock', model='mock', content='E1 E2 E404'))
    with tenant_context(tid), SessionLocal() as db:
        q = ai.ask_report(db, report(db, **{kind: True}), 'Valves', None)
        db.commit()
        assert not q.grounded
        assert 'E404' not in q.citations


def test_valid_complete_citations_can_use_real_provider_answer(client, monkeypatch):
    h = register(client, 'qa-owner@example.com')
    tid = _ctx_db(client, h)
    content = 'Valves supplied in Europe【E1】；Valves supplied in America【E2】'
    monkeypatch.setattr(ai, 'guarded_chat', lambda *a, **kw: SimpleNamespace(provider='synthetic', model='test', content=content))
    with tenant_context(tid), SessionLocal() as db:
        q = ai.ask_report(db, report(db), 'Valves', None)
        assert q.answer == content and q.model == 'synthetic:test' and q.grounded
