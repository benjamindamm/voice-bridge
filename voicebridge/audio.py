"""Mikrofonaufnahme und Stimmaktivitätserkennung (VAD).

- ``frames`` liefert fortlaufend PCM-Frames (16 Bit, mono) vom Eingabegerät.
- ``collect`` ist die reine Zustandslogik (testbar ohne Mikrofon).
- ``record_utterance`` verbindet beides und liefert eine Äußerung als PCM.
- ``on_level`` meldet pro Frame den Pegel (für die Pegelanzeige im Widget).
"""

from __future__ import annotations

import collections
import io
import threading
import wave
from dataclasses import dataclass
from typing import Callable, Deque, Iterable, Iterator, List, Optional

import numpy as np

from .config import Config


@dataclass
class Collected:
    """Eine abgeschlossene Äußerung."""

    pcm: bytes
    duration_s: float
    speech_s: float
    capped: bool

    def to_wav(self, sample_rate: int) -> bytes:
        return to_wav(self.pcm, sample_rate)


def to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Verpackt PCM (16 Bit, mono) in eine WAV-Datei."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def frame_rms(frame: bytes) -> float:
    """Effektivwert eines PCM-Frames (16 Bit)."""
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))


def collect(
    frames: Iterable[bytes],
    is_speech: Callable[[bytes], bool],
    *,
    frame_ms: int,
    pre_roll_ms: int,
    silence_ms: int,
    min_speech_ms: int,
    max_utterance_s: float,
    idle_timeout_s: Optional[float] = None,
    stop_event: Optional[threading.Event] = None,
    send_event: Optional[threading.Event] = None,
    mute_event: Optional[threading.Event] = None,
    min_frame_rms: float = 0.0,
    on_level: Optional[Callable[[float], None]] = None,
) -> Optional[Collected]:
    """Schneidet aus einem Frame-Strom eine Äußerung heraus.

    - startet erst nach ``min_speech_ms`` zusammenhängender Sprache
    - behält ``pre_roll_ms`` Vorlauf, damit der Wortanfang nicht fehlt
    - endet nach ``silence_ms`` Stille und verwirft die Stille am Ende
    - bricht bei ``max_utterance_s`` ab und markiert ``capped``
    """
    pre_roll_frames = max(1, int(round(pre_roll_ms / frame_ms)))
    min_speech_frames = max(1, int(round(min_speech_ms / frame_ms)))
    silence_limit = max(1, int(round(silence_ms / frame_ms)))
    max_frames = max(1, int(round(max_utterance_s * 1000 / frame_ms)))
    idle_limit = (
        None if idle_timeout_s is None else max(1, int(round(idle_timeout_s * 1000 / frame_ms)))
    )

    ring: Deque[bytes] = collections.deque(maxlen=pre_roll_frames)
    collected: List[bytes] = []
    started = False
    speech_run = 0
    silence_run = 0
    idle_frames = 0
    speech_frames = 0
    capped = False

    for frame in frames:
        if stop_event is not None and stop_event.is_set():
            return None
        if mute_event is not None and mute_event.is_set():
            return None
        if send_event is not None and send_event.is_set():
            if started:
                break
            if ring:
                collected = list(ring)
                started = True
            break
        level = frame_rms(frame) if (on_level is not None or min_frame_rms > 0) else 0.0
        if on_level is not None:
            on_level(level)
        vote = is_speech(frame)
        if min_frame_rms > 0 and level < min_frame_rms:
            vote = False

        if not started:
            ring.append(frame)
            idle_frames += 1
            if vote:
                speech_run += 1
                if speech_run >= min_speech_frames:
                    started = True
                    collected = list(ring)
                    speech_frames = speech_run
                    silence_run = 0
            else:
                speech_run = 0
                if idle_limit is not None and idle_frames >= idle_limit:
                    return None
            continue

        if len(collected) >= max_frames:
            capped = True
            break

        collected.append(frame)
        if vote:
            speech_frames += 1
            silence_run = 0
        else:
            silence_run += 1
            if silence_run >= silence_limit and speech_frames >= min_speech_frames:
                del collected[-silence_run:]
                break

    if not started or not collected:
        return None

    return Collected(
        pcm=b"".join(collected),
        duration_s=len(collected) * frame_ms / 1000.0,
        speech_s=speech_frames * frame_ms / 1000.0,
        capped=capped,
    )


def frames(cfg: Config, on_open: Optional[Callable[[], None]] = None) -> Iterator[bytes]:
    """Liefert fortlaufend PCM-Frames vom Standard-Eingabegerät."""
    import sounddevice as sd

    frame_samples = int(cfg.sample_rate * cfg.frame_ms / 1000)
    expected_bytes = frame_samples * 2

    with sd.RawInputStream(
        samplerate=cfg.sample_rate,
        channels=1,
        dtype="int16",
        blocksize=frame_samples,
    ) as stream:
        if on_open is not None:
            on_open()
        while True:
            data, _overflowed = stream.read(frame_samples)
            chunk = bytes(data)
            if len(chunk) != expected_bytes:
                continue
            yield chunk


def _vad(cfg: Config):
    import webrtcvad

    return webrtcvad.Vad(cfg.vad_aggressiveness)


def record_utterance(
    cfg: Config,
    idle_timeout_s: Optional[float] = None,
    stop_event: Optional[threading.Event] = None,
    send_event: Optional[threading.Event] = None,
    mute_event: Optional[threading.Event] = None,
    min_frame_rms: float = 0.0,
    on_level: Optional[Callable[[float], None]] = None,
) -> Optional[Collected]:
    """Nimmt eine Äußerung auf; ``None``, wenn keine Sprache kam oder gestoppt wurde."""
    vad = _vad(cfg)
    return collect(
        frames(cfg),
        lambda frame: vad.is_speech(frame, cfg.sample_rate),
        frame_ms=cfg.frame_ms,
        pre_roll_ms=cfg.pre_roll_ms,
        silence_ms=cfg.silence_ms,
        min_speech_ms=cfg.min_speech_ms,
        max_utterance_s=cfg.max_utterance_s,
        idle_timeout_s=idle_timeout_s,
        stop_event=stop_event,
        send_event=send_event,
        mute_event=mute_event,
        min_frame_rms=min_frame_rms,
        on_level=on_level,
    )


def speech_frames(cfg: Config) -> Iterator[bool]:
    """Liefert pro Audiorahmen, ob Sprache erkannt wurde (für Barge-in, Schritt 5)."""
    vad = _vad(cfg)
    for frame in frames(cfg):
        yield vad.is_speech(frame, cfg.sample_rate)
