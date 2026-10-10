"""发件箱额度、预热爬坡、健康分与分级熔断（M6-04/06/07，8.6，BR-6-11~15/32）。"""
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import utcnow
from app.models import BreakerEvent, Mailbox, MailboxMetricDaily, SendingDomain

LEVEL_FACTOR = {"normal": 1.0, "watch": 0.7, "slow": 0.5, "paused": 0.0}
SENDABLE_STATUS = {"warming", "active", "recovering"}


def warmup_quota(day_no: int, target: int) -> int:
    """BR-6-11：第 1–3 天每天 5 封，之后每 2 天 +5，直到目标（默认 40，约第 17 天达到；21 天完成预热）。"""
    if day_no <= 0:
        return 0
    q = 5 if day_no <= 3 else 5 + 5 * ((day_no - 3 + 1) // 2)
    return min(q, target)


def warmup_day(mb: Mailbox, today: date | None = None) -> int:
    if not mb.warmup_started_on:
        return 0
    return ((today or date.today()) - mb.warmup_started_on).days + 1


def today_metric(db: Session, mb: Mailbox, day: date | None = None) -> MailboxMetricDaily:
    d = day or date.today()
    m = db.scalar(select(MailboxMetricDaily).where(MailboxMetricDaily.mailbox_id == mb.id, MailboxMetricDaily.day == d))
    if not m:
        m = MailboxMetricDaily(mailbox_id=mb.id, day=d, sent_cold=0, sent_reply=0, hard_bounces=0, complaints=0, replies=0)
        db.add(m)
        db.flush()
    return m


def daily_cold_limit(mb: Mailbox) -> int:
    """当日冷信额度 = min(预热额度, 上限, 平台硬上限) × 健康级别系数 × 恢复期 50%。"""
    if mb.status not in SENDABLE_STATUS or mb.auth_status != "ok":
        return 0
    s = get_settings()
    target = min(mb.daily_cold_limit, s.platform_cold_hard_cap)
    base = warmup_quota(warmup_day(mb), target) if mb.status == "warming" else target
    q = base * LEVEL_FACTOR.get(mb.health_level, 0)
    if mb.status == "recovering" or (mb.recovery_until and mb.recovery_until > utcnow()):
        q *= 0.5
    return int(q)


def remaining_today(db: Session, mb: Mailbox) -> int:
    return max(daily_cold_limit(mb) - today_metric(db, mb).sent_cold, 0)


def rates_7d(db: Session, mb: Mailbox) -> dict:
    since = date.today() - timedelta(days=6)
    row = db.execute(select(func.coalesce(func.sum(MailboxMetricDaily.sent_cold + MailboxMetricDaily.sent_reply), 0),
                            func.coalesce(func.sum(MailboxMetricDaily.hard_bounces), 0),
                            func.coalesce(func.sum(MailboxMetricDaily.complaints), 0),
                            func.coalesce(func.sum(MailboxMetricDaily.replies), 0))
                     .where(MailboxMetricDaily.mailbox_id == mb.id, MailboxMetricDaily.day >= since)).one()
    sent, hb, cp, rp = (int(x) for x in row)
    return {"sent": sent, "hard_bounce_rate": hb / sent if sent else 0.0, "complaint_rate": cp / sent if sent else 0.0,
            "reply_rate": rp / sent if sent else 0.0, "hard_bounces": hb, "complaints": cp}


def health_score(r: dict, blacklisted: bool = False, inbox: float = 1.0) -> int:
    h = (100 - 40 * min(1, r["complaint_rate"] / 0.001) - 25 * min(1, r["hard_bounce_rate"] / 0.05) - 15 * (1 if blacklisted else 0)
         - 10 * (1 - inbox) + 5 * min(1, r["reply_rate"] / 0.05))
    return int(max(0, min(100, round(h))))


def evaluate(db: Session, mb: Mailbox, *, blacklisted: bool = False) -> dict:
    """重算健康分并执行熔断。样本 < 50 封时沿用上次分数（标注样本不足），但绝对阈值仍生效。"""
    s = get_settings()
    r = rates_7d(db, mb)
    enough = r["sent"] >= s.health_min_sample
    if enough or blacklisted:
        mb.health_score, mb.health_note = health_score(r, blacklisted), None
    else:
        mb.health_note = "样本不足"
    cr, br = (r["complaint_rate"], r["hard_bounce_rate"]) if enough else (0.0, 0.0)
    level, metric, value = "normal", None, 0.0
    if blacklisted:
        level, metric, value = "paused", "blacklist", 1
    elif cr >= 0.001:
        level, metric, value = "paused", "complaint_rate", cr
    elif br >= 0.05:
        level, metric, value = "paused", "hard_bounce_rate", br
    elif cr >= 0.0008:
        level, metric, value = "slow", "complaint_rate", cr
    elif br >= 0.03:
        level, metric, value = "slow", "hard_bounce_rate", br
    elif br >= 0.02 or mb.health_score < 80:
        level, metric, value = "watch", "hard_bounce_rate" if br >= 0.02 else "health_score", br if br >= 0.02 else mb.health_score
    elif mb.health_score < 60:
        level = "watch"
    if level == "paused" and mb.status not in ("paused", "cooling", "disabled", "unbound"):
        trip(db, mb, metric, value)
    elif level != "paused" and mb.status not in ("paused", "cooling"):
        if level != mb.health_level and level in ("watch", "slow"):
            db.add(BreakerEvent(scope="mailbox", scope_id=mb.id, level=level, trigger_metric=metric or "health_score",
                                trigger_value=float(value), cooldown_hours=0))
        mb.health_level = level
    if enough and cr >= 0.003 and mb.domain_id:
        pause_domain(db, mb.domain_id, "complaint_rate", cr)
    return {"score": mb.health_score, "level": mb.health_level, "status": mb.status, **r}


def trip(db: Session, mb: Mailbox, metric: str, value: float):
    """暂停冷信：恢复期内再犯冷却 96 小时（BR-6-32）。"""
    repeat = mb.status == "recovering" or (mb.recovery_until and mb.recovery_until > utcnow())
    hours = 96 if repeat else 48
    mb.status, mb.health_level, mb.recovery_until = "paused", "paused", None
    mb.breaker_count_30d += 1
    db.add(BreakerEvent(scope="mailbox", scope_id=mb.id, level="paused", trigger_metric=metric, trigger_value=float(value),
                        cooldown_hours=hours))
    db.flush()
    from app.crm.notify import managers, notify  # N-05 熔断触发：负责人 + 管理员，不可关闭
    shown = f"{value * 100:.2f}%" if metric.endswith("rate") else str(value)
    notify(db, "N-05", [mb.owner_id, *managers(db)], f"发件箱 {mb.address} 已暂停冷信",
           f"触发指标：{metric} = {shown}；冷却 {hours} 小时。请处理原因后在“发件箱与域名”中标记已处理，冷却期满由管理员确认恢复。",
           object_type="mailbox", object_id=mb.id, action_url="/mailboxes", level="urgent")
    if mb.domain_id:
        n = db.scalar(select(func.count()).select_from(Mailbox).where(Mailbox.domain_id == mb.domain_id, Mailbox.status == "paused"))
        if n >= 2:
            pause_domain(db, mb.domain_id, "mailboxes_paused", n)


def pause_domain(db: Session, domain_id, metric, value):
    d = db.get(SendingDomain, domain_id)
    if d and not d.paused:
        d.paused = True
        db.add(BreakerEvent(scope="domain", scope_id=d.id, level="domain_paused", trigger_metric=metric, trigger_value=float(value),
                            cooldown_hours=48))
        from app.crm.notify import notify, users_with
        notify(db, "N-05", users_with(db, "billing.manage") or users_with(db, "mailbox.manage"), f"发信域名 {d.domain} 已暂停全部发送",
               f"触发：{metric} = {value}；需平台投递团队复核后恢复。", object_type="domain", object_id=d.id,
               action_url="/mailboxes", level="urgent")


def mark_resolved(db: Session, mb: Mailbox, resolution: str, actor):
    """原因已处理 → 冷却中（开始计时）。"""
    ev = db.scalar(select(BreakerEvent).where(BreakerEvent.scope_id == mb.id, BreakerEvent.level == "paused",
                                              BreakerEvent.recovered_at.is_(None)).order_by(BreakerEvent.created_at.desc()).limit(1))
    hours = ev.cooldown_hours if ev else 48
    if ev:
        ev.resolution, ev.resolved_by = resolution, actor
    mb.status, mb.cooldown_until = "cooling", utcnow() + timedelta(hours=hours)
    return mb


def confirm_recovery(db: Session, mb: Mailbox, actor) -> bool:
    """冷却结束 + 管理员确认 → 恢复中（50% 额度 7 天）。"""
    if mb.status != "cooling" or not mb.cooldown_until or mb.cooldown_until > utcnow():
        return False
    mb.status, mb.health_level, mb.recovery_until = "recovering", "normal", utcnow() + timedelta(days=7)
    for ev in db.scalars(select(BreakerEvent).where(BreakerEvent.scope_id == mb.id, BreakerEvent.recovered_at.is_(None))):
        ev.recovered_at = utcnow()
    return True


def tick_lifecycle(db: Session, mb: Mailbox):
    """预热满 21 天 → 正常；恢复期满 → 正常。"""
    from app.crm.notify import notify
    if mb.status == "warming" and warmup_day(mb) > 21:
        mb.status = "active"
        notify(db, "N-20", [mb.owner_id], f"发件箱 {mb.address} 预热完成", object_type="mailbox", object_id=mb.id, action_url="/mailboxes")
    if mb.status == "recovering" and mb.recovery_until and mb.recovery_until <= utcnow():
        mb.status, mb.recovery_until = "active", None
        notify(db, "N-20", [mb.owner_id], f"发件箱 {mb.address} 已恢复正常", object_type="mailbox", object_id=mb.id, action_url="/mailboxes")  # noqa: E501
