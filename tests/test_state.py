"""Tests für die Sitzungs-Persistenz."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicebridge import state


class StateTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unterordner" / "session.json"
            original = state.SessionState("ses_1", "/tmp/projekt", "voice-orchestrator", 123.5)
            state.save(original, path)
            loaded = state.load(path)

        self.assertEqual(loaded, original)

    def test_kaputte_datei(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            path.write_text("{kaputt", encoding="utf-8")
            self.assertIsNone(state.load(path))

    def test_fehlende_felder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            path.write_text('{"updated_at": 1}', encoding="utf-8")
            self.assertIsNone(state.load(path))

    def test_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.json"
            state.save(state.SessionState("ses_1", "/tmp", "voice-orchestrator", 1.0), path)
            state.clear(path)
            self.assertFalse(path.exists())
            state.clear(path)  # zweites Löschen ist harmlos

    def test_settings_standard_und_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.json"
            self.assertTrue(state.load_settings(path).resume)

            state.save_settings(state.Settings(resume=False, model="treno/local:default"), path)
            loaded = state.load_settings(path)
            self.assertFalse(loaded.resume)
            self.assertEqual(loaded.model, "treno/local:default")

            path.write_text("{kaputt", encoding="utf-8")
            fallback = state.load_settings(path)
            self.assertTrue(fallback.resume)
            self.assertEqual(fallback.model, "")


if __name__ == "__main__":
    unittest.main()
