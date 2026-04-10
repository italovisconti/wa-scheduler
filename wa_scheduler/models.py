from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from wa_scheduler.db import Base
from wa_scheduler.timeutil import utcnow


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    wa_jid: Mapped[str | None] = mapped_column(String(255), unique=True)
    phone: Mapped[str | None] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(255), default="")
    alias: Mapped[str] = mapped_column(String(255), default="")
    tags: Mapped[str] = mapped_column(String(255), default="")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    schedules: Mapped[list[Schedule]] = relationship(back_populates="contact")


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(primary_key=True)
    wa_jid: Mapped[str] = mapped_column(String(255), unique=True)
    kind: Mapped[str] = mapped_column(String(32), default="chat")
    name: Mapped[str] = mapped_column(String(255), default="")
    owner_jid: Mapped[str] = mapped_column(String(255), default="")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    schedules: Mapped[list[Schedule]] = relationship(back_populates="chat")


class MessageTemplate(Base):
    __tablename__ = "message_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    schedules: Mapped[list[Schedule]] = relationship(back_populates="template")


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    target_type: Mapped[str] = mapped_column(String(32), default="contact")
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id"))
    chat_id: Mapped[int | None] = mapped_column(ForeignKey("chats.id"))
    template_id: Mapped[int | None] = mapped_column(ForeignKey("message_templates.id"))
    message_body_override: Mapped[str] = mapped_column(Text, default="")
    attachment_path: Mapped[str] = mapped_column(String(512), default="")
    attachment_filename: Mapped[str] = mapped_column(String(255), default="")
    attachment_mime: Mapped[str] = mapped_column(String(255), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    schedule_type: Mapped[str] = mapped_column(String(32), default="one_time")
    one_time_at: Mapped[datetime | None] = mapped_column(DateTime)
    time_of_day: Mapped[str] = mapped_column(String(5), default="")
    weekdays: Mapped[str] = mapped_column(String(32), default="")
    day_of_month: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    contact: Mapped[Contact | None] = relationship(back_populates="schedules")
    chat: Mapped[Chat | None] = relationship(back_populates="schedules")
    template: Mapped[MessageTemplate | None] = relationship(back_populates="schedules")
    runs: Mapped[list[ScheduledRun]] = relationship(back_populates="schedule")


class ScheduledRun(Base):
    __tablename__ = "scheduled_runs"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_scheduled_runs_dedupe_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("schedules.id"))
    run_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    dedupe_key: Mapped[str] = mapped_column(String(255))
    payload_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str] = mapped_column(Text, default="")
    executed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    schedule: Mapped[Schedule] = relationship(back_populates="runs")
    job: Mapped[OutboundJob | None] = relationship(back_populates="scheduled_run")


class OutboundJob(Base):
    __tablename__ = "outbound_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    scheduled_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_runs.id")
    )
    job_type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    raw_command: Mapped[str] = mapped_column(Text, default="")
    stdout_payload: Mapped[str] = mapped_column(Text, default="")
    stderr_payload: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    scheduled_run: Mapped[ScheduledRun | None] = relationship(back_populates="job")
