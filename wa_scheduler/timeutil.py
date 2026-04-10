from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def parse_hhmm(value: str | None) -> time | None:
    if not value:
        return None
    return time.fromisoformat(value)


def local_to_utc_naive(local_dt: datetime, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=tz)
    return local_dt.astimezone(UTC).replace(tzinfo=None)


def utc_naive_to_local(utc_dt: datetime, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    return utc_dt.replace(tzinfo=UTC).astimezone(tz)


def next_daily_occurrence(
    reference_utc: datetime, timezone_name: str, at_time: time
) -> datetime:
    local_reference = utc_naive_to_local(reference_utc, timezone_name)
    candidate = local_reference.replace(
        hour=at_time.hour,
        minute=at_time.minute,
        second=0,
        microsecond=0,
    )
    if candidate <= local_reference:
        candidate += timedelta(days=1)
    return local_to_utc_naive(candidate, timezone_name)


def next_weekly_occurrence(
    reference_utc: datetime, timezone_name: str, at_time: time, weekdays: list[int]
) -> datetime | None:
    if not weekdays:
        return None

    local_reference = utc_naive_to_local(reference_utc, timezone_name)
    base = local_reference.replace(second=0, microsecond=0)

    for offset in range(0, 14):
        candidate_date = (base + timedelta(days=offset)).date()
        if candidate_date.weekday() not in weekdays:
            continue
        candidate = datetime.combine(candidate_date, at_time, tzinfo=base.tzinfo)
        if candidate > local_reference:
            return local_to_utc_naive(candidate, timezone_name)
    return None


def next_monthly_occurrence(
    reference_utc: datetime, timezone_name: str, at_time: time, day_of_month: int
) -> datetime | None:
    if day_of_month < 1 or day_of_month > 31:
        return None

    local_reference = utc_naive_to_local(reference_utc, timezone_name)
    year = local_reference.year
    month = local_reference.month

    for _ in range(0, 14):
        try:
            candidate = datetime(
                year,
                month,
                day_of_month,
                at_time.hour,
                at_time.minute,
                tzinfo=local_reference.tzinfo,
            )
        except ValueError:
            candidate = None

        if candidate and candidate > local_reference:
            return local_to_utc_naive(candidate, timezone_name)

        month += 1
        if month > 12:
            month = 1
            year += 1

    return None
