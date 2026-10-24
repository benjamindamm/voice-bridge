"""Sehr dezente, dumpfe Klangschleife für den Denk-Modus.

Ein kurzer, tiefer Zweiklang (110 Hz + 165 Hz) mit langsamem An- und
Abschwellen. Die Schleife ist nahtlos (ganzzahlige Perioden, Hüllkurve an den
Rändern null) und wird über ``ffplay -loop 0`` ohne Lücken abgespielt.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from typing import Optional

import numpy as np

from .audio import to_wav

SAMPLE_RATE = 24000
DEFAULT_VOLUME = 0.5
LOOP_SECONDS = 6.0

# (Startzeit im 6-s-Muster, Startfrequenz, Zielfrequenz)
BUBBLES = (
    (0.2, 360.0, 700.0),
    (1.3, 300.0, 620.0),
    (2.7, 420.0, 820.0),
    (4.1, 320.0, 650.0),
    (5.2, 380.0, 740.0),
)


def thinking_wav(sample_rate: int = SAMPLE_RATE, duration: float = LOOP_SECONDS) -> bytes:
    """Erzeugt eine nahtlose Schleife aus leisen, kurzen Bläschen (Bopple)."""
    count = max(1, int(sample_rate * duration))
    samples = np.zeros(count, dtype=np.float64)

    bubble_seconds = 0.09
    cycle_seconds = 6.0

    cycle_start = 0.0
    while cycle_start < duration:
        for offset, start_frequency, end_frequency in BUBBLES:
            start = cycle_start + offset
            start_index = int(start * sample_rate)
            if start_index >= count:
                continue
            note_count = int(bubble_seconds * sample_rate)
            t = np.arange(note_count) / sample_rate
            frequency = start_frequency * (
                (end_frequency / start_frequency) ** (t / bubble_seconds)
            )
            phase = 2.0 * math.pi * np.cumsum(frequency) / sample_rate
            envelope = (1.0 - np.exp(-t / 0.003)) * np.exp(-t / 0.022)
            note = np.sin(phase) * envelope * 0.06
            end = min(count, start_index + note_count)
            samples[start_index:end] += note[: end - start_index]
        cycle_start += cycle_seconds

    # Sanfter Tiefpass: nimmt nur die harten Kanten, lässt das Bläschen weich.
    kernel = np.ones(8) / 8.0
    samples = np.convolve(samples, kernel, mode="same")

    return to_wav((samples * 32767.0).astype(np.int16).tobytes(), sample_rate)


class ThinkingLoop:
    """Steuert die Schleife über ``ffplay`` (nahtlos, ohne Lücken)."""

    def __init__(self, volume: float = DEFAULT_VOLUME) -> None:
        self._volume = max(0.0, min(1.0, volume))
        self._process: Optional[subprocess.Popen] = None
        self._path: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self.running:
            return
        if not shutil.which("afplay"):
            return
        if self._path is None:
            handle = tempfile.NamedTemporaryFile(
                prefix="voicebridge-", suffix=".wav", delete=False
            )
            try:
                handle.write(thinking_wav(duration=180.0))
            finally:
                handle.close()
            self._path = handle.name
        try:
            self._process = subprocess.Popen(
                ["afplay", "-v", "{:.2f}".format(self._volume), self._path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            self._process = None

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()
        if self._path is not None:
            try:
                os.unlink(self._path)
            except OSError:
                pass
            self._path = None
