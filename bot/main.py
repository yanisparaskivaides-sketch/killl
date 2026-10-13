"""Start the Grand Mobile 15 kill-list bot."""

from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.ai_client import AIClient, AIServiceError
from bot.config import (
    BOT_VERSION,
    DISCORD_TOKEN,
    MAX_KILL_IMPORT_BYTES,
    MAX_KILL_IMPORT_FILES,
    MAX_KILL_IMPORT_LINES,
    MAX_SCAN_BYTES,
)
from bot.embeds import (
    build_dashboard_embed,
    build_profile_embed,
    build_period_report_embed,
    build_release_embed,
    build_recent_kills_embed,
    build_roster_embed,
    build_rosters_embed,
    build_top_embed,
    next_hue,
)
from bot.graphs import create_graph
from bot.kill_import import (
    canonical_mp,
    parse_import_args,
    parse_iso_date,
    parse_kill_lines,
)
from bot.parser import parse_kill_log
from bot.reports import (
    DEFAULT_MILESTONE_THRESHOLDS,
    DEFAULT_REPORT_TIME,
    DEFAULT_WEEKLY_DAY,
    REPORT_TIMEZONE,
    REPORT_TIMEZONE_NAME,
    due_reports,
    parse_milestone_thresholds,
    parse_report_time,
)
from bot.stats import MP_FILTERS, SCOPES
from bot.storage import AsyncJsonStore
from bot.utils import GANGS, canonical_opg, gang_label, normalize_name, safe_text, valid_nick
from bot.views import MainMenuView

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("kill_list_bot")


def kill_fingerprint(event: dict[str, Any]) -> tuple[str, ...]:
    def value(group: str, field: str) -> str:
        nested = event.get(group)
        if not isinstance(nested, dict):
            return ""
        return str(nested.get(field, "")).strip().casefold()

    return (
        str(event.get("date", "")),
        str(event.get("time", "")),
        str(event.get("mp", "unknown")),
        value("killer", "name"),
        value("killer", "id"),
        value("weapon", "name"),
        value("weapon", "id"),
        value("victim", "name"),
        value("victim", "id"),
    )


class KillListBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.store = AsyncJsonStore()
        self.ai = AIClient()
        self._rgb_tasks: dict[int, asyncio.Task[None]] = {}

    async def setup_hook(self) -> None:
        await self.store.initialize()
        self.flush_loop.start()
        self.backup_loop.start()
        self.report_loop.start()
        try:
            synced = await self.tree.sync()
            logger.info("Synced %d global application command(s)", len(synced))
        except discord.HTTPException:
            # Prefix commands remain available if application-command sync is unavailable.
            logger.exception("Could not sync the /menu application command")

    async def on_ready(self) -> None:
        logger.info("Connected to Discord as %s", self.user)

    async def close(self) -> None:
        for task in list(self._rgb_tasks.values()):
            task.cancel()
        self._rgb_tasks.clear()
        for loop in (self.flush_loop, self.backup_loop, self.report_loop):
            if loop.is_running():
                loop.cancel()
        await self.store.flush()
        await self.ai.close()
        await super().close()

    @tasks.loop(seconds=5)
    async def flush_loop(self) -> None:
        try:
            await self.store.flush()
        except (OSError, RuntimeError):
            logger.exception("Could not flush JSON data")

    @tasks.loop(minutes=30)
    async def backup_loop(self) -> None:
        await self.store.backup()

    @tasks.loop(minutes=1)
    async def report_loop(self) -> None:
        try:
            settings = await self.store.get("settings")
            due = due_reports(settings, datetime.now(REPORT_TIMEZONE))
            if not due:
                return

            channel = await self._resolve_text_channel(
                settings.get("report_channel_id")
            )
            if channel is None:
                logger.warning("Scheduled report channel is unavailable")
                return

            kills = await self.store.get("kills")
            for period, key in due:
                await channel.send(embed=build_period_report_embed(kills, period))
                last_key = "last_daily_report" if period == "day" else "last_weekly_report"
                await self.store.mutate(
                    "settings", lambda current: current.update({last_key: key})
                )
                logger.info("Sent scheduled %s kill-list report", period)
        except discord.HTTPException as exc:
            logger.warning("Could not send scheduled report: %s", type(exc).__name__)
        except Exception:
            logger.exception("Scheduled report task failed")

    @report_loop.before_loop
    async def before_report_loop(self) -> None:
        await self.wait_until_ready()

    async def _resolve_text_channel(self, channel_id: Any) -> Any | None:
        try:
            numeric_id = int(channel_id)
        except (TypeError, ValueError):
            return None
        channel = self.get_channel(numeric_id)
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(numeric_id)
        except discord.HTTPException as exc:
            logger.warning("Could not resolve report channel: %s", type(exc).__name__)
            return None

    async def on_message(self, message: discord.Message) -> None:
        if (
            message.guild is not None
            and (not message.author.bot or message.webhook_id is not None)
        ):
            content = message.content
            if content.startswith("[") and " убил " in content:
                await self.record_kill_message(message)
        await self.process_commands(message)

    async def on_command_error(
        self, context: commands.Context[Any], error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await context.reply(
                "Эту команду может использовать только администратор сервера.",
                mention_author=False,
            )
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await context.reply(
                "Не хватает аргумента. Используйте `!help`, чтобы посмотреть синтаксис.",
                mention_author=False,
            )
            return
        if isinstance(error, commands.CommandOnCooldown):
            await context.reply(
                f"Подождите {error.retry_after:.0f} сек. перед повтором.",
                mention_author=False,
            )
            return
        if isinstance(error, commands.BadArgument):
            await context.reply("Проверьте аргументы команды.", mention_author=False)
            return
        original = getattr(error, "original", error)
        logger.error(
            "Command %s failed: %s",
            getattr(context.command, "qualified_name", "unknown"),
            type(original).__name__,
            exc_info=original,
        )
        await context.reply(
            "Команда завершилась с ошибкой. Подробности записаны в журнал.",
            mention_author=False,
        )

    async def record_kill_message(self, message: discord.Message) -> None:
        settings = await self.store.get("settings")
        mp = settings.get("mp_channels", {}).get(str(message.channel.id), "unknown")
        event = parse_kill_log(
            message.content,
            mp=mp,
            created_at=message.created_at,
            message_id=message.id,
        )
        if not event:
            return

        await self.add_kill_events([event], notify_channel_id=message.channel.id)

    async def add_kill_events(
        self,
        events: list[dict[str, Any]],
        *,
        notify_channel_id: int | None = None,
    ) -> int:
        if not events:
            return 0

        settings = await self.store.get("settings")
        thresholds = parse_milestone_thresholds(
            settings.get("milestone_thresholds", DEFAULT_MILESTONE_THRESHOLDS)
        )
        target_channel_id = (
            settings.get("milestone_channel_id")
            or settings.get("report_channel_id")
            or notify_channel_id
        )

        def add_events(
            kills: list[dict[str, Any]],
        ) -> tuple[int, list[tuple[str, int]]]:
            source_ids = {
                str(row.get("source_message_id"))
                for row in kills
                if row.get("source_message_id") is not None
            }
            fingerprints = {kill_fingerprint(row) for row in kills}
            previous_totals: dict[str, int] = {}
            display_names: dict[str, str] = {}
            for row in kills:
                killer = row.get("killer")
                name = str(killer.get("name", "")).strip() if isinstance(killer, dict) else ""
                if name:
                    key = normalize_name(name)
                    display_names.setdefault(key, name)
                    previous_totals[key] = previous_totals.get(key, 0) + 1

            inserted = 0
            added_totals: dict[str, int] = {}
            for event in events:
                source_id = event.get("source_message_id")
                fingerprint = kill_fingerprint(event)
                if (
                    (source_id is not None and str(source_id) in source_ids)
                    or fingerprint in fingerprints
                ):
                    continue
                kills.append(event)
                fingerprints.add(fingerprint)
                if source_id is not None:
                    source_ids.add(str(source_id))
                inserted += 1
                killer = event.get("killer")
                name = str(killer.get("name", "")).strip() if isinstance(killer, dict) else ""
                if name:
                    key = normalize_name(name)
                    display_names.setdefault(key, name)
                    added_totals[key] = added_totals.get(key, 0) + 1

            milestones: list[tuple[str, int]] = []
            for key, count in added_totals.items():
                previous = previous_totals.get(key, 0)
                current = previous + count
                milestones.extend(
                    (display_names[key], threshold)
                    for threshold in thresholds
                    if previous < threshold <= current
                )
            return inserted, milestones

        added, milestones = await self.store.mutate("kills", add_events)
        if milestones and target_channel_id is not None:
            channel = await self._resolve_text_channel(target_channel_id)
            if channel is not None:
                lines = [
                    f"🏅 **{safe_text(name)}** достиг рубежа **{threshold} киллов**"
                    for name, threshold in milestones
                ]
                embed = discord.Embed(
                    title="НОВЫЙ РУБЕЖ КИЛЛ-ЛИСТА",
                    description="\n".join(lines),
                    color=discord.Color.gold(),
                    timestamp=datetime.now(UTC),
                )
                embed.set_footer(text="Общий счёт по всем MP")
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException as exc:
                    logger.warning("Could not send milestone alert: %s", type(exc).__name__)
        return added

    async def add_member(self, nick: str, opg: str) -> tuple[bool, str]:
        nick = nick.strip()
        if not valid_nick(nick):
            return False, "Ник может содержать только латиницу, кириллицу, цифры и `_` (до 32 символов)."
        if opg not in GANGS:
            return False, "Неизвестная ОПГ."
        members = await self.store.get("members")
        existing = next(
            (
                row
                for row in members
                if normalize_name(str(row.get("name", ""))) == normalize_name(nick)
            ),
            None,
        )
        if existing:
            return False, f"{safe_text(nick)} уже состоит в {gang_label(str(existing.get('opg', '?')))}."

        def add(rows: list[dict[str, Any]]) -> None:
            rows.append(
                {
                    "name": nick,
                    "opg": opg,
                    "label": "",
                    "joined_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
            )

        await self.store.mutate("members", add)
        return True, f"✅ {safe_text(nick)} добавлен в {gang_label(opg)}."

    async def remove_member(self, nick: str) -> tuple[bool, str]:
        key = normalize_name(nick)

        def remove(rows: list[dict[str, Any]]) -> str | None:
            for index, member in enumerate(rows):
                if normalize_name(str(member.get("name", ""))) == key:
                    return str(rows.pop(index).get("name", nick))
            return None

        removed = await self.store.mutate("members", remove)
        if not removed:
            return False, f"Участник {safe_text(nick)} не найден."
        return True, f"✅ {safe_text(removed)} удалён из состава."

    async def move_member(self, nick: str, opg: str) -> tuple[bool, str]:
        if opg not in GANGS:
            return False, "Неизвестная ОПГ."
        key = normalize_name(nick)

        def move(rows: list[dict[str, Any]]) -> str | None:
            for member in rows:
                if normalize_name(str(member.get("name", ""))) == key:
                    member["opg"] = opg
                    # Labels belong to a gang's leadership and do not follow a transfer.
                    member["label"] = ""
                    return str(member.get("name", nick))
            return None

        moved = await self.store.mutate("members", move)
        if not moved:
            return False, f"Участник {safe_text(nick)} не найден."
        return True, f"✅ {safe_text(moved)} переведён в {gang_label(opg)} без метки."

    async def assign_label(
        self, nick: str, label: str
    ) -> tuple[bool, str, str | None, str | None, str | None]:
        if label not in {"СС", "ЛД"}:
            return False, "Неизвестная метка.", None, None, None
        key = normalize_name(nick)

        def assign(rows: list[dict[str, Any]]) -> tuple[str, str, str | None] | None:
            target = next(
                (
                    member
                    for member in rows
                    if normalize_name(str(member.get("name", ""))) == key
                ),
                None,
            )
            if not target:
                return None
            old_leader = None
            if label == "ЛД":
                for member in rows:
                    if (
                        member.get("opg") == target.get("opg")
                        and member.get("label") == "ЛД"
                        and member is not target
                    ):
                        old_leader = str(member.get("name", ""))
                        member["label"] = ""
            target["label"] = label
            return (
                str(target.get("name", nick)),
                str(target.get("opg", "")),
                old_leader,
            )

        result = await self.store.mutate("members", assign)
        if not result:
            return False, f"Участник {safe_text(nick)} не найден.", None, None, None
        name, opg, old_leader = result
        return True, "", name, opg if opg in GANGS else None, old_leader

    async def clear_label(self, nick: str) -> tuple[bool, str, str | None]:
        key = normalize_name(nick)

        def clear(rows: list[dict[str, Any]]) -> tuple[str, str] | None:
            for member in rows:
                if normalize_name(str(member.get("name", ""))) == key:
                    member["label"] = ""
                    return str(member.get("name", nick)), str(member.get("opg", ""))
            return None

        result = await self.store.mutate("members", clear)
        if not result:
            return False, f"Участник {safe_text(nick)} не найден.", None
        name, opg = result
        return True, name, opg if opg in GANGS else None

    async def current_mp(self, channel_id: int) -> str:
        settings = await self.store.get("settings")
        return str(settings.get("mp_channels", {}).get(str(channel_id), "unknown"))

    def cancel_rgb(self, channel_id: int | None) -> None:
        if channel_id is None:
            return
        task = self._rgb_tasks.pop(channel_id, None)
        if task and not task.done():
            task.cancel()

    async def start_rgb(self, message: discord.Message) -> None:
        channel_id = message.channel.id
        self.cancel_rgb(channel_id)
        task = asyncio.create_task(self._rgb_loop(message))
        self._rgb_tasks[channel_id] = task

    async def _rgb_loop(self, message: discord.Message) -> None:
        channel_id = message.channel.id
        hue = 0.0
        try:
            while True:
                settings = await self.store.get("settings")
                style = str(settings.get("style", "smooth"))
                embed = await build_dashboard_embed(self, hue)
                try:
                    await message.edit(embed=embed)
                except (discord.NotFound, discord.Forbidden):
                    break
                except discord.HTTPException as exc:
                    logger.warning("RGB dashboard edit failed: %s", type(exc).__name__)
                    if exc.status == 429:
                        await asyncio.sleep(5)
                    else:
                        break
                hue, delay = next_hue(style, hue)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("RGB task stopped after an unexpected error")
        finally:
            if self._rgb_tasks.get(channel_id) is asyncio.current_task():
                self._rgb_tasks.pop(channel_id, None)

    async def send_dashboard(self, destination: Any) -> discord.Message:
        message = await destination.send(
            embed=await build_dashboard_embed(self),
            view=MainMenuView(self),
        )
        await self.start_rgb(message)
        return message


bot = KillListBot()
PROCESS_STARTED_AT = datetime.now(UTC)


@bot.command(name="version")
async def version_command(context: commands.Context[Any]) -> None:
    """Show the installed bot release version."""
    await context.reply(f"Grand Mobile Kill-List Bot **v{BOT_VERSION}**", mention_author=False)


@bot.command(name="uptime")
async def uptime_command(context: commands.Context[Any]) -> None:
    """Show uptime of the current process."""
    seconds = max(0, int((datetime.now(UTC) - PROCESS_STARTED_AT).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    days, hours = divmod(hours, 24)
    await context.reply(
        f"Бот работает: **{days} д. {hours} ч. {minutes} мин. {seconds} сек.**",
        mention_author=False,
    )


@bot.command(name="health")
async def health_command(context: commands.Context[Any]) -> None:
    """Basic read-only status check."""
    try:
        kills = await bot.store.get("kills")
        members = await bot.store.get("members")
        settings = await bot.store.get("settings")
    except (OSError, RuntimeError, KeyError):
        logger.exception("Health check could not read data store")
        await context.reply("⚠️ Хранилище не удалось прочитать. Проверьте логи бота.", mention_author=False)
        return
    embed = discord.Embed(title="Состояние Kill-List", color=discord.Color.green())
    embed.add_field(name="Версия", value=f"v{BOT_VERSION}", inline=True)
    embed.add_field(name="Discord", value="Подключён" if bot.is_ready() else "Подключение…", inline=True)
    embed.add_field(name="AI", value="Доступен" if bot.ai.available else "Не настроен", inline=True)
    embed.add_field(name="Убийств", value=str(len(kills)), inline=True)
    embed.add_field(name="Участников", value=str(len(members)), inline=True)
    embed.add_field(name="Отчёты", value="Включены" if settings.get("reports_enabled") else "Выключены", inline=True)
    await context.reply(embed=embed, mention_author=False)


@bot.command(name="backup")
@commands.has_guild_permissions(administrator=True)
@commands.cooldown(1, 30, commands.BucketType.user)
async def backup_command(context: commands.Context[Any]) -> None:
    """Create a manual backup; restricted to Discord server administrators."""
    path = await bot.store.backup()
    if path is None:
        await context.reply("❌ Не удалось создать резервную копию. Проверьте логи.", mention_author=False)
        return
    await context.reply(f"✅ Резервная копия создана: `{path.name}`", mention_author=False)


@bot.tree.command(name="menu", description="Открыть панель управления Kill-List")
async def slash_menu(interaction: discord.Interaction) -> None:
    if interaction.guild is None or interaction.channel is None:
        await interaction.response.send_message(
            "Панель доступна на сервере Discord.", ephemeral=True
        )
        return
    embed = await build_dashboard_embed(bot)
    await interaction.response.send_message(embed=embed, view=MainMenuView(bot))
    message = await interaction.original_response()
    await bot.start_rgb(message)


@bot.command(name="menu")
async def menu_command(context: commands.Context[Any]) -> None:
    await bot.send_dashboard(context)


@bot.command(name="help")
async def help_command(context: commands.Context[Any]) -> None:
    embed = discord.Embed(
        title="📖 Список команд",
        description="Команды с префиксом `!`. Панель также доступна через `/menu`.",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Для всех участников",
        value=(
            "`!help` — этот список\n"
            "`!menu` / `/menu` — панель и составы\n"
            "`!kills` — последние убийства\n"
            "`!top [all|day|week|ГГГГ-ММ-ДД] [1-20] [all|gs|vch|fura]` — топ и килл-лист\n"
            "`!roster [опг]` — составы ОПГ\n"
            "`!find <ник>` — профиль, киллы и смерти\n"
            "`!graph kills|weapons|killers` — график\n"
            "`!health` — состояние бота\n"
            "`!version` / `!uptime` — версия и время работы\n"
            "`!backup` — резервная копия (администраторы)"
        ),
        inline=False,
    )
    embed.add_field(
        name="Только для администраторов",
        value=(
            "`!add <ник> <опг>`, `!remove <ник>`, `!move <ник> <опг>`\n"
            "`!ss <ник>`, `!ld <ник>`, `!clr <ник>` — метки состава\n"
            "`!setmp gs|vch|fura|none` — метка канала\n"
            "`!scan <опг>` — распознать ники со скриншота\n"
            "`!addkills <gs|vch|fura> [ГГГГ-ММ-ДД]` — текст или .txt; без даты — сегодня UTC\n"
            "`!ai <текст или вопрос>` — спросить, изменить состав или добавить лог из сообщения\n"
            "`!mode` — переключить режим"
        ),
        inline=False,
    )
    await context.send(embed=embed)


@bot.command(name="kills")
async def kills_command(context: commands.Context[Any]) -> None:
    await context.send(embed=await build_recent_kills_embed(bot))


@bot.command(name="addkills", aliases=["addlog"])
@commands.has_guild_permissions(administrator=True)
@commands.cooldown(1, 5, commands.BucketType.user)
async def add_kills_command(
    context: commands.Context[Any], *, args: str = ""
) -> None:
    try:
        mp, selected_date, body = parse_import_args(args)
    except ValueError as exc:
        await context.reply(str(exc), mention_author=False)
        return

    attachments = context.message.attachments
    if len(attachments) > MAX_KILL_IMPORT_FILES:
        await context.reply(
            f"За раз можно загрузить не больше {MAX_KILL_IMPORT_FILES} файлов.",
            mention_author=False,
        )
        return

    chunks: list[str] = [body] if body.strip() else []
    total_bytes = len(body.encode("utf-8"))
    for attachment in attachments:
        if not attachment.filename.casefold().endswith(".txt"):
            await context.reply(
                "Поддерживаются только файлы `.txt`.",
                mention_author=False,
            )
            return
        if attachment.size > MAX_KILL_IMPORT_BYTES:
            await context.reply(
                "Файл слишком большой. Максимум — 2 МБ на весь импорт.",
                mention_author=False,
            )
            return
        try:
            raw_file = await attachment.read()
        except (discord.HTTPException, OSError):
            await context.reply("Не удалось прочитать файл `.txt`.", mention_author=False)
            return
        total_bytes += len(raw_file)
        if total_bytes > MAX_KILL_IMPORT_BYTES:
            await context.reply(
                "Импорт слишком большой. Максимальный общий размер — 2 МБ.",
                mention_author=False,
            )
            return
        try:
            chunks.append(raw_file.decode("utf-8-sig"))
        except UnicodeDecodeError:
            await context.reply(
                "Файл должен быть сохранён в кодировке UTF-8.",
                mention_author=False,
            )
            return

    log_text = "\n".join(chunks)
    nonempty_lines = [line for line in log_text.splitlines() if line.strip()]
    if not nonempty_lines:
        await context.reply(
            "Формат: `!addkills <gs|vch|fura> [ГГГГ-ММ-ДД]`, "
            "затем добавьте строки текстом или приложите `.txt`.",
            mention_author=False,
        )
        return
    if len(nonempty_lines) > MAX_KILL_IMPORT_LINES:
        await context.reply(
            f"В одном импорте допускается не больше {MAX_KILL_IMPORT_LINES} строк.",
            mention_author=False,
        )
        return

    log_date = selected_date or datetime.now(UTC).date()
    source_prefix = f"import:{context.guild.id if context.guild else 0}:{context.message.id}"
    events = parse_kill_lines(
        log_text,
        mp=mp,
        log_date=log_date,
        source_prefix=source_prefix,
    )
    if not events:
        await context.reply(
            "Не нашёл строк в формате `21:09:47] Ник[ID] убил с помощью "
            '"Оружие[ID]" > Ник[ID]`.',
            mention_author=False,
        )
        return

    added = await bot.add_kill_events(events)
    duplicates = len(events) - added
    ignored = len(nonempty_lines) - len(events)
    await context.reply(
        f"✅ MP: **{mp.upper()}**, дата: **{log_date.isoformat()}**\n"
        f"Добавлено: **{added}** • дубликатов: **{duplicates}** "
        f"• не распознано строк: **{ignored}**.",
        mention_author=False,
    )


@bot.command(name="top")
async def top_command(context: commands.Context[Any], *, args: str = "") -> None:
    scope, count, mp = "all", 3, "all"
    for token in args.lower().split():
        if token in SCOPES:
            scope = token
        elif parse_iso_date(token):
            scope = token
        elif token in MP_FILTERS:
            mp = token
        elif token.isdigit():
            count = int(token)
        else:
            await context.reply(
                "Формат: `!top [all|day|week|ГГГГ-ММ-ДД] [1-20] "
                "[all|gs|vch|fura]`.",
                mention_author=False,
            )
            return
    if not 1 <= count <= 20:
        await context.reply("Размер топа должен быть от 1 до 20.", mention_author=False)
        return
    await context.send(embed=await build_top_embed(bot, scope, count, mp))


@bot.command(name="add")
@commands.has_guild_permissions(administrator=True)
async def add_command(context: commands.Context[Any], *, args: str) -> None:
    parts = args.rsplit(maxsplit=1)
    if len(parts) != 2:
        await context.reply("Формат: `!add <ник> <опг>`.", mention_author=False)
        return
    opg = canonical_opg(parts[1])
    if not opg:
        await context.reply("ОПГ: tambov / orekh / kurgan / kavkaz.", mention_author=False)
        return
    _ok, result = await bot.add_member(parts[0], opg)
    await context.send(result)


@bot.command(name="remove")
@commands.has_guild_permissions(administrator=True)
async def remove_command(context: commands.Context[Any], *, nick: str) -> None:
    _ok, result = await bot.remove_member(nick)
    await context.send(result)


@bot.command(name="move")
@commands.has_guild_permissions(administrator=True)
async def move_command(context: commands.Context[Any], *, args: str) -> None:
    parts = args.rsplit(maxsplit=1)
    if len(parts) != 2:
        await context.reply("Формат: `!move <ник> <новая опг>`.", mention_author=False)
        return
    opg = canonical_opg(parts[1])
    if not opg:
        await context.reply("ОПГ: tambov / orekh / kurgan / kavkaz.", mention_author=False)
        return
    _ok, result = await bot.move_member(parts[0], opg)
    await context.send(result)


@bot.command(name="ss")
@commands.has_guild_permissions(administrator=True)
async def ss_command(context: commands.Context[Any], *, nick: str) -> None:
    ok, error, name, opg, _old_leader = await bot.assign_label(nick, "СС")
    if not ok or not name or not opg:
        await context.send(error or "Не удалось назначить метку.")
        return
    embed = discord.Embed(
        title=f"⭐ {safe_text(name)} назначен старшим составом {gang_label(opg)}",
        color=discord.Color.blue(),
        timestamp=datetime.now(UTC),
    )
    await context.send(embed=embed)


@bot.command(name="ld")
@commands.has_guild_permissions(administrator=True)
async def ld_command(context: commands.Context[Any], *, nick: str) -> None:
    ok, error, name, opg, old_leader = await bot.assign_label(nick, "ЛД")
    if not ok or not name or not opg:
        await context.send(error or "Не удалось назначить метку.")
        return
    embed = discord.Embed(
        title=f"👑 {safe_text(name)} назначен лидером {gang_label(opg)}",
        color=discord.Color.gold(),
        timestamp=datetime.now(UTC),
    )
    if old_leader:
        embed.description = f"Прежний лидер {safe_text(old_leader)} снят."
    await context.send(embed=embed)


@bot.command(name="clr")
@commands.has_guild_permissions(administrator=True)
async def clear_label_command(context: commands.Context[Any], *, nick: str) -> None:
    ok, name, opg = await bot.clear_label(nick)
    if not ok or not opg:
        await context.send(name)
        return
    embed = discord.Embed(
        title=f"❌ {safe_text(name)} снят с должности в {gang_label(opg)}",
        color=discord.Color.dark_grey(),
        timestamp=datetime.now(UTC),
    )
    await context.send(embed=embed)


@bot.command(name="roster")
async def roster_command(context: commands.Context[Any], *, opg_name: str = "") -> None:
    if not opg_name.strip():
        await context.send(embed=await build_rosters_embed(bot))
        return
    opg = canonical_opg(opg_name)
    if not opg:
        await context.reply("ОПГ: tambov / orekh / kurgan / kavkaz.", mention_author=False)
        return
    await context.send(embed=await build_roster_embed(bot, opg))


@bot.command(name="find")
async def find_command(context: commands.Context[Any], *, nick: str) -> None:
    await context.send(embed=await build_profile_embed(bot, nick))


@bot.command(name="setmp")
@commands.has_guild_permissions(administrator=True)
async def set_mp_command(context: commands.Context[Any], *, mp: str) -> None:
    mp = mp.strip().lower()
    if mp not in {"gs", "vch", "fura", "none"}:
        await context.reply("Формат: `!setmp gs|vch|fura|none`.", mention_author=False)
        return
    channel_id = str(context.channel.id)

    def update(settings: dict[str, Any]) -> None:
        settings.setdefault("mp_channels", {})
        if mp == "none":
            settings["mp_channels"].pop(channel_id, None)
        else:
            settings["mp_channels"][channel_id] = mp

    await bot.store.mutate("settings", update)
    await context.send(
        f"✅ Тег MP канала: **{'без тега' if mp == 'none' else mp.upper()}**."
    )


@bot.command(name="scan")
@commands.has_guild_permissions(administrator=True)
@commands.cooldown(1, 20, commands.BucketType.user)
async def scan_command(context: commands.Context[Any], *, opg_name: str) -> None:
    opg = canonical_opg(opg_name)
    if not opg:
        await context.reply("ОПГ: tambov / orekh / kurgan / kavkaz.", mention_author=False)
        return
    attachment: discord.Attachment | None = next(
        (
            item
            for item in context.message.attachments
            if (item.content_type or "").startswith("image/")
        ),
        None,
    )
    if attachment is None and context.message.reference:
        referenced = context.message.reference.resolved
        if not isinstance(referenced, discord.Message):
            try:
                referenced = await context.channel.fetch_message(
                    context.message.reference.message_id
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                referenced = None
        if isinstance(referenced, discord.Message):
            attachment = next(
                (
                    item
                    for item in referenced.attachments
                    if (item.content_type or "").startswith("image/")
                ),
                None,
            )
    if not attachment:
        await context.reply(
            "Прикрепите изображение к `!scan <опг>` или ответьте этой командой на сообщение со скрином.",
            mention_author=False,
        )
        return
    if attachment.size > MAX_SCAN_BYTES:
        await context.reply("Размер скриншота не должен превышать 10 МБ.", mention_author=False)
        return
    media_type = attachment.content_type or mimetypes.guess_type(attachment.filename)[0]
    if media_type not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        await context.reply("Поддерживаются PNG, JPEG, WEBP и GIF.", mention_author=False)
        return
    try:
        image = await attachment.read()
        nicks = await bot.ai.extract_nicks(image, media_type)
    except AIServiceError as exc:
        await context.reply(str(exc), mention_author=False)
        return
    except discord.HTTPException:
        await context.reply("Не удалось скачать изображение из Discord.", mention_author=False)
        return

    added: list[str] = []
    skipped = 0
    for nick in nicks:
        success, _result = await bot.add_member(nick, opg)
        if success:
            added.append(nick)
        else:
            skipped += 1
    visible = ", ".join(safe_text(name, limit=32) for name in added)
    report = (
        f"Нашёл **{len(nicks)}** ников; добавил **{len(added)}** в {gang_label(opg)}."
    )
    if skipped:
        report += f" Уже в составе или пропущено: {skipped}."
    if visible:
        report += f"\n{visible}"
    await context.send(report[:1900])


@bot.command(name="graph")
@commands.cooldown(1, 10, commands.BucketType.user)
async def graph_command(context: commands.Context[Any], *, kind: str) -> None:
    kind = kind.strip().lower()
    if kind not in {"kills", "weapons", "killers"}:
        await context.reply(
            "Формат: `!graph kills|weapons|killers`.", mention_author=False
        )
        return
    kills = await bot.store.get("kills")
    image = await create_graph(kind, kills)
    file = discord.File(io.BytesIO(image), filename=f"{kind}.png")
    embed = discord.Embed(
        title={
            "kills": "Киллы за последние 14 дней",
            "weapons": "Топ-10 оружия",
            "killers": "Топ-10 киллеров",
        }[kind],
        color=discord.Color.dark_teal(),
    )
    embed.set_image(url=f"attachment://{kind}.png")
    await context.send(embed=embed, file=file)


@bot.command(name="mode")
@commands.has_guild_permissions(administrator=True)
async def mode_command(context: commands.Context[Any]) -> None:
    def toggle(settings: dict[str, Any]) -> str:
        settings["mode"] = "ai" if settings.get("mode") != "ai" else "logic"
        return str(settings["mode"])

    mode = await bot.store.mutate("settings", toggle)
    await context.send(
        f"🧠 Режим: **{'Нейронка' if mode == 'ai' else 'Логика'}**. "
        "AI-команды запускаются только через `!ai` и `!scan`."
    )


@bot.command(name="ai")
@commands.has_guild_permissions(administrator=True)
@commands.cooldown(1, 5, commands.BucketType.user)
async def ai_command(context: commands.Context[Any], *, text: str) -> None:
    try:
        parsed = await bot.ai.parse_command(text)
    except AIServiceError as exc:
        await context.reply(str(exc), mention_author=False)
        return

    action = parsed["action"]
    nick = parsed["user"]
    opg = parsed["opg"]
    answer = parsed.get("answer", "")
    if action in {"answer", "unknown"} and answer:
        await context.reply(
            answer[:1800],
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    elif action == "add_kills":
        mp = canonical_mp(parsed.get("mp"))
        if mp is None:
            mp = canonical_mp(await bot.current_mp(context.channel.id))
        if mp is None:
            await context.reply(
                "Чтобы добавить лог, укажи `gs`, `vch` или `fura`, "
                "либо сначала задай MP каналу через `!setmp`.",
                mention_author=False,
            )
            return
        selected_date = parse_iso_date(parsed.get("date"))
        log_date = selected_date or datetime.now(UTC).date()
        source_prefix = (
            f"ai:{context.guild.id if context.guild else 0}:{context.message.id}"
        )
        events = parse_kill_lines(
            text,
            mp=mp,
            log_date=log_date,
            source_prefix=source_prefix,
        )
        if not events:
            await context.reply(
                "Не нашёл в сообщении строку лога в формате "
                "`21:09:47] Ник[ID] убил с помощью \"Оружие[ID]\" > Ник[ID]`.",
                mention_author=False,
            )
            return
        added = await bot.add_kill_events(events)
        await context.reply(
            f"✅ Добавлено в килл-лист **{mp.upper()}** за **{log_date.isoformat()}**: "
            f"**{added}** из {len(events)}. Дубликаты пропущены.",
            mention_author=False,
        )
    elif action == "add" and nick and opg:
        _ok, result = await bot.add_member(nick, opg)
        await context.send(result)
    elif action == "remove" and nick:
        _ok, result = await bot.remove_member(nick)
        await context.send(result)
    elif action == "move" and nick and opg:
        _ok, result = await bot.move_member(nick, opg)
        await context.send(result)
    elif action in {"ss", "ld"} and nick:
        label = "СС" if action == "ss" else "ЛД"
        ok, error, name, assigned_opg, old_leader = await bot.assign_label(nick, label)
        if not ok or not name or not assigned_opg:
            await context.send(error or "Не удалось назначить метку.")
            return
        title = (
            f"⭐ {safe_text(name)} назначен старшим составом {gang_label(assigned_opg)}"
            if label == "СС"
            else f"👑 {safe_text(name)} назначен лидером {gang_label(assigned_opg)}"
        )
        embed = discord.Embed(
            title=title,
            color=discord.Color.gold() if label == "ЛД" else discord.Color.blue(),
            timestamp=datetime.now(UTC),
        )
        if old_leader:
            embed.description = f"Прежний лидер {safe_text(old_leader)} снят."
        await context.send(embed=embed)
    else:
        await context.reply(
            "Не получилось подготовить ответ. Попробуйте переформулировать вопрос.",
            mention_author=False,
        )


async def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN отсутствует. Добавьте токен в переменные окружения хостинга (DISCORD_TOKEN)."
        )
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
