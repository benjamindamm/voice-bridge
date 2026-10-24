"""Tests für das Laden und Prüfen der Konfiguration."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voicebridge.config import Config, ConfigError, endpoint, parse_dotenv

BASIS = (
    "VOICE_STT_URL=http://stt.test:8792/v1\n"
    "VOICE_TTS_URL=http://tts.test:8791/v1\n"
    "VOICE_API_KEY=test-key-1234\n"
)


class ParseDotenvTests(unittest.TestCase):
    def test_kommentare_export_quotes_inline(self) -> None:
        text = """
        # Kommentar
        VOICE_STT_URL=http://s:8792/v1   # inline
        export VOICE_TTS_URL="http://s:8791/v1"
        VOICE_API_KEY='abc # kein Kommentar'
        leer=
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(text, encoding="utf-8")
            values = parse_dotenv(path)

        self.assertEqual(values["VOICE_STT_URL"], "http://s:8792/v1")
        self.assertEqual(values["VOICE_TTS_URL"], "http://s:8791/v1")
        self.assertEqual(values["VOICE_API_KEY"], "abc # kein Kommentar")
        self.assertEqual(values["leer"], "")


class EndpointTests(unittest.TestCase):
    def test_basis_url_wird_ergaenzt(self) -> None:
        self.assertEqual(
            endpoint("http://h:8792/v1", "audio/transcriptions"),
            "http://h:8792/v1/audio/transcriptions",
        )

    def test_vollstaendige_url_bleibt(self) -> None:
        self.assertEqual(
            endpoint("http://h:8792/v1/audio/transcriptions", "audio/transcriptions"),
            "http://h:8792/v1/audio/transcriptions",
        )


class LoadTests(unittest.TestCase):
    def _write(self, tmp: str, text: str) -> Path:
        path = Path(tmp) / ".env"
        path.write_text(text, encoding="utf-8")
        return path

    def test_fehlende_datei_meldet_pfad(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "fehlt.env"
            with self.assertRaises(ConfigError) as ctx:
                Config.load(missing)
        self.assertIn("kopiere .env.example", str(ctx.exception))

    def test_platzhalter_werden_abgelehnt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "VOICE_STT_URL=http://stt.test:8792/v1\n"
                "VOICE_TTS_URL=http://tts.test:8791/v1\n"
                "VOICE_API_KEY=CHANGE_ME\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                Config.load(path)
        self.assertIn("VOICE_API_KEY", str(ctx.exception))
        self.assertIn("CHANGE_ME", str(ctx.exception))

    def test_fehlender_api_key_wird_gemeldet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "VOICE_STT_URL=http://stt.test:8792/v1\n"
                "VOICE_TTS_URL=http://tts.test:8791/v1\n",
            )
            with self.assertRaises(ConfigError) as ctx:
                Config.load(path)
        self.assertIn("VOICE_API_KEY fehlt", str(ctx.exception))

    def test_gueltige_konfiguration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp, BASIS + "VOICE_SESSION_DIR={}\n".format(tmp)
            )
            cfg = Config.load(path)

        self.assertEqual(cfg.sample_rate, 16000)
        self.assertEqual(cfg.frame_ms, 20)
        self.assertEqual(cfg.vad_aggressiveness, 2)
        self.assertEqual(cfg.silence_ms, 700)
        self.assertEqual(cfg.max_utterance_s, 25.0)
        self.assertEqual(cfg.agent, "voice-orchestrator")
        self.assertEqual(cfg.model, "")
        self.assertEqual(cfg.model_providers, ())
        self.assertEqual(cfg.stt_model, "whisper-turbo")
        self.assertEqual(cfg.stt_language, "de")
        self.assertEqual(cfg.tts_model, "speaches-ai/piper-de_DE-thorsten-medium")
        self.assertEqual(cfg.tts_voice, "de_DE-thorsten-medium")
        self.assertAlmostEqual(cfg.signal_volume, 0.5)
        self.assertAlmostEqual(cfg.barge_factor, 1.8)
        self.assertEqual(cfg.barge_ms, 400)
        self.assertAlmostEqual(cfg.barge_min_rms, 600.0)
        self.assertEqual(cfg.barge_vad, 2)
        self.assertEqual(cfg.ambient_ms, 250)
        self.assertAlmostEqual(cfg.record_min_rms, 0.0)
        self.assertIn("1234", cfg.masked_api_key)
        self.assertTrue(cfg.session_dir.is_absolute())
        self.assertEqual(
            cfg.stt_endpoint, "http://stt.test:8792/v1/audio/transcriptions"
        )
        self.assertEqual(cfg.tts_endpoint, "http://tts.test:8791/v1/audio/speech")

    def test_standard_session_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, BASIS)
            cfg = Config.load(path)

        self.assertEqual(cfg.session_dir, Path("~/voice-workspace").expanduser().resolve())

    def test_player_standard_und_pruefung(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, BASIS)
            cfg = Config.load(path)
        self.assertEqual(cfg.player, "afplay")

        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, BASIS + "VOICE_PLAYER=kaputt\n")
            with self.assertRaises(ConfigError) as ctx:
                Config.load(path)
        self.assertIn("VOICE_PLAYER", str(ctx.exception))

    def test_umgebung_hat_vorrang(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, BASIS)
            with mock.patch.dict(os.environ, {"VOICE_TTS_URL": "http://shell:8791/v1"}):
                cfg = Config.load(path)

        self.assertEqual(cfg.tts_url, "http://shell:8791/v1")
        self.assertEqual(cfg.stt_url, "http://stt.test:8792/v1")

    def test_allgemeine_shell_variablen_werden_ignoriert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, BASIS + "VOICE_AGENT=voice-orchestrator\n")
            # AGENT=1 existiert z. B. bei manchen Shell-Setups; darf nicht greifen.
            with mock.patch.dict(os.environ, {"AGENT": "1", "TOKEN": "fremd"}):
                cfg = Config.load(path)

        self.assertEqual(cfg.api_key, "test-key-1234")
        self.assertEqual(cfg.agent, "voice-orchestrator")

    def test_modell_provider_liste(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                BASIS + "VOICE_MODEL_PROVIDERS=treno, ollama-cloud ,\n",
            )
            cfg = Config.load(path)

        self.assertEqual(cfg.model_providers, ("treno", "ollama-cloud"))

    def test_ungueltige_zahl_wird_gemeldet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, BASIS + "VOICE_SILENCE_MS=abc\n")
            with self.assertRaises(ConfigError) as ctx:
                Config.load(path)
        self.assertIn("VOICE_SILENCE_MS", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
