"""Shared kill filtering, roster statistics, and leaderboard calculations."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from bot.utils import normalize_name

SCOPES = {"all", "day", "week"}
MP_FILTERS = {"all", "gs", "vch", "fura"}


def filter_kills(
    kills: list[dict[str, Any]], scope: str = "all", mp: str = "all"
) -> list[dict[str, Any]]:
    now = datetime.now(UTC).timestamp()
    cutoff = {"day": now - 24 * 60 * 60, "week": now - 7 * 24 * 60 * 60}.get(scope)
    selected_date = None
    if scope not in SCOPES:
        try:
            selected_date = date.fromisoformat(scope)
        except ValueError:
            return []
    result = [
        row
        for row in kills
        if (mp == "all" or row.get("mp", "unknown") == mp)
        and (cutoff is None or int(row.get("ts", 0)) >= cutoff)
        and (selected_date is None or row.get("date") == selected_date.isoformat())
    ]
    return sorted(result, key=lambda row: int(row.get("ts", 0)), reverse=True)


def kill_totals(kills: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    display_names: dict[str, str] = {}
    for row in kills:
        name = str(row.get("killer", {}).get("name", "")).strip()
        if name:
            key = normalize_name(name)
            display_names.setdefault(key, name)
            totals[display_names[key]] = totals.get(display_names[key], 0) + 1
    return totals


def death_totals(kills: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    display_names: dict[str, str] = {}
    for row in kills:
        name = str(row.get("victim", {}).get("name", "")).strip()
        if name:
            key = normalize_name(name)
            display_names.setdefault(key, name)
            totals[display_names[key]] = totals.get(display_names[key], 0) + 1
    return totals


def member_kills(kills: list[dict[str, Any]], nick: str) -> int:
    key = normalize_name(nick)
    return sum(
        1
        for row in kills
        if normalize_name(str(row.get("killer", {}).get("name", ""))) == key
    )


def member_deaths(kills: list[dict[str, Any]], nick: str) -> int:
    key = normalize_name(nick)
    return sum(
        1
        for row in kills
        if normalize_name(str(row.get("victim", {}).get("name", ""))) == key
    )

