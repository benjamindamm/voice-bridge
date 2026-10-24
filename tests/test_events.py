"""Tests für Event-Meilensteine und den Narrator (ohne SSE)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from voicebridge.config import Config
from voicebridge.events import (
    Narrator,
    phrase_for_subagent,
    phrase_for_tool,
    subagent_call,
)

BASIS = (
    "VOICE_STT_URL=http://h:8792/v1\n"
    "VOICE_TTS_URL=http://h:8791/v1\n"
    "VOICE_API_KEY=test-key-1234\n"
)


def make_config(tmp: str) -> Config:
    path = Path(tmp) / ".env"
    path.write_text(BASIS, encoding="utf-8")
    return Config.load(path)


class FakeClient:
    def __init__(self) -> None:
        self.endpoint = SimpleNamespace(url="http://test", password="pw")


class PhraseTests(unittest.TestCase):
    def test_tools(self) -> None:
        self.assertEqual(phrase_for_tool("read"), "Ich schaue nach.")
        self.assertEqual(phrase_for_tool("glob"), "Ich schaue nach.")
        self.assertEqual(phrase_for_tool("grep"), "Ich schaue nach.")
        self.assertEqual(phrase_for_tool("edit"), "Ich ändere etwas.")
        self.assertEqual(phrase_for_tool("write"), "Ich ändere etwas.")
        self.assertEqual(phrase_for_tool("shell"), "Ich führe einen Befehl aus.")
        self.assertEqual(phrase_for_tool("subagent"), "Ich starte einen Helfer.")
        self.assertEqual(phrase_for_tool("webfetch"), "Ich recherchiere im Web.")
        self.assertEqual(phrase_for_tool("unbekannt"), "Ich arbeite weiter.")


class NarratorTests(unittest.TestCase):
    def test_eigene_session_und_tool_zuordnung(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            narrator = Narrator(cfg, FakeClient(), "ses_1", lambda text, stop: None)

        narrator._handle({"type": "session.execution.started", "data": {"sessionID": "ses_1"}})
        self.assertIsNone(narrator._pending)
        self.assertTrue(narrator._pending_signal)

        narrator._handle(
            {
                "type": "session.tool.input.started",
                "data": {"sessionID": "ses_1", "id": "t1", "name": "read"},
            }
        )
        self.assertIsNone(narrator._pending)

        narrator._handle(
            {"type": "session.tool.called", "data": {"sessionID": "ses_1", "id": "t1"}}
        )
        self.assertEqual(narrator._pending, "Ich schaue nach.")

        narrator._handle({"type": "session.execution.started", "data": {"sessionID": "ses_2"}})
        self.assertEqual(narrator._pending, "Ich schaue nach.")

        narrator._handle({"type": "session.execution.failed", "data": {"sessionID": "ses_1"}})
        self.assertEqual(narrator._pending, "Das ist fehlgeschlagen.")


class SubagentTests(unittest.TestCase):
    def test_aufruf_lesen(self) -> None:
        info = subagent_call(
            "subagent", {"input": {"agent": "scout", "description": "README"}}
        )
        self.assertEqual(info, {"agent": "scout", "description": "README"})
        self.assertIsNone(subagent_call("read", {"input": {"agent": "scout"}}))
        self.assertIsNone(subagent_call("subagent", {"input": {}}))

    def test_ansage_mit_namen(self) -> None:
        self.assertEqual(phrase_for_subagent("scout"), "Ich gebe das an den Scout.")
        self.assertEqual(phrase_for_subagent("coder"), "Ich gebe das an den Coder.")
        self.assertEqual(
            phrase_for_subagent("voice-reviewer"), "Ich lasse das prüfen."
        )
        self.assertEqual(
            phrase_for_subagent("unbekannt"), "Ich gebe das an einen Helfer."
        )


class NarratorSubagentTests(unittest.TestCase):
    def _narrator(self, updates):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = make_config(tmp.name)
        return Narrator(
            cfg,
            FakeClient(),
            "ses_1",
            lambda text, stop: None,
            on_subagent=updates.append,
        )

    def test_start_ende(self) -> None:
        updates = []
        narrator = self._narrator(updates)

        narrator._handle(
            {
                "type": "session.tool.input.started",
                "data": {"sessionID": "ses_1", "id": "t1", "name": "subagent"},
            }
        )
        narrator._handle(
            {
                "type": "session.tool.called",
                "data": {
                    "sessionID": "ses_1",
                    "id": "t1",
                    "input": {"agent": "scout", "description": "README"},
                },
            }
        )

        self.assertEqual(
            updates,
            [{"agent": "scout", "state": "running", "description": "README"}],
        )
        self.assertEqual(narrator._pending, "Ich gebe das an den Scout.")
        self.assertTrue(narrator._force_next)

        narrator._handle(
            {"type": "session.tool.success", "data": {"sessionID": "ses_1", "id": "t1"}}
        )
        self.assertEqual(updates[-1]["state"], "done")

    def test_fehler(self) -> None:
        updates = []
        narrator = self._narrator(updates)
        narrator._handle(
            {
                "type": "session.tool.input.started",
                "data": {"sessionID": "ses_1", "id": "t2", "name": "subagent"},
            }
        )
        narrator._handle(
            {
                "type": "session.tool.called",
                "data": {
                    "sessionID": "ses_1",
                    "id": "t2",
                    "input": {"agent": "coder"},
                },
            }
        )
        narrator._handle(
            {"type": "session.tool.error", "data": {"sessionID": "ses_1", "id": "t2"}}
        )

        self.assertEqual(updates[-1]["state"], "failed")
        self.assertEqual(narrator._pending, "Das ist fehlgeschlagen.")

    def test_andere_session_ignoriert(self) -> None:
        updates = []
        narrator = self._narrator(updates)
        narrator._handle(
            {
                "type": "session.tool.input.started",
                "data": {"sessionID": "ses_2", "id": "t3", "name": "subagent"},
            }
        )
        narrator._handle(
            {
                "type": "session.tool.called",
                "data": {
                    "sessionID": "ses_2",
                    "id": "t3",
                    "input": {"agent": "scout"},
                },
            }
        )
        self.assertEqual(updates, [])


if __name__ == "__main__":
    unittest.main()
