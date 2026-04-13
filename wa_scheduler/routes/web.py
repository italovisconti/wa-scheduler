from __future__ import annotations

import mimetypes
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from wa_scheduler.config import get_settings
from wa_scheduler.db import get_session
from wa_scheduler.models import (
    AppSetting,
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


def _friendly_timezone_name(timezone_name: str) -> str:
    if not timezone_name or timezone_name == "None":
        return "UTC"
    mapping = {
        "UTC": "UTC",
        "America/Caracas": "Caracas",
        "America/New_York": "New York",
        "America/Mexico_City": "Mexico City",
        "America/Los_Angeles": "Los Angeles",
        "Europe/London": "London",
        "Europe/Madrid": "Madrid",
    }
    label = mapping.get(timezone_name, timezone_name.replace("_", " "))
    if timezone_name == "UTC":
        return label
    try:
        offset = datetime.now(ZoneInfo(timezone_name)).strftime("%z")
        offset = f"UTC{offset[:3]}:{offset[3:]}"
        return f"{label} ({offset})"
    except Exception:
        return label


def _format_interval_minutes(
    minutes: int | None, legacy_hours: int | None = None
) -> str:
    total_minutes = minutes if minutes is not None else (legacy_hours or 1) * 60
    if total_minutes % 60 == 0:
        hours = total_minutes // 60
        return f"{hours} h"
    return f"{total_minutes} min"


def _app_timezone(session: Session) -> str:
    setting = session.get(AppSetting, "timezone")
    settings = get_settings()
    candidate = (
        setting.value if setting and setting.value else settings.default_timezone
    ) or "UTC"
    if candidate == "None":
        candidate = settings.default_timezone or "UTC"
    try:
        ZoneInfo(candidate)
    except Exception:
        candidate = settings.default_timezone or "UTC"
    return candidate


def _timezones(app_timezone: str) -> list[str]:
    settings = get_settings()
    values = [
        app_timezone,
        settings.default_timezone,
        "America/Caracas",
        "UTC",
        "America/New_York",
        "America/Mexico_City",
        "America/Los_Angeles",
        "Europe/London",
        "Europe/Madrid",
    ]
    return [value for value in dict.fromkeys(values) if value]


def _schedule_target_options(
    contacts: list[Contact], chats: list[Chat]
) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for contact in contacts:
        display = (
            contact.alias
            or contact.display_name
            or contact.phone
            or contact.wa_jid
            or f"Contact {contact.id}"
        )
        label = f"Contact: {display}"
        search = " ".join(
            value
            for value in [
                label,
                contact.alias or "",
                contact.display_name or "",
                contact.phone or "",
                contact.wa_jid or "",
                contact.tags or "",
            ]
            if value
        ).lower()
        options.append(
            {"value": f"contact:{contact.id}", "label": label, "search": search}
        )

    for chat in chats:
        display = chat.name or chat.wa_jid or f"Chat {chat.id}"
        label = f"Chat: {display}"
        search = " ".join(
            value for value in [label, chat.name or "", chat.wa_jid or ""] if value
        ).lower()
        options.append({"value": f"chat:{chat.id}", "label": label, "search": search})

    return options


def _schedule_target_ref(schedule: Schedule) -> str:
    if schedule.target_type == "contact" and schedule.contact_id is not None:
        return f"contact:{schedule.contact_id}"
    if schedule.target_type == "chat" and schedule.chat_id is not None:
        return f"chat:{schedule.chat_id}"
    return ""


def _local_input_value(utc_dt: datetime | None, timezone_name: str) -> str:
    if utc_dt is None:
        return ""
    return utc_naive_to_local(utc_dt, timezone_name).strftime("%Y-%m-%dT%H:%M")


def _recurring_default_values(app_timezone: str) -> dict[str, str]:
    now_utc = utcnow()
    return {
        "default_start_at": _local_input_value(now_utc, app_timezone),
        "default_until_at": _local_input_value(
            now_utc + timedelta(days=1), app_timezone
        ),
    }


def _ui_context(session: Session) -> dict:
    app_timezone = _app_timezone(session)
    return {
        "app_timezone": app_timezone,
        "timezones": _timezones(app_timezone),
        "timezone_label": _friendly_timezone_name,
        "format_interval_minutes": _format_interval_minutes,
    }


def _set_app_timezone(session: Session, timezone_name: str) -> None:
    setting = session.get(AppSetting, "timezone")
    if setting is None:
        setting = AppSetting(key="timezone", value=timezone_name)
        session.add(setting)
    else:
        setting.value = timezone_name


def _validate_timezone(timezone_name: str) -> str:
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid timezone") from exc
    return timezone_name


def _schedule_form_context(session: Session, selected_target_ref: str = "") -> dict:
    app_timezone = _app_timezone(session)
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
    target_options = _schedule_target_options(contacts, chats)
    items = session.scalars(
        select(MessageTemplate).order_by(MessageTemplate.name.asc())
    ).all()
    return {
        "schedules": schedules,
        "contacts": contacts,
        "chats": chats,
        "templates_list": items,
        "target_options": target_options,
        "selected_target_ref": selected_target_ref,
        "target_label": _target_label,
        "app_timezone": app_timezone,
        "format_interval_minutes": _format_interval_minutes,
        "to_local": utc_naive_to_local,
        "timezones": _timezones(app_timezone),
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
    interval_value: int,
    interval_unit: str,
    repeat_until_at: str,
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
        schedule.interval_minutes = None
        schedule.interval_hours = None
        schedule.repeat_until_at = None
    elif schedule_type == "interval":
        if not one_time_at:
            raise HTTPException(status_code=400, detail="start_at is required")
        unit = interval_unit.strip().lower()
        if unit not in {"minutes", "hours"}:
            raise HTTPException(status_code=400, detail="Invalid interval unit")
        if unit == "minutes":
            if interval_value < 5:
                raise HTTPException(
                    status_code=400, detail="interval must be at least 5 minutes"
                )
            interval_minutes = interval_value
        else:
            if interval_value < 1:
                raise HTTPException(
                    status_code=400, detail="interval must be at least 1 hour"
                )
            interval_minutes = interval_value * 60
        if not repeat_until_at:
            raise HTTPException(status_code=400, detail="repeat_until_at is required")
        start_dt = datetime.fromisoformat(one_time_at)
        until_dt = datetime.fromisoformat(repeat_until_at)
        start_utc = local_to_utc_naive(start_dt, timezone)
        until_utc = local_to_utc_naive(until_dt, timezone)
        max_horizon = utcnow() + timedelta(days=3)
        if until_utc > max_horizon:
            raise HTTPException(
                status_code=400,
                detail="repeat_until_at cannot be more than 3 days from now",
            )
        if start_utc > until_utc:
            raise HTTPException(
                status_code=400,
                detail="start_at must be before repeat_until_at",
            )
        schedule.one_time_at = start_utc
        schedule.interval_minutes = interval_minutes
        schedule.interval_hours = None
        schedule.repeat_until_at = until_utc
        schedule.time_of_day = ""
        schedule.weekdays = ""
        schedule.day_of_month = None
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
        schedule.interval_minutes = None
        schedule.interval_hours = None
        schedule.repeat_until_at = None

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
            **_ui_context(session),
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
            **_ui_context(session),
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
        return _redirect("/", f"Could not send the message: {exc}")

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
            **_ui_context(session),
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
    return _redirect("/contacts", "Contact updated.")


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
            **_ui_context(session),
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
            **_ui_context(session),
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
            **_ui_context(session),
        },
    )


@router.get("/settings")
def settings_page(request: Request, session: Session = Depends(get_session)):
    app_timezone = _app_timezone(session)
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "notice": request.query_params.get("notice", ""),
            "app_timezone": app_timezone,
            "timezones": _timezones(app_timezone),
            **_ui_context(session),
        },
    )


@router.post("/settings/timezone")
def update_timezone(
    timezone: str = Form(...),
    apply_existing: bool = Form(default=False),
    session: Session = Depends(get_session),
):
    timezone = _validate_timezone(timezone)
    _set_app_timezone(session, timezone)

    if apply_existing:
        schedules = session.scalars(select(Schedule)).all()
        for schedule in schedules:
            schedule.timezone = timezone
            if schedule.schedule_type != "one_time":
                schedule.next_run_at = compute_next_run(schedule)

    session.commit()
    return _redirect("/settings", "Timezone updated.")


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
            **_schedule_form_context(
                session, selected_target_ref=request.query_params.get("target_ref", "")
            ),
            **_ui_context(session),
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
            **_schedule_form_context(
                session, selected_target_ref=_schedule_target_ref(schedule)
            ),
            **_ui_context(session),
        },
    )


@router.get("/recurring")
def recurring_page(request: Request, session: Session = Depends(get_session)):
    app_timezone = _app_timezone(session)
    interval_schedules = (
        session.scalars(
            select(Schedule)
            .options(
                joinedload(Schedule.contact),
                joinedload(Schedule.chat),
                joinedload(Schedule.template),
            )
            .where(Schedule.schedule_type == "interval")
            .order_by(Schedule.created_at.desc())
        )
        .unique()
        .all()
    )
    return templates.TemplateResponse(
        request=request,
        name="recurring.html",
        context={
            "notice": request.query_params.get("notice", ""),
            "interval_schedules": interval_schedules,
            **_schedule_form_context(
                session, selected_target_ref=request.query_params.get("target_ref", "")
            ),
            **_ui_context(session),
            **_recurring_default_values(app_timezone),
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
    interval_value: int = Form(default=1),
    interval_unit: str = Form(default="hours"),
    repeat_until_at: str = Form(default=""),
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
        interval_value=interval_value,
        interval_unit=interval_unit,
        repeat_until_at=repeat_until_at,
    )
    session.add(schedule)
    session.commit()
    return _redirect("/schedules", "Schedule created.")


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
    interval_value: int = Form(default=1),
    interval_unit: str = Form(default="hours"),
    repeat_until_at: str = Form(default=""),
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
        interval_value=interval_value,
        interval_unit=interval_unit,
        repeat_until_at=repeat_until_at,
    )
    session.commit()
    return _redirect("/schedules", "Schedule updated.")


@router.post("/recurring")
def create_recurring_schedule(
    name: str = Form(...),
    target_ref: str = Form(...),
    template_id: int = Form(default=0),
    message_body_override: str = Form(default=""),
    timezone: str = Form(default="UTC"),
    interval_value: int = Form(...),
    interval_unit: str = Form(...),
    one_time_at: str = Form(default=""),
    repeat_until_at: str = Form(...),
    session: Session = Depends(get_session),
):
    schedule = Schedule(is_active=True, is_paused=False, schedule_type="interval")

    _apply_schedule_form(
        schedule=schedule,
        session=session,
        name=name,
        target_ref=target_ref,
        template_id=template_id,
        message_body_override=message_body_override,
        attachment_path="",
        attachment_filename="",
        attachment_mime="",
        attachment_upload=None,
        clear_attachment=False,
        timezone=timezone,
        schedule_type="interval",
        one_time_at=one_time_at,
        time_of_day="",
        weekdays=None,
        day_of_month=1,
        interval_value=interval_value,
        interval_unit=interval_unit,
        repeat_until_at=repeat_until_at,
    )
    if schedule.next_run_at is None:
        raise HTTPException(
            status_code=400,
            detail="The selected range does not produce any future run",
        )
    session.add(schedule)
    session.commit()
    return _redirect("/recurring", "Recurring schedule created.")


@router.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: int, session: Session = Depends(get_session)):
    schedule = session.get(Schedule, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="Schedule not found")
    schedule.is_paused = not schedule.is_paused
    session.commit()
    return _redirect("/schedules", "Schedule updated.")


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
            **_ui_context(session),
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
    return _redirect("/runs", "Run requeued.")


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
