from __future__ import annotations

import datetime as dt
from ..timeutils import utc_now
import json

from sqlalchemy import or_, select

from ..extensions import db


def enqueue_job(job_type: str, payload: dict, *, run_after: dt.datetime | None = None, max_attempts: int = 5) -> int:
    from ..models import JobQueue

    job = JobQueue(
        job_type=job_type,
        payload_json=json.dumps(payload),
        status="queued",
        attempts=0,
        max_attempts=max_attempts,
        run_after=run_after,
    )
    db.session.add(job)
    db.session.flush()
    return job.id


def lease_job(worker_id: str, lease_seconds: int = 60):
    from ..models import JobQueue

    now = utc_now()
    stmt = (
        select(JobQueue)
        .where(
            JobQueue.status.in_(["queued", "failed"]),
            or_(JobQueue.run_after.is_(None), JobQueue.run_after <= now),
            or_(JobQueue.lease_until.is_(None), JobQueue.lease_until < now),
            JobQueue.attempts < JobQueue.max_attempts,
        )
        .order_by(JobQueue.created_at.asc())
        .limit(1)
    )
    bind = db.session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    candidate = db.session.execute(stmt).scalars().first()
    if candidate is None:
        return None

    candidate.status = "running"
    candidate.started_at = now
    candidate.lease_until = now + dt.timedelta(seconds=lease_seconds)
    candidate.worker_id = worker_id
    candidate.attempts = int(candidate.attempts or 0) + 1
    db.session.commit()
    return candidate


def complete_job(job_id: int, message: str = "ok") -> None:
    from ..models import JobHistory, JobQueue

    job = db.session.get(JobQueue, job_id)
    if job is None:
        return
    job.status = "succeeded"
    job.finished_at = utc_now()
    job.last_error = None
    db.session.add(JobHistory(job_id=job.id, status="succeeded", message=message))
    db.session.commit()


def fail_job(job_id: int, error: str) -> None:
    from ..models import JobHistory, JobQueue

    job = db.session.get(JobQueue, job_id)
    if job is None:
        return
    job.last_error = error[:2000]
    if int(job.attempts or 0) >= int(job.max_attempts or 0):
        job.status = "dead_letter"
    else:
        job.status = "failed"
        job.lease_until = None
    job.finished_at = utc_now()
    db.session.add(JobHistory(job_id=job.id, status=job.status, message=job.last_error))
    db.session.commit()
