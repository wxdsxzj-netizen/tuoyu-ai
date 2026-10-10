from celery import Celery
from celery.schedules import crontab

from app.core.config import get_settings

s = get_settings()
celery_app = Celery("tradeleads", broker=s.redis_url, backend=s.redis_url, include=["app.tasks.jobs"])
celery_app.conf.update(
    task_always_eager=s.celery_eager, task_eager_propagates=True, task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "sanctions-daily": {"task": "sanctions.refresh", "schedule": crontab(hour=10, minute=30)},
        "outreach-tick": {"task": "outreach.tick", "schedule": 60.0},
        "mailbox-health": {"task": "mailbox.health", "schedule": 900.0},
        "mailbox-poll": {"task": "mailbox.poll", "schedule": 120.0},
        "dns-daily": {"task": "dns.daily", "schedule": crontab(hour=9, minute=0)},
        "dsr-daily": {"task": "dsr.daily", "schedule": crontab(hour=1, minute=0)},
    }, task_default_retry_delay=10, timezone="UTC", task_serializer="json",  # noqa: E501
)
