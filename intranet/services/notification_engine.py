from __future__ import annotations

import json

from ..extensions import db
from ..types import NotificationBatchResult


class NotificationEngine:
    """In-app notification enqueue and delivery scheduling."""

    @staticmethod
    def enqueue(event_type: str, actor_id: int | None, subject_ref: str) -> NotificationBatchResult:
        from ..jobs.queue import enqueue_job
        from ..models import Notification

        channels = ["in_app", "email"]
        queued = 0
        for channel in channels:
            n = Notification(
                event_type=event_type,
                actor_user_id=actor_id,
                subject_ref=subject_ref,
                channel=channel,
                status="queued",
            )
            db.session.add(n)
            db.session.flush()
            enqueue_job(
                "send_notification",
                {
                    "notification_id": n.id,
                    "channel": channel,
                    "event_type": event_type,
                    "subject_ref": subject_ref,
                },
            )
            queued += 1

        db.session.commit()
        return NotificationBatchResult(queued_jobs=queued, channels=channels)
