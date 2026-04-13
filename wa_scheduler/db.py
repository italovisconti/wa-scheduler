from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from wa_scheduler.config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()

connect_args = (
    {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
)

engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)


def get_session() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    from wa_scheduler import models  # noqa: F401

    Base.metadata.create_all(bind=engine)

    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as conn:
        columns = {column["name"] for column in inspect(conn).get_columns("schedules")}
        if "interval_hours" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE schedules ADD COLUMN interval_hours INTEGER DEFAULT 1"
            )
        if "interval_minutes" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE schedules ADD COLUMN interval_minutes INTEGER DEFAULT 5"
            )
        if "repeat_until_at" not in columns:
            conn.exec_driver_sql(
                "ALTER TABLE schedules ADD COLUMN repeat_until_at DATETIME"
            )
