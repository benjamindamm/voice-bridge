"""Tests für den STT-Client (ohne Netzwerk)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from voicebridge.config import Config
from voicebridge.stt import SttError, silence_wav, transcribe, warmup

BASIS = (
    "VOICE_STT_URL=http://stt.test:8792/v1\n"
    "VOICE_TTS_URL=http://tts.test:8791/v1\n"
    "VOICE_API_KEY=test-key-1234\n"
)


def make_config(tmp: str, extra: str = "") -> Config:
    path = Path(tmp) / ".env"
    path.write_text(BASIS + extra, encoding="utf-8")
    return Config.load(path)


def response(status: int = 200, payload=None, text: str = ""):
    fake = mock.Mock(status_code=status, text=text)
    if payload is None:
        fake.json.side_effect = ValueError("kein JSON")
    else:
        fake.json.return_value = payload
    return fake


class TranscribeTests(unittest.TestCase):
    def test_felder_und_strip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            fake = response(200, {"text": "  hallo welt "})
            with mock.patch("voicebridge.stt.requests.post", return_value=fake) as post:
                text = transcribe(cfg, b"RIFF-test")

        self.assertEqual(text, "hallo welt")
        args, kwargs = post.call_args
        self.assertEqual(args[0], cfg.stt_endpoint)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key-1234")
        self.assertEqual(kwargs["data"]["model"], "whisper-turbo")
        self.assertEqual(kwargs["data"]["language"], "de")
        self.assertNotIn("prompt", kwargs["data"])
        self.assertEqual(kwargs["files"]["file"][0], "audio.wav")
        self.assertEqual(kwargs["files"]["file"][1], b"RIFF-test")
        self.assertEqual(kwargs["timeout"], (10.0, cfg.request_timeout_s))

    def test_prompt_wird_gesendet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp, "VOICE_STT_PROMPT=Autocomplete, Aria\n")
            fake = response(200, {"text": "ok"})
            with mock.patch("voicebridge.stt.requests.post", return_value=fake) as post:
                transcribe(cfg, b"x")

        self.assertEqual(post.call_args.kwargs["data"]["prompt"], "Autocomplete, Aria")

    def test_http_fehler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            fake = response(401, text="unauthorized")
            with mock.patch("voicebridge.stt.requests.post", return_value=fake):
                with self.assertRaises(SttError) as ctx:
                    transcribe(cfg, b"x")
        self.assertIn("401", str(ctx.exception))

    def test_netzwerkfehler(self) -> None:
        import requests

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            with mock.patch(
                "voicebridge.stt.requests.post", side_effect=requests.ConnectionError("weg")
            ):
                with self.assertRaises(SttError) as ctx:
                    transcribe(cfg, b"x")
        self.assertIn("nicht erreichbar", str(ctx.exception))

    def test_kein_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            with mock.patch("voicebridge.stt.requests.post", return_value=response(200)):
                with self.assertRaises(SttError):
                    transcribe(cfg, b"x")


class WarmupTests(unittest.TestCase):
    def test_sendet_eine_sekunde_stille(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            fake = response(200, {"text": ""})
            with mock.patch("voicebridge.stt.requests.post", return_value=fake) as post:
                warmup(cfg)

        wav = post.call_args.kwargs["files"]["file"][1]
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertAlmostEqual((len(wav) - 44) / 2 / cfg.sample_rate, 1.0, places=2)
        self.assertEqual(post.call_args.kwargs["timeout"], (10.0, 180.0))

    def test_silence_wav_laenge(self) -> None:
        wav = silence_wav(16000, seconds=0.5)
        self.assertTrue(wav.startswith(b"RIFF"))
        self.assertEqual(len(wav), 44 + 16000)


if __name__ == "__main__":
    unittest.main()
