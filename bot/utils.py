"""Names, gang aliases, and small validation helpers."""

from __future__ import annotations

import re

import discord

GANGS: dict[str, dict[str, str]] = {
    "tambov": {"name": "Тамбов", "emoji": "🟥"},
    "orekh": {"name": "Ореховская", "emoji": "🟧"},
    "kurgan": {"name": "Курганская", "emoji": "🟨"},
    "kavkaz": {"name": "Кавказская", "emoji": "🟩"},
}

_OPG_ALIASES = {
    "tambov": "tambov",
    "тамбов": "tambov",
    "тамбовская": "tambov",
    "tambovskaya": "tambov",
    "orekh": "orekh",
    "орех": "orekh",
    "ореховская": "orekh",
    "orehovskaya": "orekh",
    "orekhovskaya": "orekh",
    "kurgan": "kurgan",
    "курган": "kurgan",
    "курганская": "kurgan",
    "kavkaz": "kavkaz",
    "кавказ": "kavkaz",
    "кавказская": "kavkaz",
}

_NICK_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9_]{1,32}$")


def normalize_name(value: str) -> str:
    return value.strip().casefold()


def canonical_opg(value: str | None) -> str | None:
    if not value:
        return None
    key = re.sub(r"[\s_-]+", "", value.strip().casefold())
    return _OPG_ALIASES.get(key)


def gang_label(opg: str) -> str:
    gang = GANGS.get(opg)
    return f"{gang['emoji']} {gang['name']}" if gang else opg


def valid_nick(value: str) -> bool:
    return bool(_NICK_RE.fullmatch(value.strip()))


def safe_text(value: str, *, limit: int = 100) -> str:
    """Escape user-supplied nicks before placing them in Discord messages."""
    return discord.utils.escape_markdown(value[:limit], as_needed=True)


def format_label(label: str | None) -> str:
    return {"ЛД": "👑 Лидер", "СС": "⭐ Старший состав"}.get(label or "", "Участник")

