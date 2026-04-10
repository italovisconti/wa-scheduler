from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from wa_scheduler.models import Chat, Contact, OutboundJob, Schedule
from wa_scheduler.services.wacli import WacliClient, WacliError


def build_health_payload(session: Session) -> dict:
    client = WacliClient()
    health_error = ""
    health = None

    try:
        health, _ = client.doctor()
    except WacliError as exc:
        health_error = str(exc)

    counts = {
        "contacts": session.scalar(select(func.count()).select_from(Contact)) or 0,
        "chats": session.scalar(select(func.count()).select_from(Chat)) or 0,
        "schedules": session.scalar(select(func.count()).select_from(Schedule)) or 0,
        "jobs_pending": session.scalar(
            select(func.count())
            .select_from(OutboundJob)
            .where(OutboundJob.status == "pending")
        )
        or 0,
    }

    return {
        "ok": health_error == "",
        "wacli": health,
        "error": health_error,
        "counts": counts,
    }
