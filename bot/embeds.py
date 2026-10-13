"""Discord embed builders for the dashboard, roster, profiles, and rankings."""

from __future__ import annotations

import colorsys
import math
from datetime import UTC, datetime
from typing import Any

import discord

from bot.stats import filter_kills, kill_totals, member_deaths, member_kills
from bot.utils import GANGS, format_label, gang_label, normalize_name, safe_text

RGB_STYLES = ("smooth", "rainbow", "sunset", "neon", "pulse")
STYLE_LABELS = {
    "smooth": "Smooth",
    "rainbow": "Rainbow",
    "sunset": "Sunset",
    "neon": "Neon",
    "pulse": "Pulse",
}
_NEON = ((0, 255, 150), (0, 200, 255), (255, 0, 200), (255, 120, 0))


def rgb_color(style: str, hue: float) -> discord.Color:
    if style == "sunset":
        red, green, blue = colorsys.hsv_to_rgb(
            0.02 + 0.15 * abs(math.sin(hue)), 0.82, 1.0
        )
        return discord.Color.from_rgb(int(red * 255), int(green * 255), int(blue * 255))
    if style == "neon":
        return discord.Color.from_rgb(*_NEON[int(hue) % len(_NEON)])
    if style == "pulse":
        intensity = 0.5 + 0.5 * abs(math.sin(hue))
        return discord.Color.from_rgb(
            int(200 * intensity), int(40 * intensity), int(80 * intensity)
        )
    red, green, blue = colorsys.hsv_to_rgb(hue % 1.0, 0.86, 1.0)
    return discord.Color.from_rgb(int(red * 255), int(green * 255), int(blue * 255))


def next_hue(style: str, hue: float) -> tuple[float, float]:
    increments = {"smooth": (0.01, 2.5), "rainbow": (0.05, 1.5), "sunset": (0.1, 2.0),
                  "neon": (1.0, 1.5), "pulse": (0.15, 1.0)}
    step, delay = increments.get(style, increments["smooth"])
    return hue + step, delay


async def build_dashboard_embed(bot: Any, hue: float = 0.0) -> discord.Embed:
    settings = await bot.store.get("settings")
    members = await bot.store.get("members")
    kills = await bot.store.get("kills")
    totals = kill_totals(kills)
    top_killers = sorted(
        totals.items(), key=lambda item: (-item[1], item[0].casefold())
    )[:3]

    embed = discord.Embed(
        title="⚡ KILL-LIST • LIVE CONTROL",
        description="**Живая панель сервера**  •  состав  •  киллы  •  активность MP",
        color=rgb_color(str(settings.get("style", "smooth")), hue),
        timestamp=datetime.now(UTC),
    )
    mode = "Нейронка" if settings.get("mode") == "ai" else "Логика"
    embed.add_field(
        name="🎛️ СИСТЕМА",
        value=(
            f"Режим: **{mode}**\n"
            f"RGB: **{STYLE_LABELS.get(settings.get('style', 'smooth'), 'Smooth')}**"
        ),
        inline=True,
    )
    embed.add_field(
        name="👥 СОСТАВ",
        value=f"**{len(members)}** участников",
        inline=True,
    )
    embed.add_field(
        name="⚔️ ВСЕГО КИЛЛОВ",
        value=f"**{len(kills)}**",
        inline=True,
    )
    podium = [
        f"{('🥇', '🥈', '🥉')[index]} **{safe_text(name, limit=32)}** — {count}"
        for index, (name, count) in enumerate(top_killers)
    ]
    embed.add_field(
        name="🏆 ЛИДЕРЫ КИЛЛ-ЛИСТА",
        value="\n".join(podium) or "Пока нет записей",
        inline=False,
    )
    mp_counts = {
        mp: sum(1 for row in kills if row.get("mp") == mp)
        for mp in ("gs", "vch", "fura")
    }
    embed.add_field(
        name="🌐 АКТИВНОСТЬ MP",
        value=(
            f"**GS**  `{mp_counts['gs']}`   "
            f"**VCH**  `{mp_counts['vch']}`   "
            f"**Фура**  `{mp_counts['fura']}`"
        ),
        inline=False,
    )
    gang_counts = {opg: 0 for opg in GANGS}
    for member in members:
        if member.get("opg") in gang_counts:
            gang_counts[member["opg"]] += 1
    for opg, gang in GANGS.items():
        embed.add_field(
            name=f"{gang['emoji']} {gang['name'].upper()}",
            value=f"**{gang_counts[opg]}** в составе",
            inline=True,
        )
    latest = sorted(kills, key=lambda row: int(row.get("ts", 0)), reverse=True)[:3]
    recent = [
        f"`{row.get('time', '--:--:--')}` "
        f"{safe_text(str(row.get('killer', {}).get('name', '?')), limit=24)} "
        f"→ {safe_text(str(row.get('victim', {}).get('name', '?')), limit=24)}"
        for row in latest
    ]
    embed.add_field(
        name="📡 ПОСЛЕДНЯЯ АКТИВНОСТЬ",
        value="\n".join(recent) or "Ждём первый лог",
        inline=False,
    )
    embed.set_footer(
        text="LIVE • Данные сохраняются автоматически • RGB обновляется в реальном времени"
    )
    return embed


def build_period_report_embed(
    kills: list[dict[str, Any]], period: str
) -> discord.Embed:
    matching = filter_kills(kills, period)
    totals = kill_totals(matching)
    ranking = sorted(totals.items(), key=lambda item: (-item[1], item[0].casefold()))
    title, label = (
        ("📊 СВОДКА ЗА 24 ЧАСА", "Последние 24 часа")
        if period == "day"
        else ("📊 ИТОГИ НЕДЕЛИ", "Последние 7 дней")
    )
    now = datetime.now(UTC)
    embed = discord.Embed(
        title=title,
        description=(
            f"**{len(matching)}** убийств  •  "
            f"**{len(totals)}** активных игроков\n{label}"
        ),
        color=rgb_color("rainbow", now.timestamp() / 20),
        timestamp=now,
    )
    top_lines = [
        f"{('🥇', '🥈', '🥉')[index]} **{safe_text(name, limit=32)}** — {count}"
        for index, (name, count) in enumerate(ranking[:5])
    ]
    embed.add_field(
        name="🏆 ТОП КИЛЛЕРОВ",
        value="\n".join(top_lines) or "За период киллов нет",
        inline=True,
    )
    mp_counts = {
        mp: sum(1 for row in matching if row.get("mp") == mp)
        for mp in ("gs", "vch", "fura")
    }
    embed.add_field(
        name="🌐 ПО MP",
        value=(
            f"GS: **{mp_counts['gs']}**\n"
            f"VCH: **{mp_counts['vch']}**\n"
            f"Фура: **{mp_counts['fura']}**"
        ),
        inline=True,
    )
    recent_lines = [
        f"`{row.get('time', '--:--:--')}` "
        f"{safe_text(str(row.get('killer', {}).get('name', '?')), limit=24)} "
        f"→ {safe_text(str(row.get('victim', {}).get('name', '?')), limit=24)}"
        for row in matching[:5]
    ]
    embed.add_field(
        name="💀 СВЕЖИЕ КИЛЛЫ",
        value="\n".join(recent_lines) or "Новых записей нет",
        inline=False,
    )
    embed.set_footer(text="Kill-List • Автоматический отчёт")
    return embed


def build_release_embed() -> discord.Embed:
    now = datetime.now(UTC)
    embed = discord.Embed(
        title="🚀 KILL-LIST • ОБНОВЛЕНИЕ",
        description=(
            "**Новый выпуск системы килл-листов**\n"
            "Больше автоматизации, понятная статистика и живой RGB-интерфейс."
        ),
        color=rgb_color("rainbow", now.timestamp() / 20),
        timestamp=now,
    )
    embed.add_field(
        name="📥 Импорт логов",
        value="Вставка текста и `.txt`, выбор MP и даты через `!addkills`.",
        inline=False,
    )
    embed.add_field(
        name="📊 Автоматические отчёты",
        value=(
            "Сводки за 24 часа и 7 дней; настройка канала и времени — `!reports 21:00`."
        ),
        inline=False,
    )
    embed.add_field(
        name="🏅 Рубежи киллов",
        value="Уведомления о личных рубежах настраиваются через `!milestones`.",
        inline=False,
    )
    embed.add_field(
        name="🎨 LIVE-панель",
        value="Обновлённая статистика, лидеры, MP и переливающийся RGB.",
        inline=False,
    )
    embed.add_field(
        name="🤖 AI и фильтры",
        value=(
            "`!ai` отвечает на вопросы и добавляет присланный лог; "
            "`!top` фильтрует киллы по MP и дате."
        ),
        inline=False,
    )
    embed.set_footer(text="Чтобы опубликовать анонс в этом канале, используйте !release")
    return embed


async def build_roster_embed(bot: Any, opg: str) -> discord.Embed:
    members = await bot.store.get("members")
    kills = await bot.store.get("kills")
    roster = [member for member in members if member.get("opg") == opg]
    order = {"ЛД": 0, "СС": 1, "": 2, None: 2}
    roster.sort(
        key=lambda member: (
            order.get(member.get("label"), 2),
            normalize_name(str(member.get("name", ""))),
        )
    )
    gang = GANGS[opg]
    embed = discord.Embed(
        title=f"{gang['emoji']} Состав: {gang['name']}",
        color=discord.Color.from_rgb(190, 48, 62),
    )
    sections = (("ЛД", "👑 Лидер"), ("СС", "⭐ Старший состав"), ("", "Участники"))
    for label, title in sections:
        entries = [
            member
            for member in roster
            if (member.get("label") or "") == label
        ]
        if not entries:
            continue
        lines = [
            f"{safe_text(str(member.get('name', '?')))} — "
            f"{member_kills(kills, str(member.get('name', '')))}"
            for member in entries
        ]
        embed.add_field(name=title, value="\n".join(lines)[:1024], inline=False)
    if not roster:
        embed.description = "В этой ОПГ пока нет участников."
    embed.set_footer(text=f"Всего: {len(roster)}")
    return embed


async def build_rosters_embed(bot: Any) -> discord.Embed:
    members = await bot.store.get("members")
    kills = await bot.store.get("kills")
    embed = discord.Embed(title="Составы ОПГ", color=discord.Color.dark_red())
    for opg, gang in GANGS.items():
        roster = [member for member in members if member.get("opg") == opg]
        roster.sort(
            key=lambda member: (
                {"ЛД": 0, "СС": 1}.get(member.get("label"), 2),
                normalize_name(str(member.get("name", ""))),
            )
        )
        lines = [
            f"{'👑 ' if member.get('label') == 'ЛД' else '⭐ ' if member.get('label') == 'СС' else ''}"
            f"{safe_text(str(member.get('name', '?')))} — "
            f"{member_kills(kills, str(member.get('name', '')))}"
            for member in roster[:25]
        ]
        embed.add_field(
            name=f"{gang['emoji']} {gang['name']} ({len(roster)})",
            value="\n".join(lines)[:1024] or "Пока пусто",
            inline=True,
        )
    return embed


async def build_top_embed(
    bot: Any, scope: str = "all", count: int = 3, mp: str = "all"
) -> discord.Embed:
    all_kills = await bot.store.get("kills")
    matching = filter_kills(all_kills, scope, mp)
    totals = kill_totals(matching)
    ranking = sorted(totals.items(), key=lambda item: (-item[1], item[0].casefold()))
    scope_labels = {
        "all": "Всё время",
        "day": "Последние 24 часа",
        "week": "Последние 7 дней",
    }
    mp_labels = {"all": "Все MP", "gs": "GS", "vch": "VCh", "fura": "Фура"}
    embed = discord.Embed(
        title=f"🏆 Топ-{count} • {scope_labels.get(scope, f'Дата {scope}')} • {mp_labels[mp]}",
        color=discord.Color.gold(),
    )
    ranking_lines = [
        f"**{position}.** {safe_text(name)} — {value}"
        for position, (name, value) in enumerate(ranking[:count], start=1)
    ]
    embed.add_field(
        name=f"🏆 ТОП-{count}",
        value="\n".join(ranking_lines)[:1024] or "За этот период киллов нет.",
        inline=False,
    )
    recent_lines: list[str] = []
    for row in matching[:20]:
        killer = safe_text(str(row.get("killer", {}).get("name", "?")), limit=32)
        victim = safe_text(str(row.get("victim", {}).get("name", "?")), limit=32)
        weapon = safe_text(str(row.get("weapon", {}).get("name", "?")), limit=30)
        recent_lines.append(f"[{row.get('time', '--:--:--')}] {killer} → {victim} ({weapon})")
    embed.add_field(
        name="💀 КИЛЛ-ЛИСТ",
        value="\n".join(recent_lines)[:1024] or "Киллов пока нет.",
        inline=False,
    )
    embed.set_footer(text=f"Всего в фильтре: {len(matching)}")
    return embed


async def build_recent_kills_embed(bot: Any, limit: int = 15) -> discord.Embed:
    kills = await bot.store.get("kills")
    lines = []
    for row in sorted(kills, key=lambda item: int(item.get("ts", 0)), reverse=True)[:limit]:
        killer = safe_text(str(row.get("killer", {}).get("name", "?")))
        victim = safe_text(str(row.get("victim", {}).get("name", "?")))
        weapon = safe_text(str(row.get("weapon", {}).get("name", "?")))
        lines.append(
            f"[{row.get('time', '--:--:--')}] {killer} → {victim} ({weapon})"
        )
    embed = discord.Embed(title="Последние убийства", color=discord.Color.red())
    embed.description = "\n".join(lines)[:4000] or "Киллов пока нет."
    return embed


async def build_profile_embed(bot: Any, nick: str) -> discord.Embed:
    members = await bot.store.get("members")
    kills = await bot.store.get("kills")
    member = next(
        (row for row in members if normalize_name(str(row.get("name", ""))) == normalize_name(nick)),
        None,
    )
    embed = discord.Embed(
        title=f"Профиль: {safe_text(nick)}",
        color=discord.Color.blurple(),
    )
    if member:
        embed.add_field(name="ОПГ", value=gang_label(str(member.get("opg", "?"))), inline=True)
        embed.add_field(
            name="Метка",
            value={"ЛД": "ЛД", "СС": "СС"}.get(member.get("label"), "—"),
            inline=True,
        )
    else:
        embed.add_field(name="ОПГ", value="Не состоит в ОПГ", inline=True)
        embed.add_field(name="Метка", value="—", inline=True)
    embed.add_field(name="Киллы", value=str(member_kills(kills, nick)), inline=True)
    embed.add_field(name="Смерти", value=str(member_deaths(kills, nick)), inline=True)
    return embed


def short_kill_list(kills: list[dict[str, Any]], limit: int = 15) -> str:
    lines = [
        f"[{row.get('time', '--:--:--')}] "
        f"{safe_text(str(row.get('killer', {}).get('name', '?')))} → "
        f"{safe_text(str(row.get('victim', {}).get('name', '?')))}"
        for row in sorted(kills, key=lambda item: int(item.get("ts", 0)), reverse=True)[:limit]
    ]
    return "\n".join(lines) or "Киллов пока нет."

