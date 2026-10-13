"""Schedule helpers and safe defaults for automated kill-list reports."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Any

REPORT_TIMEZONE_NAME = "Europe/Athens"
REPORT_TIMEZONE = ZoneInfo(REPORT_TIMEZONE_NAME)
DEFAULT_REPORT_TIME = "21:00"
DEFAULT_WEEKLY_DAY = 6  # Sunday
DEFAULT_MILESTONE_THRESHOLDS = (10, 25, 50, 100, 250, 500, 1000)

_CLOCK_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def parse_report_time(value: str) -> str | None:
    value = value.strip()
    return value if _CLOCK_RE.fullmatch(value) else None


def parse_milestone_thresholds(values: Any) -> list[int]:
    if not isinstance(values, (list, tuple, set)):
        return []
    valid = {
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 100_000
    }
    return sorted(valid)


def due_reports(
    settings: dict[str, Any], now: datetime | None = None
) -> list[tuple[str, str]]:
    """Return due report kinds with idempotency keys for the current local minute."""
    if not settings.get("reports_enabled") or not settings.get("report_channel_id"):
        return []

    report_time = parse_report_time(str(settings.get("report_time", DEFAULT_REPORT_TIME)))
    if report_time is None:
        return []

    current = now or datetime.now(REPORT_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=REPORT_TIMEZONE)
    else:
        current = current.astimezone(REPORT_TIMEZONE)
    if current.strftime("%H:%M") != report_time:
        return []

    due: list[tuple[str, str]] = []
    day_key = current.date().isoformat()
    if settings.get("last_daily_report") != day_key:
        due.append(("day", day_key))

    try:
        weekly_day = int(settings.get("report_weekday", DEFAULT_WEEKLY_DAY))
    except (TypeError, ValueError):
        weekly_day = DEFAULT_WEEKLY_DAY
    week_key = current.strftime("%G-W%V")
    if (
        current.weekday() == weekly_day
        and settings.get("last_weekly_report") != week_key
    ):
        due.append(("week", week_key))
    return due
