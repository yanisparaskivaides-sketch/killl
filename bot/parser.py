"""Parser for Grand Mobile kill-log messages."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from typing import Any

KILL_LOG_RE = re.compile(
    r'\[?(\d{2}:\d{2}:\d{2})\]\s+(\S+?)\[(\d+)\]\s+убил\s+'
    r'с\s+помощью\s+"([^"]+?)\[(\d+)\]"\s*>\s*(\S+?)\[(\d+)\]',
    re.IGNORECASE,
)


def parse_kill_log(
    message: str,
    *,
    mp: str = "unknown",
    created_at: datetime | None = None,
    message_id: int | str | None = None,
) -> dict[str, Any] | None:
    """Parse one log line; the timestamp uses the Discord message's UTC date."""
    match = KILL_LOG_RE.search(message)
    if not match:
        return None

    logged_time, killer, killer_id, weapon, weapon_id, victim, victim_id = (
        match.groups()
    )
    base_date: date = (
        created_at.astimezone(UTC).date() if created_at else datetime.now(UTC).date()
    )
    parsed_time = time.fromisoformat(logged_time)
    timestamp = datetime.combine(base_date, parsed_time, tzinfo=UTC)
    entry: dict[str, Any] = {
        "time": logged_time,
        "date": base_date.isoformat(),
        "ts": int(timestamp.timestamp()),
        "mp": mp if mp in {"gs", "vch", "fura"} else "unknown",
        "killer": {"name": killer, "id": int(killer_id)},
        "weapon": {"name": weapon, "id": int(weapon_id)},
        "victim": {"name": victim, "id": int(victim_id)},
    }
    if message_id is not None:
        entry["source_message_id"] = str(message_id)
    return entry

