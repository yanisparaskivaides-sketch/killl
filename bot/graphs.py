"""Matplotlib charts with a short-lived in-memory PNG cache."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import threading
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)
_CACHE_TTL = 10 * 60
_CACHE: dict[str, tuple[float, bytes]] = {}
_PLOT_LOCK = threading.Lock()


def _cache_key(kind: str, kills: list[dict[str, Any]]) -> str:
    serialised = json.dumps(kills, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialised.encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def _render(kind: str, kills: list[dict[str, Any]]) -> bytes:
    with _PLOT_LOCK:
        key = _cache_key(kind, kills)
        cached = _CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        figure, axis = plt.subplots(figsize=(10, 5), constrained_layout=True)
        try:
            if kind == "kills":
                start = datetime.now(UTC).date() - timedelta(days=13)
                counts: Counter[str] = Counter()
                for row in kills:
                    try:
                        day = datetime.fromtimestamp(int(row.get("ts", 0)), UTC).date()
                    except (ValueError, TypeError, OSError):
                        continue
                    if start <= day <= datetime.now(UTC).date():
                        counts[day.isoformat()] += 1
                days = [start + timedelta(days=index) for index in range(14)]
                axis.bar(
                    [day.strftime("%d %b") for day in days],
                    [counts[day.isoformat()] for day in days],
                    color="#ee425b",
                )
                axis.set_title("Убийства за последние 14 дней")
                axis.set_ylabel("Киллы")
                axis.tick_params(axis="x", rotation=35)
            elif kind == "weapons":
                counts = Counter(
                    str(row.get("weapon", {}).get("name", "Неизвестно"))
                    for row in kills
                )
                items = counts.most_common(10)
                labels = [name[:24] for name, _ in reversed(items)]
                values = [count for _, count in reversed(items)]
                axis.barh(labels, values, color="#f39b39")
                axis.set_title("Топ-10 оружия")
                axis.set_xlabel("Киллы")
            elif kind == "killers":
                counts = Counter(
                    str(row.get("killer", {}).get("name", "Неизвестно"))
                    for row in kills
                )
                items = counts.most_common(10)
                labels = [name[:24] for name, _ in reversed(items)]
                values = [count for _, count in reversed(items)]
                axis.barh(labels, values, color="#45b977")
                axis.set_title("Топ-10 киллеров")
                axis.set_xlabel("Киллы")
            else:
                raise ValueError(f"Unsupported graph type: {kind}")

            axis.grid(axis="x" if kind == "kills" else "x", alpha=0.2)
            figure.patch.set_facecolor("#fffaf6")
            axis.set_facecolor("#fffaf6")
            output = io.BytesIO()
            figure.savefig(output, format="png", dpi=150, facecolor=figure.get_facecolor())
            image = output.getvalue()
            _CACHE[key] = (time.monotonic() + _CACHE_TTL, image)
            for old_key, (expiry, _) in list(_CACHE.items()):
                if expiry <= time.monotonic():
                    _CACHE.pop(old_key, None)
            return image
        finally:
            plt.close(figure)


async def create_graph(kind: str, kills: list[dict[str, Any]]) -> bytes:
    """Run plotting off the Discord event loop."""
    return await asyncio.to_thread(_render, kind, kills)

