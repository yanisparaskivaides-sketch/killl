"""Constants and configuration shared by the bot."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("BOT_DATA_DIR", str(ROOT_DIR / "bot" / "data"))).expanduser()
BACKUP_DIR = Path(os.getenv("BOT_BACKUP_DIR", str(ROOT_DIR / "bot" / "backups"))).expanduser()

load_dotenv(ROOT_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o-mini").strip()
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
BOT_VERSION = "5.0.0"
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()

MAX_SCAN_BYTES = 10 * 1024 * 1024
MAX_KILL_IMPORT_BYTES = 2 * 1024 * 1024
MAX_KILL_IMPORT_FILES = 5
MAX_KILL_IMPORT_LINES = 1000

