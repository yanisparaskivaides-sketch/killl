"""Utilities for importing dated kill logs into the shared kill list."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from typing import Any

from bot.parser import parse_kill_log

MP_ALIASES = {
    "gs": "gs",
    "гс": "gs",
    "vch": "vch",
    "вч": "vch",
    "fura": "fura",
    "фура": "fura",
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def canonical_mp(value: str | None) -> str | None:
    if not value:
        return None
    return MP_ALIASES.get(value.strip().casefold())


def parse_iso_date(value: str | None) -> date | None:
    if not value or not _DATE_RE.fullmatch(value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def parse_import_args(args: str) -> tuple[str, date | None, str]:
    parts = args.strip().split(maxsplit=1)
    mp = canonical_mp(parts[0]) if parts else None
    if mp is None:
        raise ValueError("Укажите MP: `gs`, `vch` или `fura`.")

    rest = parts[1] if len(parts) > 1 else ""
    if not rest:
        return mp, None, ""

    rest_parts = rest.split(maxsplit=1)
    possible_date = rest_parts[0]
    if _DATE_RE.fullmatch(possible_date):
        selected_date = parse_iso_date(possible_date)
        if selected_date is None:
            raise ValueError("Дата должна существовать и быть в формате `ГГГГ-ММ-ДД`.")
        body = rest_parts[1] if len(rest_parts) > 1 else ""
        return mp, selected_date, body

    return mp, None, rest


def parse_kill_lines(
    text: str,
    *,
    mp: str,
    log_date: date,
    source_prefix: str,
) -> list[dict[str, Any]]:
    created_at = datetime.combine(log_date, time.min, tzinfo=UTC)
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        event = parse_kill_log(
            line,
            mp=mp,
            created_at=created_at,
            message_id=f"{source_prefix}:{line_number}",
        )
        if event:
            events.append(event)
    return events
