"""Small async JSON store with lazy loading and debounced atomic writes."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from bot.config import BACKUP_DIR, DATA_DIR
from bot.reports import (
    DEFAULT_MILESTONE_THRESHOLDS,
    DEFAULT_REPORT_TIME,
    DEFAULT_WEEKLY_DAY,
    REPORT_TIMEZONE_NAME,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")
LEGACY_DATA_FILES = ("contracts.json",)

DEFAULTS: dict[str, Any] = {
    "kills": [],
    "members": [],
    "settings": {
        "mode": "logic",
        "style": "smooth",
        "mp_channels": {},
        "reports_enabled": False,
        "report_channel_id": None,
        "report_time": DEFAULT_REPORT_TIME,
        "report_timezone": REPORT_TIMEZONE_NAME,
        "report_weekday": DEFAULT_WEEKLY_DAY,
        "last_daily_report": "",
        "last_weekly_report": "",
        "milestone_channel_id": None,
        "milestone_thresholds": list(DEFAULT_MILESTONE_THRESHOLDS),
    },
}


class AsyncJsonStore:
    """Keeps JSON documents in memory and flushes changed files every few seconds."""

    def __init__(self, data_dir: Path = DATA_DIR, backup_dir: Path = BACKUP_DIR):
        self.data_dir = data_dir
        self.backup_dir = backup_dir
        self._cache: dict[str, Any] = {}
        self._dirty: set[str] = set()
        self._lock = asyncio.Lock()

    def _path(self, key: str) -> Path:
        if key not in DEFAULTS:
            raise KeyError(f"Unknown JSON store: {key}")
        return self.data_dir / f"{key}.json"

    @staticmethod
    def _read_file(path: Path, default: Any) -> Any:
        if not path.exists():
            return copy.deepcopy(default)
        try:
            with path.open("r", encoding="utf-8") as handle:
                result = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            # Do not silently replace a damaged file: doing so could erase real records.
            raise RuntimeError(f"Could not read {path.name}: {exc}") from exc
        if not isinstance(result, type(default)):
            raise RuntimeError(f"{path.name} must contain a {type(default).__name__}")
        return result

    async def _ensure_loaded_locked(self, key: str) -> None:
        if key not in self._cache:
            path = self._path(key)
            self._cache[key] = await asyncio.to_thread(
                self._read_file, path, DEFAULTS[key]
            )
            if not path.exists():
                self._dirty.add(key)

    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        # Loading once at startup surfaces corrupt files before the bot takes commands.
        for key in DEFAULTS:
            async with self._lock:
                await self._ensure_loaded_locked(key)
        await self.flush()

    async def get(self, key: str) -> Any:
        async with self._lock:
            await self._ensure_loaded_locked(key)
            return copy.deepcopy(self._cache[key])

    async def mutate(self, key: str, change: Callable[[Any], T]) -> T:
        async with self._lock:
            await self._ensure_loaded_locked(key)
            result = change(self._cache[key])
            self._dirty.add(key)
            return copy.deepcopy(result)

    @staticmethod
    def _write_atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
        temporary.replace(path)

    async def flush(self) -> None:
        async with self._lock:
            pending = list(self._dirty)
            for key in pending:
                await asyncio.to_thread(
                    self._write_atomic, self._path(key), self._cache[key]
                )
                self._dirty.discard(key)

    async def backup(self, keep: int = 48) -> Path | None:
        await self.flush()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.backup_dir / stamp

        def copy_snapshot() -> None:
            target.mkdir(parents=True, exist_ok=False)
            filenames = [f"{key}.json" for key in DEFAULTS]
            filenames.extend(LEGACY_DATA_FILES)
            for filename in filenames:
                source = self.data_dir / filename
                if source.exists():
                    shutil.copy2(source, target / source.name)

            snapshots = sorted(
                (path for path in self.backup_dir.iterdir() if path.is_dir()),
                key=lambda path: path.name,
                reverse=True,
            )
            for old in snapshots[keep:]:
                shutil.rmtree(old)

        try:
            await asyncio.to_thread(copy_snapshot)
            return target
        except FileExistsError:
            logger.warning("Skipping duplicate backup timestamp %s", stamp)
        except OSError:
            logger.exception("Could not create automatic backup")
        return None

