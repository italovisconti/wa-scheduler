from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from jinja2 import Template
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, joinedload

from wa_scheduler.config import get_settings
from wa_scheduler.models import OutboundJob, Schedule, ScheduledRun
from wa_scheduler.timeutil import (
    next_daily_occurrence,
    next_monthly_occurrence,
    next_weekly_occurrence,
    parse_hhmm,
    utcnow,
)


def compute_next_run(
    schedule: Schedule, reference_utc: datetime | None = None
) -> datetime | None:
    reference_utc = reference_utc or utcnow()

    if schedule.schedule_type == "one_time":
        if schedule.one_time_at and schedule.one_time_at > reference_utc:
            return schedule.one_time_at
        return None

    if schedule.schedule_type == "interval":
        interval_minutes = schedule.interval_minutes
        if interval_minutes is None and schedule.interval_hours is not None:
            interval_minutes = schedule.interval_hours * 60
        start_at = schedule.one_time_at
        if interval_minutes is None or interval_minutes < 5 or start_at is None:
            return None

        interval = timedelta(minutes=interval_minutes)
        if reference_utc <= start_at:
            candidate = start_at
        else:
            elapsed = reference_utc - start_at
            intervals_elapsed = int(elapsed.total_seconds() // interval.total_seconds())
            candidate = start_at + interval * (intervals_elapsed + 1)

        until_at = schedule.repeat_until_at
        if until_at is not None and candidate > until_at:
            return None
        return candidate

    at_time = parse_hhmm(schedule.time_of_day)
    if at_time is None:
        return None

    timezone_name = schedule.timezone or get_settings().default_timezone

    if schedule.schedule_type == "daily":
        return next_daily_occurrence(reference_utc, timezone_name, at_time)

    if schedule.schedule_type == "weekly":
        weekdays = [
            int(value) for value in schedule.weekdays.split(",") if value.strip()
        ]
        return next_weekly_occurrence(reference_utc, timezone_name, at_time, weekdays)

    if schedule.schedule_type == "monthly":
        day_of_month = schedule.day_of_month or 1
        return next_monthly_occurrence(
            reference_utc, timezone_name, at_time, day_of_month
        )

    return None


def build_run_payload(schedule: Schedule) -> dict[str, Any]:
    target = None
    target_label = None
    target_kind = schedule.target_type

    if schedule.target_type == "contact" and schedule.contact is not None:
        target = schedule.contact.wa_jid or schedule.contact.phone
        target_label = (
            schedule.contact.alias
            or schedule.contact.display_name
            or schedule.contact.phone
        )
    elif schedule.target_type == "chat" and schedule.chat is not None:
        target = schedule.chat.wa_jid
        target_label = schedule.chat.name

    template_body = schedule.template.body if schedule.template else ""
    body_template = schedule.message_body_override or template_body
    body = Template(body_template).render(
        contact_name=(schedule.contact.display_name if schedule.contact else ""),
        alias=(schedule.contact.alias if schedule.contact else ""),
        chat_name=(schedule.chat.name if schedule.chat else ""),
    )

    return {
        "to": target,
        "target_label": target_label,
        "target_kind": target_kind,
        "body": body,
        "attachment_path": schedule.attachment_path,
        "attachment_filename": schedule.attachment_filename,
        "attachment_mime": schedule.attachment_mime,
        "schedule_name": schedule.name,
    }


def ensure_schedule_next_run(schedule: Schedule) -> None:
    if schedule.next_run_at is None:
        schedule.next_run_at = compute_next_run(schedule)


def materialize_runs(session: Session, horizon_minutes: int | None = None) -> int:
    now = utcnow()
    horizon_minutes = horizon_minutes or get_settings().materialize_horizon_minutes
    horizon = now + timedelta(minutes=horizon_minutes)

    schedules = (
        session.scalars(
            select(Schedule)
            .options(
                joinedload(Schedule.contact),
                joinedload(Schedule.chat),
                joinedload(Schedule.template),
            )
            .where(
                and_(
                    Schedule.is_active.is_(True),
                    Schedule.is_paused.is_(False),
                    or_(
                        Schedule.next_run_at.is_not(None),
                        Schedule.one_time_at.is_not(None),
                    ),
                )
            )
        )
        .unique()
        .all()
    )

    created = 0
    for schedule in schedules:
        ensure_schedule_next_run(schedule)
        if schedule.next_run_at is None or schedule.next_run_at > horizon:
            if schedule.next_run_at is None and schedule.schedule_type in {
                "one_time",
                "interval",
            }:
                schedule.is_active = False
            continue

        # This key is what keeps materialization idempotent across restarts.
        dedupe_key = f"schedule:{schedule.id}:{schedule.next_run_at.isoformat()}"
        existing = session.scalar(
            select(ScheduledRun).where(ScheduledRun.dedupe_key == dedupe_key)
        )
        if existing is not None:
            continue

        payload = build_run_payload(schedule)
        run = ScheduledRun(
            schedule=schedule,
            run_at=schedule.next_run_at,
            dedupe_key=dedupe_key,
            payload_snapshot=payload,
        )
        session.add(run)
        session.flush()

        session.add(
            OutboundJob(
                scheduled_run=run,
                job_type="send_file" if payload.get("attachment_path") else "send_text",
                status="pending",
                priority=10,
                available_at=run.run_at,
                payload=payload,
            )
        )

        if schedule.schedule_type == "one_time":
            schedule.next_run_at = None
        else:
            # Move the reference forward so recurring schedules do not regenerate the
            # same occurrence on the next worker tick.
            schedule.next_run_at = compute_next_run(
                schedule, reference_utc=run.run_at + timedelta(seconds=1)
            )
            if schedule.schedule_type == "interval" and schedule.next_run_at is None:
                schedule.is_active = False

        created += 1

    session.commit()
    return created


def enqueue_unique_job(session: Session, job_type: str, priority: int = 50) -> bool:
    existing = session.scalar(
        select(OutboundJob).where(
            and_(
                OutboundJob.job_type == job_type,
                OutboundJob.scheduled_run_id.is_(None),
                OutboundJob.status.in_(["pending", "processing"]),
            )
        )
    )
    if existing is not None:
        return False

    session.add(
        OutboundJob(
            job_type=job_type,
            status="pending",
            priority=priority,
            available_at=utcnow(),
            payload=None,
        )
    )
    session.commit()
    return True
