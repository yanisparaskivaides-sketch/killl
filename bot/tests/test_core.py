from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.ai_client import AIClient, _friendly_provider_error, _provider_error_code
from bot.config import OPENROUTER_BASE_URL
from bot.main import KillListBot, bot as discord_bot
from bot.kill_import import parse_import_args, parse_kill_lines
from bot.parser import parse_kill_log
from bot.stats import filter_kills, kill_totals
from bot.storage import AsyncJsonStore
from bot.views import MainMenuView, TopView


class ParserTests(unittest.TestCase):
    def test_parses_kill_record_and_uses_utc_date(self) -> None:
        created = datetime(2026, 10, 10, 21, 0, tzinfo=UTC)
        parsed = parse_kill_log(
            '[21:00:15] Nick_1[118] убил с помощью "AK-47[24]" > Victim[414]',
            mp="gs",
            created_at=created,
            message_id=1234,
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["date"], "2026-10-10")
        self.assertEqual(parsed["mp"], "gs")
        self.assertEqual(parsed["killer"], {"name": "Nick_1", "id": 118})
        self.assertEqual(parsed["weapon"], {"name": "AK-47", "id": 24})
        self.assertEqual(parsed["victim"], {"name": "Victim", "id": 414})
        self.assertEqual(parsed["source_message_id"], "1234")

    def test_non_kill_text_is_ignored(self) -> None:
        self.assertIsNone(parse_kill_log("Обычное сообщение убил"))


class StatsTests(unittest.TestCase):
    def test_leaderboard_filters_period_and_merges_name_case(self) -> None:
        now = int(datetime.now(UTC).timestamp())
        kills = [
            {"ts": now - 20, "mp": "gs", "killer": {"name": "Player"}},
            {"ts": now - 40, "mp": "gs", "killer": {"name": "player"}},
            {"ts": now - 8 * 86400, "mp": "gs", "killer": {"name": "Old"}},
            {"ts": now - 10, "mp": "fura", "killer": {"name": "Road"}},
        ]
        recent_gs = filter_kills(kills, "day", "gs")
        self.assertEqual(len(recent_gs), 2)
        self.assertEqual(kill_totals(recent_gs), {"Player": 2})

    def test_leaderboard_can_filter_one_date_and_mp(self) -> None:
        kills = [
            {"date": "2026-10-11", "mp": "gs", "killer": {"name": "Player"}},
            {"date": "2026-10-10", "mp": "gs", "killer": {"name": "Old"}},
            {"date": "2026-10-11", "mp": "fura", "killer": {"name": "Road"}},
        ]
        matching = filter_kills(kills, "2026-10-11", "gs")
        self.assertEqual(kill_totals(matching), {"Player": 1})

    def test_top_view_exposes_a_specific_date_filter(self) -> None:
        view = TopView(discord_bot)
        labels = [str(child.label) for child in view.children if child.label]
        self.assertIn("Дата", labels)


class KillImportTests(unittest.TestCase):
    def test_import_header_selects_mp_date_and_keeps_inline_log(self) -> None:
        mp, selected_date, body = parse_import_args(
            'гс 2026-10-11\n21:09:47] Arsenii_Hitman[141] убил с помощью '
            '"Desert Eagle[24]" > Vadim0077[37]'
        )
        self.assertEqual(mp, "gs")
        self.assertEqual(selected_date.isoformat() if selected_date else None, "2026-10-11")
        self.assertTrue(body.startswith("21:09:47]"))

    def test_import_parser_uses_selected_mp_and_date(self) -> None:
        events = parse_kill_lines(
            '21:09:47] Arsenii_Hitman[141] убил с помощью '
            '"Desert Eagle[24]" > Vadim0077[37]',
            mp="vch",
            log_date=datetime(2026, 10, 11, tzinfo=UTC).date(),
            source_prefix="test-import",
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["mp"], "vch")
        self.assertEqual(events[0]["date"], "2026-10-11")


class OpenRouterErrorTests(unittest.TestCase):
    def test_empty_openrouter_balance_shows_billing_guidance(self) -> None:
        for code in ("insufficient_quota", "credit_balance_exhausted"):
            with self.subTest(code=code):
                error = SimpleNamespace(
                    status_code=429,
                    code=None,
                    body={"error": {"code": code}},
                )
                self.assertEqual(_provider_error_code(error), code)
                message = _friendly_provider_error(error)
                self.assertIn("кредиты", message.lower())
                self.assertIn("openrouter", message.lower())

    def test_generic_rate_limit_is_not_misreported_as_bad_credentials(self) -> None:
        error = SimpleNamespace(status_code=429, code="rate_limit_exceeded", body=None)
        message = _friendly_provider_error(error)
        self.assertIn("HTTP 429", message)
        self.assertNotIn("API-ключ", message)

    def test_openrouter_base_url_is_configured(self) -> None:
        self.assertEqual(OPENROUTER_BASE_URL, "https://openrouter.ai/api/v1")


class AIPromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_general_question_produces_a_chat_answer(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"action":"answer","user":"","opg":null,"answer":"Всё хорошо, спасибо!"}'
                    )
                )
            ]
        )
        create = AsyncMock(return_value=response)
        client = AIClient.__new__(AIClient)
        client.model = "openai/gpt-4o-mini"
        client._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=create),
            )
        )

        result = await client.parse_command("как дела брат?")

        self.assertEqual(result["action"], "answer")
        self.assertEqual(result["answer"], "Всё хорошо, спасибо!")
        self.assertEqual(
            create.await_args.kwargs["messages"][1]["content"],
            "как дела брат?",
        )

    async def test_ai_import_action_normalizes_mp_and_date(self) -> None:
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"action":"add_kills","user":"","opg":null,'
                            '"answer":"","mp":"ГС","date":"2026-10-11"}'
                        )
                    )
                )
            ]
        )
        client = AIClient.__new__(AIClient)
        client.model = "openai/gpt-4o-mini"
        client._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(return_value=response)),
            )
        )

        result = await client.parse_command("Добавь этот лог в ГС за 2026-10-11")

        self.assertEqual(result["action"], "add_kills")
        self.assertEqual(result["mp"], "gs")
        self.assertEqual(result["date"], "2026-10-11")

    async def test_ai_command_replies_to_general_answer(self) -> None:
        context = SimpleNamespace(reply=AsyncMock(), send=AsyncMock())
        command = discord_bot.get_command("ai")
        self.assertIsNotNone(command)
        assert command is not None

        with patch.object(
            discord_bot.ai,
            "parse_command",
            AsyncMock(
                return_value={
                    "action": "unknown",
                    "user": "",
                    "opg": None,
                    "answer": "Всё хорошо!",
                }
            ),
        ):
            await command.callback(context, text="как дела брат?")

        context.reply.assert_awaited_once()
        self.assertEqual(context.reply.await_args.args[0], "Всё хорошо!")
        context.send.assert_not_awaited()

    async def test_ai_command_imports_a_log_only_after_explicit_request(self) -> None:
        context = SimpleNamespace(
            reply=AsyncMock(),
            send=AsyncMock(),
            message=SimpleNamespace(id=123),
            guild=SimpleNamespace(id=456),
        )
        command = discord_bot.get_command("ai")
        self.assertIsNotNone(command)
        assert command is not None
        text = (
            'Добавь в килл-лист GS за 2026-10-11: '
            '21:09:47] Arsenii_Hitman[141] убил с помощью '
            '"Desert Eagle[24]" > Vadim0077[37]'
        )

        with (
            patch.object(
                discord_bot.ai,
                "parse_command",
                AsyncMock(
                    return_value={
                        "action": "add_kills",
                        "user": "",
                        "opg": None,
                        "answer": "",
                        "mp": "gs",
                        "date": "2026-10-11",
                    }
                ),
            ),
            patch.object(
                discord_bot,
                "add_kill_events",
                AsyncMock(return_value=1),
            ) as add_events,
        ):
            await command.callback(context, text=text)

        events = add_events.await_args.args[0]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["killer"]["name"], "Arsenii_Hitman")
        self.assertEqual(events[0]["mp"], "gs")
        self.assertEqual(events[0]["date"], "2026-10-11")
        context.reply.assert_awaited_once()


class KillImportCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_command_imports_plain_text_for_selected_mp_and_day(self) -> None:
        context = SimpleNamespace(
            reply=AsyncMock(),
            message=SimpleNamespace(id=321, attachments=[]),
            guild=SimpleNamespace(id=654),
        )
        command = discord_bot.get_command("addkills")
        self.assertIsNotNone(command)
        assert command is not None
        log_text = (
            "gs 2026-10-11\n"
            '21:09:47] Arsenii_Hitman[141] убил с помощью '
            '"Desert Eagle[24]" > Vadim0077[37]'
        )

        with patch.object(
            discord_bot,
            "add_kill_events",
            AsyncMock(return_value=1),
        ) as add_events:
            await command.callback(context, args=log_text)

        events = add_events.await_args.args[0]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["mp"], "gs")
        self.assertEqual(events[0]["date"], "2026-10-11")
        context.reply.assert_awaited_once()

    async def test_command_reads_txt_attachment(self) -> None:
        file_text = (
            '[21:09:47] Arsenii_Hitman[141] убил с помощью '
            '"Desert Eagle[24]" > Vadim0077[37]'
        )
        attachment = SimpleNamespace(
            filename="kills.TXT",
            size=len(file_text.encode("utf-8")),
            read=AsyncMock(return_value=file_text.encode("utf-8")),
        )
        context = SimpleNamespace(
            reply=AsyncMock(),
            message=SimpleNamespace(id=322, attachments=[attachment]),
            guild=SimpleNamespace(id=654),
        )
        command = discord_bot.get_command("addkills")
        self.assertIsNotNone(command)
        assert command is not None

        with patch.object(
            discord_bot,
            "add_kill_events",
            AsyncMock(return_value=1),
        ) as add_events:
            await command.callback(context, args="vch 2026-10-10")

        events = add_events.await_args.args[0]
        self.assertEqual(events[0]["mp"], "vch")
        self.assertEqual(events[0]["date"], "2026-10-10")
        attachment.read.assert_awaited_once()


class CommandSurfaceTests(unittest.TestCase):
    def test_help_is_available_and_contract_commands_are_removed(self) -> None:
        command_names = set(discord_bot.all_commands)
        self.assertIn("help", command_names)
        self.assertIn("addkills", command_names)
        self.assertIn("addlog", command_names)
        self.assertFalse({"contract", "contracts", "uncontract"} & command_names)

    def test_main_menu_has_no_contract_button(self) -> None:
        labels = [
            str(child.label)
            for child in MainMenuView(discord_bot).children
            if getattr(child, "label", None)
        ]
        self.assertFalse(any("контракт" in label.casefold() for label in labels))


class BotDataTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.bot = KillListBot()
        self.bot.store = AsyncJsonStore(root / "data", root / "backups")
        await self.bot.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.bot.close()
        self.tempdir.cleanup()

    async def test_one_leader_per_gang_and_public_transition_data(self) -> None:
        self.assertTrue((await self.bot.add_member("OldLead", "tambov"))[0])
        self.assertTrue((await self.bot.add_member("NewLead", "tambov"))[0])
        old_result = await self.bot.assign_label("OldLead", "ЛД")
        self.assertEqual(old_result, (True, "", "OldLead", "tambov", None))
        new_result = await self.bot.assign_label("NewLead", "ЛД")
        self.assertEqual(new_result, (True, "", "NewLead", "tambov", "OldLead"))
        members = await self.bot.store.get("members")
        labels = {member["name"]: member["label"] for member in members}
        self.assertEqual(labels, {"OldLead": "", "NewLead": "ЛД"})

    async def test_legacy_contract_data_is_preserved_without_active_feature(self) -> None:
        root = Path(self.tempdir.name)
        data_dir = root / "legacy-data"
        data_dir.mkdir()
        contract_file = data_dir / "contracts.json"
        contract_file.write_text(
            '[{"id":"legacy-1","status":"active","target":"OldTarget"}]\n',
            encoding="utf-8",
        )

        archived_store = AsyncJsonStore(data_dir, root / "legacy-backups")
        await archived_store.initialize()
        await archived_store.flush()
        contents = contract_file.read_text(encoding="utf-8")
        self.assertIn("legacy-1", contents)
        self.assertIn("OldTarget", contents)
        snapshot = await archived_store.backup()
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(
            (snapshot / "contracts.json").read_text(encoding="utf-8"),
            contents,
        )

    async def test_member_names_are_unique_across_gangs(self) -> None:
        self.assertTrue((await self.bot.add_member("CaseTest", "tambov"))[0])
        ok, message = await self.bot.add_member("casetest", "kurgan")
        self.assertFalse(ok)
        self.assertIn("уже состоит", message)

    async def test_kill_import_skips_duplicate_records(self) -> None:
        event = parse_kill_log(
            '[21:00:15] Nick_1[118] убил с помощью "AK-47[24]" > Victim[414]',
            mp="gs",
            created_at=datetime(2026, 10, 10, tzinfo=UTC),
            message_id="import:first",
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(await self.bot.add_kill_events([event, event]), 1)

        repeated = dict(event)
        repeated["source_message_id"] = "import:second"
        self.assertEqual(await self.bot.add_kill_events([repeated]), 0)
        self.assertEqual(len(await self.bot.store.get("kills")), 1)


if __name__ == "__main__":
    unittest.main()
