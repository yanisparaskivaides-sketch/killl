"""Interactive dashboard, gang roster, and filtered top-list Discord views."""

from __future__ import annotations

import discord

from bot.embeds import (
    RGB_STYLES,
    STYLE_LABELS,
    build_dashboard_embed,
    build_recent_kills_embed,
    build_roster_embed,
    build_top_embed,
)
from bot.kill_import import parse_iso_date
from bot.utils import GANGS, canonical_opg


async def _deny_unless_admin(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Это действие доступно только администраторам сервера.",
            ephemeral=True,
        )
        return False
    return True


class ReturnToMenuView(discord.ui.View):
    def __init__(self, bot: "KillListBot"):
        super().__init__(timeout=300)
        self.bot = bot

    @discord.ui.button(label="⬅️ Назад", style=discord.ButtonStyle.secondary)
    async def back(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        embed = await build_dashboard_embed(self.bot)
        await interaction.response.edit_message(embed=embed, view=MainMenuView(self.bot))
        await self.bot.start_rgb(interaction.message)


class MainMenuView(discord.ui.View):
    def __init__(self, bot: "KillListBot"):
        super().__init__(timeout=600)
        self.bot = bot

    @discord.ui.button(label="🧠 Режим", style=discord.ButtonStyle.primary, row=0)
    async def mode(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not await _deny_unless_admin(interaction):
            return
        def change(settings: dict) -> None:
            settings["mode"] = "ai" if settings.get("mode") != "ai" else "logic"

        await self.bot.store.mutate("settings", change)
        await interaction.response.edit_message(
            embed=await build_dashboard_embed(self.bot), view=self
        )

    @discord.ui.button(label="💀 Киллы", style=discord.ButtonStyle.danger, row=0)
    async def kills(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.bot.cancel_rgb(interaction.channel_id)
        await interaction.response.edit_message(
            embed=await build_recent_kills_embed(self.bot),
            view=ReturnToMenuView(self.bot),
        )

    @discord.ui.button(label="🏆 Топы", style=discord.ButtonStyle.success, row=0)
    async def tops(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self.bot.cancel_rgb(interaction.channel_id)
        view = TopView(self.bot)
        await interaction.response.edit_message(
            embed=await build_top_embed(self.bot), view=view
        )

    @discord.ui.button(label="🎨 Стиль", style=discord.ButtonStyle.secondary, row=1)
    async def style(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not await _deny_unless_admin(interaction):
            return

        def change(settings: dict) -> None:
            current = settings.get("style", "smooth")
            index = RGB_STYLES.index(current) if current in RGB_STYLES else -1
            settings["style"] = RGB_STYLES[(index + 1) % len(RGB_STYLES)]

        await self.bot.store.mutate("settings", change)
        await interaction.response.edit_message(
            embed=await build_dashboard_embed(self.bot), view=self
        )

    @discord.ui.select(
        placeholder="Выберите ОПГ",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(
                label=gang["name"],
                value=opg,
                emoji=gang["emoji"],
            )
            for opg, gang in GANGS.items()
        ],
        row=2,
    )
    async def gang_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        opg = select.values[0]
        self.bot.cancel_rgb(interaction.channel_id)
        await interaction.response.edit_message(
            embed=await build_roster_embed(self.bot, opg),
            view=GangView(self.bot, opg),
        )


class GangView(discord.ui.View):
    def __init__(self, bot: "KillListBot", opg: str):
        super().__init__(timeout=600)
        self.bot = bot
        self.opg = opg

    @discord.ui.button(label="➕ Добавить", style=discord.ButtonStyle.success, row=0)
    async def add(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if await _deny_unless_admin(interaction):
            await interaction.response.send_modal(
                AddMemberModal(self.bot, self.opg, interaction.message)
            )

    @discord.ui.button(label="➖ Удалить", style=discord.ButtonStyle.danger, row=0)
    async def remove(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if await _deny_unless_admin(interaction):
            await interaction.response.send_modal(
                RemoveMemberModal(self.bot, self.opg, interaction.message)
            )

    @discord.ui.button(label="🔁 Перевести", style=discord.ButtonStyle.primary, row=0)
    async def move(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if await _deny_unless_admin(interaction):
            await interaction.response.send_modal(
                MoveMemberModal(self.bot, self.opg, interaction.message)
            )

    @discord.ui.button(label="⬅️ Назад", style=discord.ButtonStyle.secondary, row=0)
    async def back(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            embed=await build_dashboard_embed(self.bot),
            view=MainMenuView(self.bot),
        )
        await self.bot.start_rgb(interaction.message)


class AddMemberModal(discord.ui.Modal, title="Добавить участника"):
    nick = discord.ui.TextInput(
        label="Игровой ник",
        placeholder="Например, Player_15",
        min_length=1,
        max_length=32,
    )

    def __init__(
        self, bot: "KillListBot", opg: str, message: discord.Message | None
    ):
        super().__init__()
        self.bot = bot
        self.opg = opg
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        _ok, result = await self.bot.add_member(str(self.nick), self.opg)
        await interaction.response.send_message(result, ephemeral=True)
        if self.message:
            await self.message.edit(
                embed=await build_roster_embed(self.bot, self.opg),
                view=GangView(self.bot, self.opg),
            )


class RemoveMemberModal(discord.ui.Modal, title="Удалить участника"):
    nick = discord.ui.TextInput(
        label="Игровой ник",
        placeholder="Введите точный ник участника",
        min_length=1,
        max_length=32,
    )

    def __init__(
        self, bot: "KillListBot", opg: str, message: discord.Message | None
    ):
        super().__init__()
        self.bot = bot
        self.opg = opg
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        _ok, message = await self.bot.remove_member(str(self.nick))
        await interaction.response.send_message(message, ephemeral=True)
        if self.message:
            await self.message.edit(
                embed=await build_roster_embed(self.bot, self.opg),
                view=GangView(self.bot, self.opg),
            )


class MoveMemberModal(discord.ui.Modal, title="Перевести участника"):
    nick = discord.ui.TextInput(
        label="Игровой ник",
        placeholder="Введите точный ник участника",
        min_length=1,
        max_length=32,
    )
    target_opg = discord.ui.TextInput(
        label="Новая ОПГ",
        placeholder="tambov / orekh / kurgan / kavkaz",
        min_length=3,
        max_length=24,
    )

    def __init__(
        self, bot: "KillListBot", opg: str, message: discord.Message | None
    ):
        super().__init__()
        self.bot = bot
        self.opg = opg
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        opg = canonical_opg(str(self.target_opg))
        if not opg:
            await interaction.response.send_message(
                "ОПГ не распознана. Допустимы tambov, orekh, kurgan и kavkaz.",
                ephemeral=True,
            )
            return
        _ok, message = await self.bot.move_member(str(self.nick), opg)
        await interaction.response.send_message(message, ephemeral=True)
        if self.message:
            await self.message.edit(
                embed=await build_roster_embed(self.bot, self.opg),
                view=GangView(self.bot, self.opg),
            )


class TopView(discord.ui.View):
    def __init__(
        self,
        bot: "KillListBot",
        scope: str = "all",
        mp: str = "all",
        count: int = 3,
    ):
        super().__init__(timeout=600)
        self.bot = bot
        self.scope = scope
        self.mp = mp
        self.count = count
        self._mark_active_buttons()

    def _mark_active_buttons(self) -> None:
        scope_by_label = {"День": "day", "Неделя": "week", "Всё": "all"}
        mp_by_label = {"GS": "gs", "VCh": "vch", "Фура": "fura", "Все MP": "all"}
        for child in self.children:
            if not isinstance(child, discord.ui.Button):
                continue
            label = str(child.label)
            if label in scope_by_label:
                child.style = (
                    discord.ButtonStyle.primary
                    if scope_by_label[label] == self.scope
                    else discord.ButtonStyle.secondary
                )
            elif label in mp_by_label:
                child.style = (
                    discord.ButtonStyle.primary
                    if mp_by_label[label] == self.mp
                    else discord.ButtonStyle.secondary
                )
            elif label == "Дата":
                child.style = (
                    discord.ButtonStyle.primary
                    if parse_iso_date(self.scope)
                    else discord.ButtonStyle.secondary
                )

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._mark_active_buttons()
        await interaction.response.edit_message(
            embed=await build_top_embed(
                self.bot, self.scope, self.count, self.mp
            ),
            view=self,
        )

    @discord.ui.button(label="День", style=discord.ButtonStyle.secondary, row=0)
    async def day(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.scope = "day"
        await self._refresh(interaction)

    @discord.ui.button(label="Неделя", style=discord.ButtonStyle.secondary, row=0)
    async def week(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.scope = "week"
        await self._refresh(interaction)

    @discord.ui.button(label="Всё", style=discord.ButtonStyle.primary, row=0)
    async def all_time(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.scope = "all"
        await self._refresh(interaction)

    @discord.ui.button(label="GS", style=discord.ButtonStyle.secondary, row=1)
    async def gs(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.mp = "gs"
        await self._refresh(interaction)

    @discord.ui.button(label="VCh", style=discord.ButtonStyle.secondary, row=1)
    async def vch(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.mp = "vch"
        await self._refresh(interaction)

    @discord.ui.button(label="Фура", style=discord.ButtonStyle.secondary, row=1)
    async def fura(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.mp = "fura"
        await self._refresh(interaction)

    @discord.ui.button(label="Все MP", style=discord.ButtonStyle.primary, row=1)
    async def all_mp(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.mp = "all"
        await self._refresh(interaction)

    @discord.ui.button(label="Топ-N", style=discord.ButtonStyle.success, row=2)
    async def top_n(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(TopNModal(self, interaction.message))

    @discord.ui.button(label="Дата", style=discord.ButtonStyle.secondary, row=2)
    async def choose_date(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(TopDateModal(self, interaction.message))

    @discord.ui.button(label="⬅️ Назад", style=discord.ButtonStyle.secondary, row=2)
    async def back(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            embed=await build_dashboard_embed(self.bot),
            view=MainMenuView(self.bot),
        )
        await self.bot.start_rgb(interaction.message)


class TopDateModal(discord.ui.Modal, title="Килл-лист за дату"):
    selected_date = discord.ui.TextInput(
        label="Дата (ГГГГ-ММ-ДД)",
        placeholder="2026-10-11",
        min_length=10,
        max_length=10,
    )

    def __init__(self, view: TopView, message: discord.Message | None):
        super().__init__()
        self.top_view = view
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        selected = str(self.selected_date).strip()
        if parse_iso_date(selected) is None:
            await interaction.response.send_message(
                "Введите дату в формате `ГГГГ-ММ-ДД`.",
                ephemeral=True,
            )
            return
        self.top_view.scope = selected
        updated_view = TopView(
            self.top_view.bot,
            self.top_view.scope,
            self.top_view.mp,
            self.top_view.count,
        )
        await interaction.response.send_message(
            f"✅ Выбрана дата **{selected}**.",
            ephemeral=True,
        )
        if self.message:
            await self.message.edit(
                embed=await build_top_embed(
                    self.top_view.bot,
                    self.top_view.scope,
                    self.top_view.count,
                    self.top_view.mp,
                ),
                view=updated_view,
            )


class TopNModal(discord.ui.Modal, title="Размер топа"):
    count = discord.ui.TextInput(
        label="Количество мест (от 1 до 20)",
        placeholder="3",
        min_length=1,
        max_length=2,
        default="3",
    )

    def __init__(self, view: TopView, message: discord.Message | None):
        super().__init__()
        self.top_view = view
        self.message = message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            count = int(str(self.count).strip())
        except ValueError:
            count = 0
        if not 1 <= count <= 20:
            await interaction.response.send_message(
                "Введите целое число от 1 до 20.", ephemeral=True
            )
            return
        self.top_view.count = count
        updated_view = TopView(
            self.top_view.bot,
            self.top_view.scope,
            self.top_view.mp,
            self.top_view.count,
        )
        await interaction.response.send_message(
            "✅ Размер топа обновлён.",
            ephemeral=True,
        )
        if self.message:
            await self.message.edit(
                embed=await build_top_embed(
                    self.top_view.bot,
                    self.top_view.scope,
                    self.top_view.count,
                    self.top_view.mp,
                ),
                view=updated_view,
            )

