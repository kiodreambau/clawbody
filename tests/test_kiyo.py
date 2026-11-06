"""Regression checks for the shared Kiyo entry points."""

import os
import stat
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kiyo import backends, calendar
from kiyo.assistant import Assistant
from kiyo.cli import build_parser, dispatch
from kiyo.robot import WAKE
from kiyo.settings import KiyoError, Settings


class AssistantTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"KIYO_CONFIG": temp.name + "/config.toml", "KIYO_STATE_DIR": temp.name})
        env.start()
        self.addCleanup(env.stop)
        self.settings = Settings()
        self.assistant = Assistant(self.settings)

    def test_followup_survives_interface_switch_and_restart(self):
        with patch.object(backends, "reply", return_value="Merke ich mir.") as reply:
            self.assistant.ask("Merke dir Wintergarten.")
            self.assertIn("Schnittstelle", self.assistant.ask("Wechsle zu OpenClaw"))
            restarted = Assistant(Settings())
            self.assertIn("OpenClaw", restarted.ask("über lokal"))
            restarted.ask("Was habe ich gesagt?", voice=True)
            call = reply.call_args
            self.assertEqual(call.args[1], "openclaw")
            self.assertEqual(call.args[2][0]["content"], "Merke dir Wintergarten.")
            self.assertTrue(call.kwargs["voice"])
        path = self.settings.state / "conversation.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_pending_task_waits_for_interface(self):
        with patch.object(backends, "reply", return_value="Erledigt.") as reply:
            self.assertIn("Schnittstelle", self.assistant.ask("Nutze Hermes. Antworte mit Hallo."))
            reply.assert_not_called()
            self.assertEqual(self.assistant.ask("API"), "Erledigt.")
            self.assertEqual(reply.call_args.args[3], "Antworte mit Hallo.")

    def test_new_conversation_discards_pending_task(self):
        self.assistant.ask("Nutze OpenClaw. Merke dir Wintergarten.")
        self.assistant.new()
        self.assertIsNone(self.assistant.status()["pending"])
        with patch.object(backends, "reply", return_value="OK") as reply:
            self.assistant.ask("Weiter.")
        self.assertEqual(reply.call_args.args[2], [])

    def test_backend_failure_does_not_try_another_agent_or_append_history(self):
        with patch.object(backends, "reply", side_effect=KiyoError("Offline")) as reply:
            with self.assertRaisesRegex(KiyoError, "Offline"):
                self.assistant.ask("Nutze OpenClaw über lokal. Wo waren wir?")
            reply.assert_called_once()
        self.assertEqual(self.assistant.status()["backend"], "openclaw")
        with self.assistant.locked_state() as state:
            self.assertEqual(state["history"], [])

    def test_calendar_stays_out_of_agent_context(self):
        with patch.object(calendar, "agenda", return_value="Zehn Uhr: Termin") as agenda:
            with patch.object(backends, "reply") as reply:
                self.assertEqual(self.assistant.ask("Was steht morgen im Kalender?", voice=True), "Zehn Uhr: Termin")
        agenda.assert_called_once_with(self.settings, "morgen")
        reply.assert_not_called()

    def test_spoken_task_only_runs_after_terminal_approval(self):
        with patch.object(backends, "reply", return_value="Geprüft.") as reply:
            self.assertIn("vorgemerkt", self.assistant.ask("Gib OpenClaw den Auftrag: prüfe Status", voice=True))
            reply.assert_not_called()
            task_id, task = next(iter(self.assistant.tasks().items()))
            self.assertEqual(task["status"], "pending")
            result = self.assistant.approve_task(task_id)
            self.assertEqual(result["session"], task["session"])
            reply.assert_called_once()
            with self.assertRaises(KiyoError):
                self.assistant.approve_task(task_id)

    def test_spoken_child_task_keeps_the_selected_gateway_until_approval(self):
        self.settings.values.update(
            openclaw_sessions={"planung": "agent:ops:calendar"},
            openclaw_profiles={"server": "dreambau"},
        )
        self.assistant.use("openclaw", "server")
        with (
            patch.object(backends, "prepare_session") as prepare,
            patch.object(backends, "reply", return_value="Geprüft.") as reply,
        ):
            self.assistant.ask("Gib OpenClaw unter Planung den Auftrag: prüfe Status", voice=True)
            prepare.assert_not_called()
            self.assistant.use("hermes", "api")
            task_id = next(iter(self.assistant.tasks()))
            result = self.assistant.approve_task(task_id)
        self.assertTrue(result["session"].startswith("agent:ops:kiyo-task-"))
        prepare.assert_called_once_with(
            self.settings,
            result["session"],
            parent="agent:ops:calendar",
            node=None,
            profile="dreambau",
        )
        self.assertEqual(reply.call_args.kwargs["profile"], "dreambau")

    def test_delegate_does_not_send_to_node_without_execution_capability(self):
        args = build_parser().parse_args(["delegate", "--node", "M4", "Status"])
        nodes = {"nodes": [{"displayName": "M4", "connected": True, "paired": True, "commands": []}]}
        with patch.object(backends, "claw_call", return_value=nodes) as rpc, patch.object(backends, "reply") as reply:
            with self.assertRaisesRegex(KiyoError, "keine Befehlsausführung"):
                dispatch(args, self.settings)
        rpc.assert_called_once_with(self.settings, "node.list", profile=None)
        reply.assert_not_called()

    def test_child_creation_verifies_lineage_before_sending(self):
        parent = "agent:main:root"
        child = "agent:main:child"
        with patch.object(
            backends,
            "claw_call",
            side_effect=[{"session": {"key": parent, "spawnDepth": 2}}, {"key": child}, {"session": {"key": child}}],
        ) as rpc:
            with self.assertRaisesRegex(KiyoError, "übergeordnete Sitzung"):
                backends.prepare_session(self.settings, child, parent=parent)
        self.assertEqual(rpc.call_args_list[1].args[2]["spawnDepth"], 3)

    def test_session_history_filters_tools_and_hidden_messages(self):
        key = "agent:main:root"
        messages = [
            {"role": "tool", "content": "private log"},
            {"role": "assistant", "display": False, "content": "hidden message"},
            {"role": "assistant", "content": [{"type": "text", "text": "sichtbar"}]},
        ]
        with patch.object(backends, "claw_call", side_effect=[{"session": {"key": key}}, {"messages": messages}]):
            history = backends.session_history(self.settings, key, 10)
        self.assertEqual(history, [{"role": "assistant", "text": "sichtbar"}])

    def test_remote_codex_history_reads_only_recent_conversation_items(self):
        payload = {
            "data": [
                {
                    "items": [
                        {"type": "userMessage", "content": [{"type": "text", "text": "Frage"}]},
                        {"type": "commandExecution", "aggregatedOutput": "tool log"},
                        {"type": "agentMessage", "text": "Antwort"},
                    ]
                },
            ]
        }
        args = build_parser().parse_args(["remote-history", "codex", "thread-1", "--node", "M4", "--limit", "1"])
        with patch.object(backends, "node_payload", return_value=payload) as rpc:
            history = dispatch(args, self.settings)
        self.assertEqual(history, [{"role": "assistant", "text": "Antwort"}])
        rpc.assert_called_once_with(
            self.settings,
            "M4",
            "codex.appServer.thread.turns.list.v1",
            {"threadId": "thread-1", "limit": 1},
            profile=None,
        )

    def test_remote_claude_history_rejects_a_different_session(self):
        with patch.object(backends, "node_payload", return_value={"threadId": "other", "items": []}):
            with self.assertRaisesRegex(KiyoError, "gewählte Claude-Sitzung"):
                backends.remote_history(self.settings, "claude", "M4", "thread-1", 1)

    def test_voice_agents_fail_closed_without_restricted_profiles(self):
        with self.assertRaisesRegex(KiyoError, "eigenes Hermes-Profil"):
            backends.reply(self.settings, "hermes", [], "Hallo", "probe", voice=True)
        with self.assertRaisesRegex(KiyoError, "Kiyo-Terminal"):
            backends.reply(self.settings, "openclaw", [], "Hallo", "probe", voice=True)


class WakeTest(unittest.TestCase):
    def test_recognizes_name_at_start_and_as_part_of_greeting(self):
        for text in ("Kio, hallo", "Hey Kiyo.", "Herr Kiyo, sag Hallo."):
            self.assertIsNotNone(WAKE.match(text))
        self.assertIsNone(WAKE.match("Ich habe Kiyo gesagt."))


class CalendarTest(unittest.TestCase):
    def test_window_keeps_local_midnight_at_dst_transition(self):
        zone = ZoneInfo("Europe/Berlin")
        start, end = calendar.window("heute", zone, now=datetime(2026, 10, 25, 12, tzinfo=zone))
        self.assertEqual(start.isoformat(), "2026-10-25T00:00:00+02:00")
        self.assertEqual(end.isoformat(), "2026-10-26T00:00:00+01:00")

    def test_calendar_read_request_is_separate_from_write_request(self):
        self.assertEqual(calendar.requested_day("Lies mir die Termine übermorgen vor"), "übermorgen")
        self.assertIsNone(calendar.requested_day("Verschiebe meinen Kalendertermin morgen"))

    def test_event_timezone_and_all_day(self):
        events = [
            {"summary": "Ganztagstermin", "start": {"date": "2026-11-06"}},
            {"summary": "Probe", "start": {"dateTime": "2026-11-06T08:00:00Z"}},
        ]
        self.assertEqual(
            calendar.format_events(events, ZoneInfo("Europe/Berlin"), "heute"),
            "Ganztägig: Ganztagstermin\n09:00: Probe",
        )


if __name__ == "__main__":
    unittest.main()
