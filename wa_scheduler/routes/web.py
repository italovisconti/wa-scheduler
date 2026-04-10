from __future__ import annotations

import mimetypes
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from wa_scheduler.config import get_settings
from wa_scheduler.db import get_session
from wa_scheduler.models import (
    Chat,
    Contact,
    MessageTemplate,
    OutboundJob,
    Schedule,
    ScheduledRun,
)
from wa_scheduler.services.health import build_health_payload
from wa_scheduler.services.scheduler import (
    compute_next_run,
    enqueue_unique_job,
    materialize_runs,
)
from wa_scheduler.services.wacli import WacliClient, WacliError
from wa_scheduler.timeutil import local_to_utc_naive, utc_naive_to_local, utcnow


router = APIRouter()
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parents[1] / "templates")
)


def _redirect(url: str, notice: str = "") -> RedirectResponse:
    suffix = f"?notice={quote(notice)}" if notice else ""
    return RedirectResponse(url=f"{url}{suffix}", status_code=303)


def _target_label(schedule: Schedule) -> str:
    if schedule.target_type == "contact" and schedule.contact is not None:
        return (
            schedule.contact.alias
            or schedule.contact.display_name
            or schedule.contact.phone
            or schedule.contact.wa_jid
            or "-"
        )
    if schedule.target_type == "chat" and schedule.chat is not None:
        return schedule.chat.name or schedule.chat.wa_jid
    return "-"


def _timezones() -> list[str]:
    settings = get_settings()
    values = [settings.default_timezone, "UTC", "America/Caracas"]
    return list(dict.fromkeys(values))


def _schedule_form_context(session: Session) -> dict:
    schedules = (
        session.scalars(
            select(Schedule)
            .options(
                joinedload(Schedule.contact),
                joinedload(Schedule.chat),
                joinedload(Schedule.template),
            )
            .order_by(Schedule.created_at.desc())
        )
        .unique()
        .all()
    )
    contacts = session.scalars(
        select(Contact).order_by(Contact.display_name.asc(), Contact.phone.asc())
    ).all()
    chats = session.scalars(select(Chat).order_by(Chat.name.asc())).all()
    items = session.scalars(
        select(MessageTemplate).order_by(MessageTemplate.name.asc())
    ).all()
    return {
        "schedules": schedules,
        "contacts": contacts,
        "chats": chats,
        "templates_list": items,
        "target_label": _target_label,
        "to_local": utc_naive_to_local,
        "timezones": _timezones(),
    }


def _save_uploaded_attachment(upload: UploadFile | None) -> tuple[str, str, str]:
    if upload is None or not upload.filename:
        return "", "", ""

    settings = get_settings()
    original_name = Path(upload.filename).name
    suffix = Path(original_name).suffix
    stored_name = f"{uuid4().hex}{suffix}"
    stored_path = settings.data_dir / "attachments" / stored_name

    # Uploaded attachments are copied into the app data volume so the worker can still
    # access them later even when the original browser upload is long gone.
    with stored_path.open("wb") as target:
        while chunk := upload.file.read(1024 * 1024):
            target.write(chunk)

    detected_mime = upload.content_type or mimetypes.guess_type(original_name)[0] or ""
    return str(stored_path), original_name, detected_mime


def _resolve_attachment_fields(
    *,
    uploaded_file: UploadFile | None,
    manual_path: str,
    manual_filename: str,
    manual_mime: str,
    existing_path: str = "",
    existing_filename: str = "",
    existing_mime: str = "",
    clear_attachment: bool = False,
) -> tuple[str, str, str]:
    if clear_attachment:
        return "", "", ""

    upload_path, upload_filename, upload_mime = _save_uploaded_attachment(uploaded_file)
    if upload_path:
        return upload_path, upload_filename, upload_mime

    manual_path = manual_path.strip()
    manual_filename = manual_filename.strip()
    manual_mime = manual_mime.strip()
    if manual_path:
        filename = manual_filename or Path(manual_path).name
        mime = manual_mime or mimetypes.guess_type(filename)[0] or ""
        return manual_path, filename, mime

    return existing_path, existing_filename, existing_mime


def _apply_schedule_form(
    *,
    schedule: Schedule,
    session: Session,
    name: str,
    target_ref: str,
    template_id: int,
    message_body_override: str,
    attachment_path: str,
    attachment_filename: str,
    attachment_mime: str,
    attachment_upload: UploadFile | None,
    clear_attachment: bool,
    timezone: str,
    schedule_type: str,
    one_time_at: str,
    time_of_day: str,
    weekdays: list[str] | None,
    day_of_month: int,
) -> None:
    try:
        target_type, raw_target_id = target_ref.split(":", 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid target") from exc

    schedule.name = name.strip()
    schedule.target_type = target_type
    schedule.template_id = template_id or None
    schedule.message_body_override = message_body_override.strip()
    schedule.timezone = timezone
    schedule.schedule_type = schedule_type
    schedule.time_of_day = time_of_day.strip()
    schedule.weekdays = ",".join(sorted(weekdays or []))
    schedule.day_of_month = day_of_month or None
    (
        schedule.attachment_path,
        schedule.attachment_filename,
        schedule.attachment_mime,
    ) = _resolve_attachment_fields(
        uploaded_file=attachment_upload,
        manual_path=attachment_path,
        manual_filename=attachment_filename,
        manual_mime=attachment_mime,
        existing_path=schedule.attachment_path or "",
        existing_filename=schedule.attachment_filename or "",
        existing_mime=schedule.attachment_mime or "",
        clear_attachment=clear_attachment,
    )

    target_id = int(raw_target_id)
    schedule.contact_id = None
    schedule.chat_id = None
    if target_type == "contact":
        if session.get(Contact, target_id) is None:
            raise HTTPException(status_code=404, detail="Target contact not found")
        schedule.contact_id = target_id
    elif target_type == "chat":
        if session.get(Chat, target_id) is None:
            raise HTTPException(status_code=404, detail="Target chat not found")
        schedule.chat_id = target_id
    else:
        raise HTTPException(status_code=400, detail="Unsupported target type")

    if schedule_type == "one_time":
        if not one_time_at:
            raise HTTPException(status_code=400, detail="one_time_at is required")
        local_dt = datetime.fromisoformat(one_time_at)
        schedule.one_time_at = local_to_utc_naive(local_dt, timezone)
    else:
        if not time_of_day:
            raise HTTPException(
                status_code=400,
                detail="time_of_day is required for recurring schedules",
            )
        if schedule_type == "weekly" and not weekdays:
            raise HTTPException(status_code=400, detail="Select at least one weekday")
        if schedule_type == "monthly" and not day_of_month:
            raise HTTPException(status_code=400, detail="day_of_month is required")
        schedule.one_time_at = None

    schedule.next_run_at = compute_next_run(schedule)


@router.get("/")
def dashboard(request: Request, session: Session = Depends(get_session)):
    health_payload = build_health_payload(session)
    contacts = session.scalars(
        select(Contact).order_by(Contact.display_name.asc(), Contact.phone.asc())
    ).all()
    chats = session.scalars(select(Chat).order_by(Chat.name.asc())).all()

    recent_runs = (
        session.scalars(
            select(ScheduledRun)
            .options(
                joinedload(ScheduledRun.schedule).joinedload(Schedule.contact),
                joinedload(ScheduledRun.schedule).joinedload(Schedule.chat),
            )
            .order_by(ScheduledRun.run_at.desc())
            .limit(10)
        )
        .unique()
        .all()
    )

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "notice": request.query_params.get("notice", ""),
            "stats": {
                "contacts": health_payload["counts"]["contacts"],
                "chats": health_payload["counts"]["chats"],
                "schedules": health_payload["counts"]["schedules"],
                "pending_jobs": health_payload["counts"]["jobs_pending"],
            },
            "health": health_payload["wacli"],
            "health_error": health_payload["error"],
            "contacts": contacts,
            "chats": chats,
            "recent_runs": recent_runs,
            "target_label": _target_label,
            "to_local": utc_naive_to_local,
            "now": utcnow(),
        },
    )


@router.get("/send-now")
def send_now_page(request: Request, session: Session = Depends(get_session)):
    contacts = session.scalars(
        select(Contact).order_by(Contact.display_name.asc(), Contact.phone.asc())
    ).all()
    chats = session.scalars(select(Chat).order_by(Chat.name.asc())).all()
    return templates.TemplateResponse(
        request=request,
        name="send_now.html",
        context={
            "notice": request.query_params.get("notice", ""),
            "contacts": contacts,
            "chats": chats,
            "selected_target_ref": request.query_params.get("target_ref", ""),
        },
    )


@router.post("/send-now")
def send_now(
    target_ref: str = Form(...),
    message: str = Form(...),
    attachment_upload: UploadFile | None = File(default=None),
    session: Session = Depends(get_session),
):
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    try:
        target_type, raw_target_id = target_ref.split(":", 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid target") from exc

    target_id = int(raw_target_id)
    if target_type == "contact":
        contact = session.get(Contact, target_id)
        if contact is None:
            raise HTTPException(status_code=404, detail="Target contact not found")
        destination = contact.wa_jid or contact.phone
    elif target_type == "chat":
        chat = session.get(Chat, target_id)
        if chat is None:
            raise HTTPException(status_code=404, detail="Target chat not found")
        destination = chat.wa_jid
    else:
        raise HTTPException(status_code=400, detail="Unsupported target type")

    if not destination:
        raise HTTPException(status_code=400, detail="Destination is missing")

    client = WacliClient()
    attachment_path = ""
    attachment_filename = ""
    attachment_mime = ""
    if attachment_upload is not None and attachment_upload.filename:
        attachment_path, attachment_filename, attachment_mime = (
            _save_uploaded_attachment(attachment_upload)
        )

    job = OutboundJob(
        job_type="send_file" if attachment_path else "send_text",
        status="processing",
        priority=0,
        available_at=utcnow(),
        attempt_count=1,
        payload={
            "to": destination,
            "target_ref": target_ref,
            "body": message,
            "attachment_path": attachment_path,
            "attachment_filename": attachment_filename,
            "attachment_mime": attachment_mime,
        },
    )
    session.add(job)
    session.commit()

    try:
        if attachment_path:
            _, result = client.send_file(
                to=destination,
                file_path=attachment_path,
                caption=message,
                mime=attachment_mime,
                filename=attachment_filename,
            )
        else:
            _, result = client.send_text(to=destination, message=message)

        job.raw_command = " ".join(result.command)
        job.stdout_payload = result.stdout
        job.stderr_payload = result.stderr
        job.status = "done"
        session.commit()
    except WacliError as exc:
        job.last_error = str(exc)
        job.status = "failed"
        session.commit()
        return _redirect("/", f"No se pudo enviar el mensaje: {exc}")

    return _redirect("/", "Mensaje enviado correctamente.")


@router.get("/contacts")
def contacts_page(
    request: Request,
    q: str = "",
    session: Session = Depends(get_session),
):
    query = q.strip()
    stmt = select(Contact)
    if query:
        pattern = f"%{query.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Contact.display_name).like(pattern),
                func.lower(Contact.alias).like(pattern),
                func.lower(Contact.phone).like(pattern),
                func.lower(Contact.wa_jid).like(pattern),
                func.lower(Contact.tags).like(pattern),
            )
        )
    contacts = session.scalars(
        stmt.order_by(Contact.display_name.asc(), Contact.phone.asc())
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="contacts.html",
        context={
            "notice": request.query_params.get("notice", ""),
            "contacts": contacts,
            "q": query,
        },
    )


@router.post("/contacts/{contact_id}")
def update_contact(
    contact_id: int,
    alias: str = Form(default=""),
    tags: str = Form(default=""),
    session: Session = Depends(get_session),
):
    contact = session.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    contact.alias = alias.strip()
    contact.tags = tags.strip()
    session.commit()
    return _redirect("/contacts", "Contacto actualizado.")


@router.get("/chats")
def chats_page(request: Request, session: Session = Depends(get_session)):
    chats = session.scalars(select(Chat).order_by(Chat.updated_at.desc())).all()
    return templates.TemplateResponse(
        request=request,
        name="chats.html",
        context={
            "notice": request.query_params.get("notice", ""),
            "chats": chats,
            "to_local": utc_naive_to_local,
        },
    )


@router.get("/templates")
def templates_page(request: Request, session: Session = Depends(get_session)):
    items = session.scalars(
        select(MessageTemplate).order_by(MessageTemplate.updated_at.desc())
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="templates.html",
        context={
            "notice": request.query_params.get("notice", ""),
            "templates_list": items,
        },
    )


@router.get("/templates/{template_id}/edit")
def edit_template_page(
    template_id: int, request: Request, session: Session = Depends(get_session)
):
    template = session.get(MessageTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    return templates.TemplateResponse(
        request=request,
        name="template_edit.html",
        context={
            "notice": request.query_params.get("notice", ""),
            "template": template,
        },
    )


@router.post("/templates")
def create_template(
    name: str = Form(...),
    body: str = Form(...),
    session: Session = Depends(get_session),
):
    template = MessageTemplate(name=name.strip(), body=body.strip())
    session.add(template)
    session.commit()
    return _redirect("/templates", "Plantilla creada.")


@router.post("/templates/{template_id}")
def update_template(
    template_id: int,
    name: str = Form(...),
    body: str = Form(...),
    session: Session = Depends(get_session),
):
    template = session.get(MessageTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    template.name = name.strip()
    template.body = body.strip()
    session.commit()
    return _redirect("/templates", "Plantilla actualizada.")


@router.post("/templates/{template_id}/delete")
def delete_template(template_id: int, session: Session = Depends(get_session)):
    template = session.get(MessageTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="Template not found")
    in_use = session.scalar(
        select(Schedule).where(Schedule.template_id == template_id).limit(1)
    )
    if in_use is not None:
        return _redirect(
            "/templates",
            "No se puede borrar la plantilla porque aun esta asignada a un schedule.",
        )
    session.delete(template)
    session.commit()
    return _redirect("/templates", "Plantilla eliminada.")


@router.get("/schedules")
def schedules_page(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request=request,
        name="schedules.html",
        context={
            "notice": request.query_params.get("notice", ""),
            **_schedule_form_context(session),
        },
    )


@router.get("/schedules/{schedule_id}/edit")
def edit_schedule_page(
    schedule_id: int, request: Request, session: Session = Depends(get_session)
):
    schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return templates.TemplateResponse(
        request=request,
        name="schedule_edit.html",
        context={
            "notice": request.query_params.get("notice", ""),
            "schedule": schedule,
            **_schedule_form_context(session),
        },
    )


@router.post("/schedules")
def create_schedule(
    name: str = Form(...),
    target_ref: str = Form(...),
    template_id: int = Form(default=0),
    message_body_override: str = Form(default=""),
    attachment_path: str = Form(default=""),
    attachment_filename: str = Form(default=""),
    attachment_mime: str = Form(default=""),
    attachment_upload: UploadFile | None = File(default=None),
    timezone: str = Form(default="UTC"),
    schedule_type: str = Form(default="one_time"),
    one_time_at: str = Form(default=""),
    time_of_day: str = Form(default=""),
    weekdays: list[str] | None = Form(default=None),
    day_of_month: int = Form(default=1),
    clear_attachment: bool = Form(default=False),
    session: Session = Depends(get_session),
):
    schedule = Schedule(is_active=True, is_paused=False)
    _apply_schedule_form(
        schedule=schedule,
        session=session,
        name=name,
        target_ref=target_ref,
        template_id=template_id,
        message_body_override=message_body_override,
        attachment_path=attachment_path,
        attachment_filename=attachment_filename,
        attachment_mime=attachment_mime,
        attachment_upload=attachment_upload,
        clear_attachment=clear_attachment,
        timezone=timezone,
        schedule_type=schedule_type,
        one_time_at=one_time_at,
        time_of_day=time_of_day,
        weekdays=weekdays,
        day_of_month=day_of_month,
    )
    session.add(schedule)
    session.commit()
    return _redirect("/schedules", "Schedule creado.")


@router.post("/schedules/{schedule_id}")
def update_schedule(
    schedule_id: int,
    name: str = Form(...),
    target_ref: str = Form(...),
    template_id: int = Form(default=0),
    message_body_override: str = Form(default=""),
    attachment_path: str = Form(default=""),
    attachment_filename: str = Form(default=""),
    attachment_mime: str = Form(default=""),
    attachment_upload: UploadFile | None = File(default=None),
    timezone: str = Form(default="UTC"),
    schedule_type: str = Form(default="one_time"),
    one_time_at: str = Form(default=""),
    time_of_day: str = Form(default=""),
    weekdays: list[str] | None = Form(default=None),
    day_of_month: int = Form(default=1),
    clear_attachment: bool = Form(default=False),
    session: Session = Depends(get_session),
):
    schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    _apply_schedule_form(
        schedule=schedule,
        session=session,
        name=name,
        target_ref=target_ref,
        template_id=template_id,
        message_body_override=message_body_override,
        attachment_path=attachment_path,
        attachment_filename=attachment_filename,
        attachment_mime=attachment_mime,
        attachment_upload=attachment_upload,
        clear_attachment=clear_attachment,
        timezone=timezone,
        schedule_type=schedule_type,
        one_time_at=one_time_at,
        time_of_day=time_of_day,
        weekdays=weekdays,
        day_of_month=day_of_month,
    )
    session.commit()
    return _redirect("/schedules", "Schedule actualizado.")


@router.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: int, session: Session = Depends(get_session)):
    schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    schedule.is_paused = not schedule.is_paused
    session.commit()
    return _redirect("/schedules", "Schedule actualizado.")


@router.post("/schedules/{schedule_id}/delete")
def delete_schedule(schedule_id: int, session: Session = Depends(get_session)):
    schedule = session.scalar(
        select(Schedule)
        .where(Schedule.id == schedule_id)
        .options(joinedload(Schedule.runs).joinedload(ScheduledRun.job))
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")

    for run in schedule.runs:
        if run.job is not None:
            session.delete(run.job)
        session.delete(run)
    session.delete(schedule)
    session.commit()
    return _redirect("/schedules", "Schedule eliminado.")


@router.get("/runs")
def runs_page(request: Request, session: Session = Depends(get_session)):
    runs = (
        session.scalars(
            select(ScheduledRun)
            .options(
                joinedload(ScheduledRun.schedule).joinedload(Schedule.contact),
                joinedload(ScheduledRun.schedule).joinedload(Schedule.chat),
                joinedload(ScheduledRun.job),
            )
            .order_by(ScheduledRun.run_at.desc())
            .limit(100)
        )
        .unique()
        .all()
    )
    direct_jobs = session.scalars(
        select(OutboundJob)
        .where(OutboundJob.scheduled_run_id.is_(None))
        .order_by(OutboundJob.created_at.desc())
        .limit(50)
    ).all()
    return templates.TemplateResponse(
        request=request,
        name="runs.html",
        context={
            "notice": request.query_params.get("notice", ""),
            "runs": runs,
            "direct_jobs": direct_jobs,
            "target_label": _target_label,
            "to_local": utc_naive_to_local,
        },
    )


@router.post("/runs/{run_id}/retry")
def retry_run(run_id: int, session: Session = Depends(get_session)):
    run = session.scalar(
        select(ScheduledRun)
        .where(ScheduledRun.id == run_id)
        .options(joinedload(ScheduledRun.job))
    )
    if run is None or run.job is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run.status = "pending"
    run.error_message = ""
    run.job.status = "pending"
    run.job.available_at = utcnow()
    run.job.last_error = ""
    session.commit()
    return _redirect("/runs", "Run reencolado.")


@router.post("/maintenance/enqueue-sync")
def enqueue_sync_jobs(session: Session = Depends(get_session)):
    created = 0
    for job_type in ["sync_once", "refresh_contacts", "refresh_chats", "healthcheck"]:
        if enqueue_unique_job(session, job_type, priority=50):
            created += 1
    return _redirect("/", f"Jobs de mantenimiento agregados: {created}.")


@router.post("/maintenance/materialize")
def materialize_jobs(session: Session = Depends(get_session)):
    created = materialize_runs(session)
    return _redirect("/", f"Runs materializados: {created}.")


@router.get("/api/health")
def api_health(session: Session = Depends(get_session)):
    payload = build_health_payload(session)
    if not payload["ok"]:
        return JSONResponse(status_code=503, content=payload)
    return payload
