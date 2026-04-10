from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from wa_scheduler.config import get_settings
from wa_scheduler.models import OutboundJob, Schedule, ScheduledRun
from wa_scheduler.services.scheduler import enqueue_unique_job, materialize_runs
from wa_scheduler.services.sync import sync_chats, sync_contacts
from wa_scheduler.services.wacli import WacliClient, WacliCommandResult, WacliError
from wa_scheduler.timeutil import utcnow


BACKOFF_STEPS = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15)]


def _command_to_string(result: WacliCommandResult) -> str:
    return " ".join(result.command)


def _mark_job_failure(
    session: Session, job: OutboundJob, error: str, max_attempts: int
) -> None:
    job.last_error = error
    if job.attempt_count >= max_attempts:
        job.status = "failed"
        if job.scheduled_run is not None:
            job.scheduled_run.status = "failed"
            job.scheduled_run.error_message = error
        session.commit()
        return

    retry_offset = BACKOFF_STEPS[min(job.attempt_count - 1, len(BACKOFF_STEPS) - 1)]
    job.status = "pending"
    job.available_at = utcnow() + retry_offset
    if job.scheduled_run is not None:
        job.scheduled_run.status = "pending"
        job.scheduled_run.error_message = error
    session.commit()


def _process_send_job(session: Session, job: OutboundJob, client: WacliClient) -> None:
    payload = job.payload or {}
    to = payload.get("to")
    if not to:
        raise WacliError("Missing destination for send job")

    attachment_path = payload.get("attachment_path") or ""
    if attachment_path:
        # Resolve the attachment right before sending so stale file references fail in
        # a visible, retryable way instead of silently disappearing.
        file_path = Path(attachment_path).expanduser()
        if not file_path.exists():
            raise WacliError(f"Attachment does not exist: {file_path}")
        _, result = client.send_file(
            to=to,
            file_path=str(file_path),
            caption=payload.get("body") or "",
            mime=payload.get("attachment_mime") or "",
            filename=payload.get("attachment_filename") or "",
        )
    else:
        _, result = client.send_text(to=to, message=payload.get("body") or "")

    job.raw_command = _command_to_string(result)
    job.stdout_payload = result.stdout
    job.stderr_payload = result.stderr
    job.status = "done"

    if job.scheduled_run is not None:
        run = job.scheduled_run
        run.status = "sent"
        run.executed_at = utcnow()
        run.error_message = ""
        schedule = run.schedule
        schedule.last_run_at = run.run_at
        if schedule.schedule_type == "one_time":
            schedule.is_active = False

    session.commit()


def _process_maintenance_job(
    session: Session, job: OutboundJob, client: WacliClient
) -> None:
    if job.job_type == "refresh_contacts":
        count = sync_contacts(session, client)
        job.stdout_payload = f"refreshed_contacts={count}"
    elif job.job_type == "refresh_chats":
        count = sync_chats(session, client)
        job.stdout_payload = f"refreshed_chats={count}"
    elif job.job_type == "healthcheck":
        data, result = client.doctor()
        job.raw_command = _command_to_string(result)
        job.stdout_payload = result.stdout
        job.stderr_payload = result.stderr
        job.payload = data
    elif job.job_type == "sync_once":
        _, result = client.sync_once()
        job.raw_command = _command_to_string(result)
        job.stdout_payload = result.stdout
        job.stderr_payload = result.stderr
    else:
        raise WacliError(f"Unsupported maintenance job type: {job.job_type}")

    job.status = "done"
    session.commit()


def process_next_job(session: Session, client: WacliClient) -> bool:
    # wacli serializes access to its store, so the app intentionally executes only
    # one job at a time for a single personal account.
    job = session.scalar(
        select(OutboundJob)
        .options(
            joinedload(OutboundJob.scheduled_run)
            .joinedload(ScheduledRun.schedule)
            .joinedload(Schedule.contact),
            joinedload(OutboundJob.scheduled_run)
            .joinedload(ScheduledRun.schedule)
            .joinedload(Schedule.chat),
            joinedload(OutboundJob.scheduled_run)
            .joinedload(ScheduledRun.schedule)
            .joinedload(Schedule.template),
        )
        .where(OutboundJob.status == "pending", OutboundJob.available_at <= utcnow())
        .order_by(
            OutboundJob.priority.asc(),
            OutboundJob.available_at.asc(),
            OutboundJob.id.asc(),
        )
    )
    if job is None:
        return False

    job.status = "processing"
    job.attempt_count += 1
    if job.scheduled_run is not None:
        job.scheduled_run.status = "processing"
    session.commit()

    try:
        if job.job_type in {"send_text", "send_file"}:
            _process_send_job(session, job, client)
        else:
            _process_maintenance_job(session, job, client)
        return True
    except WacliError as exc:
        _mark_job_failure(session, job, str(exc), get_settings().max_send_attempts)
        return True


def run_worker_loop(session_factory, once: bool = False) -> int:
    settings = get_settings()
    client = WacliClient(settings)
    processed = 0

    print(
        f"Worker started. Polling every {settings.worker_poll_seconds}s.",
        flush=True,
    )

    while True:
        with session_factory() as session:
            # The worker owns materialization so future runs keep being created even if
            # the web UI is never opened.
            materialize_runs(session)
            did_work = process_next_job(session, client)
            processed += 1 if did_work else 0

        if did_work:
            print(f"Processed a job. Total processed: {processed}", flush=True)
        else:
            print("No pending jobs. Sleeping.", flush=True)

        if once:
            break
        time.sleep(settings.worker_poll_seconds)

    return processed


def enqueue_default_sync_jobs(session: Session) -> int:
    created = 0
    for job_type in ["refresh_contacts", "refresh_chats", "healthcheck"]:
        if enqueue_unique_job(session, job_type):
            created += 1
    return created
