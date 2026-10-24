"""Tests für den TTS-Client und die Barge-in-Schwelle (ohne Audioausgabe)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from voicebridge import tts
from voicebridge.audio import to_wav
from voicebridge.config import Config

BASIS = (
    "VOICE_STT_URL=http://h:8792/v1\n"
    "VOICE_TTS_URL=http://h:8791/v1\n"
    "VOICE_API_KEY=test-key-1234\n"
)


def make_config(tmp: str, extra: str = "") -> Config:
    path = Path(tmp) / ".env"
    path.write_text(BASIS + extra, encoding="utf-8")
    return Config.load(path)


class SynthesizeTests(unittest.TestCase):
    def test_felder_und_binaerdaten(self) -> None:
        wav = to_wav(b"\x00\x00" * 160, 24000)
        fake = mock.Mock(status_code=200, content=wav, headers={"content-type": "audio/wav"})

        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            with mock.patch("voicebridge.tts.requests.post", return_value=fake) as post:
                result = tts.synthesize(cfg, "Hallo")

        self.assertEqual(result, wav)
        args, kwargs = post.call_args
        self.assertEqual(args[0], cfg.tts_endpoint)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key-1234")
        payload = kwargs["json"]
        self.assertEqual(payload["input"], "Hallo")
        self.assertNotIn("text", payload)
        self.assertEqual(payload["model"], cfg.tts_model)
        self.assertEqual(payload["voice"], cfg.tts_voice)
        self.assertEqual(payload["response_format"], "wav")
        self.assertEqual(payload["speed"], 1.0)

    def test_http_fehler(self) -> None:
        fake = mock.Mock(status_code=401, content=b"", text="unauthorized", headers={})
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            with mock.patch("voicebridge.tts.requests.post", return_value=fake):
                with self.assertRaises(tts.TtsError) as ctx:
                    tts.synthesize(cfg, "Hallo")
        self.assertIn("401", str(ctx.exception))

    def test_kein_wav(self) -> None:
        fake = mock.Mock(
            status_code=200, content=b"ID3keinwav", headers={"content-type": "audio/mpeg"}
        )
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            with mock.patch("voicebridge.tts.requests.post", return_value=fake):
                with self.assertRaises(tts.TtsError) as ctx:
                    tts.synthesize(cfg, "Hallo")
        self.assertIn("kein WAV", str(ctx.exception))


class WavTests(unittest.TestCase):
    def test_wav_info(self) -> None:
        wav = to_wav(b"\x00\x01" * 240, 24000)
        rate, channels, width, frames = tts.wav_info(wav)

        self.assertEqual((rate, channels, width), (24000, 1, 2))
        self.assertEqual(frames, b"\x00\x01" * 240)

    def test_kein_wav_wirft(self) -> None:
        with self.assertRaises(tts.TtsError):
            tts.wav_info(b"kein wav")


class BargeInTests(unittest.TestCase):
    def test_frame_rms(self) -> None:
        frame = np.array([1000, -1000], dtype=np.int16).tobytes()
        self.assertAlmostEqual(tts.frame_rms(frame), 1000.0)
        self.assertEqual(tts.frame_rms(b"\x00\x00"), 0.0)

    def test_schwelle_folgt_dem_echo(self) -> None:
        self.assertEqual(tts.barge_in_threshold(3000.0, 0.5, "strict"), 3000.0)
        self.assertEqual(tts.barge_in_threshold(3000.0, 0.0, "strict"), 300.0)
        self.assertEqual(tts.barge_in_threshold(3000.0, 0.5, "off"), 300.0)
        self.assertEqual(tts.barge_in_threshold(100.0, 9.0, "strict"), 1800.0)

    def test_schwelle_mit_eigenem_grundpegel_und_faktor(self) -> None:
        self.assertEqual(
            tts.barge_in_threshold(0.0, 0.0, "strict", floor=800.0, factor=3.0),
            800.0,
        )
        self.assertEqual(
            tts.barge_in_threshold(1000.0, 0.5, "strict", floor=600.0, factor=3.0),
            1500.0,
        )
        self.assertEqual(
            tts.barge_in_threshold(1000.0, 0.5, "off", floor=800.0, factor=3.0),
            800.0,
        )

    def test_perzentil(self) -> None:
        self.assertAlmostEqual(tts._percentile([0.2, 0.4, 0.6], 90), 0.56)
        self.assertAlmostEqual(tts._percentile([4.0], 50), 4.0)
        self.assertEqual(tts._percentile([], 90), 0.0)

    def test_ausgangspegel(self) -> None:
        samples = np.array([1000, -1000] * 160, dtype=np.int16)
        levels = tts.output_rms_levels(samples, 16000, 20)

        self.assertEqual(len(levels), 1)
        self.assertAlmostEqual(levels[0], 1000.0)


class EchoGainTests(unittest.TestCase):
    def test_median_aus_verhaeltnissen(self) -> None:
        self.assertEqual(tts._median([0.2, 0.4, 0.6]), 0.4)


if __name__ == "__main__":
    unittest.main()
