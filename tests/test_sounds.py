"""Tests für die dumpfe Denk-Schleife."""

from __future__ import annotations

import io
import unittest
import wave

import numpy as np

from voicebridge import sounds


class ThinkingLoopTests(unittest.TestCase):
    def test_wav_ist_nahtlos_und_leise(self) -> None:
        wav = sounds.thinking_wav()

        self.assertTrue(wav.startswith(b"RIFF"))
        with wave.open(io.BytesIO(wav), "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 24000)
            frames = handle.readframes(handle.getnframes())

        samples = np.frombuffer(frames, dtype=np.int16)
        self.assertGreater(len(samples), 24000)
        self.assertLessEqual(int(np.abs(samples).max()), 6000)
        self.assertGreater(int(np.abs(samples).max()), 500)

        # Nahtlos: Anfang und Ende liegen praktisch bei null
        self.assertLessEqual(abs(int(samples[0])), 50)
        self.assertLessEqual(abs(int(samples[-1])), 50)


if __name__ == "__main__":
    unittest.main()
