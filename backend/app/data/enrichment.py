"""Shared preflight, fresh-result reuse and settlement for contact providers."""
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from app.agent import credits
from app.core.db import utcnow
from app.core.errors import AppError
from app.data import providers
from app.data.normalize import normalize_email
from app.data.providers import PersonResult
from app.data.upsert import upsert_contact
from app.models import Contact


@dataclass
class Lookup:
    people: list[PersonResult]
    provider: str | None
    tried: list[str]
    cached: list[Contact]
    owned_emails: set[str]


def lookup(db, company, *, titles, limit, actor_id, task_id=None, held=False, waterfall=False):
    if not company.domain or company.deleted_at:
        raise AppError("COMMON_INVALID_STATE", details={"reason": "公司缺少官网域名或已删除"})
    credits.lock_wallet(db)
    fresh = list(db.scalars(select(Contact).where(Contact.company_id == company.id, Contact.deleted_at.is_(None),
        Contact.email.is_not(None), Contact.source_fetched_at >= utcnow() - timedelta(hours=24))
        .order_by(Contact.created_at, Contact.id).execution_options(populate_existing=True)))
    fresh = [c for c in fresh if normalize_email(c.email)]
    if titles:
        fresh = [c for c in fresh if any(t.lower() in (c.title or "").lower() for t in titles)]
    if len(fresh) >= limit:
        return Lookup([], fresh[0].source, [], fresh[:limit], set())
    credits.authorize(db, "EMAIL_FIND", limit, actor_id=actor_id, task_id=task_id, held=held)
    owned = {normalize_email(e) for e in db.scalars(select(Contact.email).where(Contact.deleted_at.is_(None),
              Contact.email.is_not(None)))}
    if not waterfall:
        provider = providers.get_provider()
        return Lookup(list(provider.search_people(domain=company.domain, titles=titles, limit=limit))[:limit],
                      provider.name, [provider.name], [], owned)
    from app.b6.hub import waterfall_for
    settings = providers.get_settings()
    tried = []
    for name in waterfall_for("email", company.country):
        if name == "hunter" and not settings.hunter_api_key:
            name = "mock"
        tried.append(name)
        try:
            provider = providers.build_provider(name, settings)
            people = list(provider.search_people(domain=company.domain, titles=titles, limit=limit))[:limit]
            if people:
                return Lookup(people, provider.name, tried, [], owned)
        except AppError:
            continue
    return Lookup([], None, tried, [], owned)


def settle(db, result, contacts, *, actor_id, task_id=None, held=False):
    emails = {normalize_email(c.email) for c in contacts} - {None}
    newly_found = emails - result.owned_emails
    if result.cached:
        newly_found = set()
    billed = credits.consume(db, "EMAIL_FIND", len(newly_found) if newly_found else max(1, len(contacts)),
                             actor_id=actor_id, task_id=task_id, held=held,
                             result="success" if newly_found else ("cached" if emails else "failed"))
    return billed


def enrich_contacts(db, company, *, titles=None, limit=10, actor_id=None, task_id=None, held=False):
    result = lookup(db, company, titles=titles, limit=limit, actor_id=actor_id, task_id=task_id, held=held)
    counts = {"created": 0, "merged": len(result.cached), "skipped": 0}
    contacts = list(result.cached)
    seen = set()
    for person in result.people:
        email = normalize_email(person.email) if person.email else None
        if email and email in seen:
            continue
        seen.add(email)
        values = {"full_name": person.full_name, "title": person.title, "department": person.department,
                  "seniority": person.seniority, "email": person.email, "email_confidence": person.email_confidence,
                  "phone": person.phone, "linkedin_url": person.linkedin_url, "country": person.country or company.country,
                  "source_fetched_at": utcnow()}
        row = upsert_contact(db, values, company=company, actor_id=actor_id, source=result.provider)
        counts["skipped" if row.action == "suspect" else row.action] += 1
        if row.action != "suspect":
            row.obj.source_fetched_at = utcnow()
            contacts.append(row.obj)
    billed = settle(db, result, contacts, actor_id=actor_id, task_id=task_id, held=held)
    return {"provider": result.provider, **counts, "cached": bool(result.cached), "credits": billed,
            "contact_ids": [str(c.id) for c in contacts if normalize_email(c.email)]}
