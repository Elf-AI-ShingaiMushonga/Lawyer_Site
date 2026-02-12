from __future__ import annotations

import datetime as dt

from ..db_context import set_db_access_context
from ..extensions import db
from .queue import enqueue_job


DEFAULT_PERIODIC_JOBS = [
    ("deadline_sweep", 15),
    ("deadline_escalation_scan", 15),
    ("deadline_digest", 60),
    ("retention_archive_sweep", 24 * 60),
    ("suspicious_activity_scan", 30),
    ("analytics_snapshot", 60),
    ("workload_forecast", 24 * 60),
    ("burnout_heuristics", 24 * 60),
]


def schedule_due_jobs(now: dt.datetime | None = None) -> int:
    from ..models import ScheduledJob

    set_db_access_context(user_id=None, role="system", is_admin=False, service_account=True)

    if now is None:
        now = dt.datetime.utcnow()

    jobs = ScheduledJob.query.filter(
        ScheduledJob.is_active.is_(True),
        ScheduledJob.next_run_at <= now,
    ).all()

    queued = 0
    for job in jobs:
        enqueue_job(job.job_type, job.default_payload or {}, run_after=now)
        queued += 1
        interval = max(1, int(job.interval_minutes or 1))
        job.last_run_at = now
        job.next_run_at = now + dt.timedelta(minutes=interval)

    db.session.commit()
    return queued
