"""Tests für die VAD-Zustandslogik (ohne Mikrofon)."""

from __future__ import annotations

import io
import threading
import unittest
import wave

from voicebridge.audio import collect, to_wav

PARAMS = dict(
    frame_ms=20,
    pre_roll_ms=60,
    silence_ms=100,
    min_speech_ms=60,
    max_utterance_s=2.0,
)


def frame(index: int) -> bytes:
    return bytes([index % 256]) * 4


def run(votes, **overrides):
    params = dict(PARAMS)
    params.update(overrides)
    frames = [frame(i) for i in range(len(votes))]
    votes_iter = iter(votes)
    return collect(frames, lambda _frame: next(votes_iter), **params)


class CollectTests(unittest.TestCase):
    def test_stille_liefert_nichts(self) -> None:
        self.assertIsNone(run([False] * 10, idle_timeout_s=0.2))

    def test_sprache_dann_stille(self) -> None:
        votes = [False, False] + [True] * 5 + [False] * 10
        result = run(votes)

        self.assertIsNotNone(result)
        assert result is not None
        # 3 Vorlaufframes + 2 weitere Sprachframes; die 5 Stilleframes fliegen raus
        self.assertEqual(len(result.pcm), 5 * 4)
        self.assertAlmostEqual(result.duration_s, 0.10, places=3)
        self.assertFalse(result.capped)

    def test_kurzer_impuls_startet_nicht(self) -> None:
        votes = [True, True] + [False] * 20
        self.assertIsNone(run(votes))

    def test_leiser_pegel_wird_ignoriert(self) -> None:
        # Der VAD meldet Sprache, aber der Pegel liegt unter der Schwelle.
        quiet = [b"\x00\x00" * 2] * 20
        result = collect(
            quiet,
            lambda _frame: True,
            frame_ms=20,
            pre_roll_ms=60,
            silence_ms=100,
            min_speech_ms=60,
            max_utterance_s=2.0,
            min_frame_rms=500.0,
        )
        self.assertIsNone(result)

    def test_obergrenze_kappt(self) -> None:
        result = run([True] * 50, pre_roll_ms=0, min_speech_ms=20, max_utterance_s=0.2)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertTrue(result.capped)
        self.assertEqual(len(result.pcm), 10 * 4)
        self.assertAlmostEqual(result.duration_s, 0.2, places=3)


    def test_senden_beendet_die_aufnahme(self) -> None:
        send = threading.Event()
        frames = [frame(i) for i in range(20)]

        def stream():
            for index, item in enumerate(frames):
                if index == 8:
                    send.set()
                yield item

        result = collect(
            stream(),
            lambda _frame: True,
            frame_ms=20,
            pre_roll_ms=60,
            silence_ms=1000,
            min_speech_ms=60,
            max_utterance_s=2.0,
            send_event=send,
        )

        self.assertIsNotNone(result)
        assert result is not None
        self.assertFalse(result.capped)
        self.assertAlmostEqual(result.duration_s, 0.16, places=3)


class WavTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        pcm = b"\x00\x01" * 800
        wav = to_wav(pcm, 16000)

        self.assertTrue(wav.startswith(b"RIFF"))
        with wave.open(io.BytesIO(wav), "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 16000)
            self.assertEqual(handle.getnframes(), 800)
            self.assertEqual(handle.readframes(800), pcm)


if __name__ == "__main__":
    unittest.main()
