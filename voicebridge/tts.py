"""Text-to-Speech-Client (speaches/Piper) und Wiedergabe mit Barge-in.

Vertrag (verifiziert 2026-10-24):
- ``POST {VOICE_TTS_URL}/audio/speech`` mit JSON
  ``{"model", "voice", "input", "response_format": "wav", "speed"}``
- Antwort: rohe WAV-Bytes (PCM 16 Bit mono 24 kHz)

Barge-in: Während der Wiedergabe liest ein Thread das Mikrofon. In der
``strict``-Sperre wird die erwartete Echosignalstärke aus dem *abgespielten*
Signal geschätzt (Pegelverhältnis während der Kalibrierung); unterbrochen wird
erst, wenn die Sprache deutlich lauter ist als das Echo.
"""

from __future__ import annotations

import collections
import io
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from typing import List, Optional, Sequence, Tuple

import numpy as np
import requests
import sounddevice as sd

from . import audio
from .config import Config

CALIBRATION_MS = 800
BARGE_MS = 250
ECHO_WINDOW_MS = 200
ECHO_FACTOR = 2.0
LEVEL_FLOOR = 300.0
AMBIENT_MARGIN = 1.6
DEFAULT_ECHO_GAIN = 0.35


class TtsError(RuntimeError):
    """TTS nicht erreichbar, abgelehnt oder unbrauchbar."""


def wav_info(data: bytes) -> Tuple[int, int, int, bytes]:
    """Liefert (Samplerate, Kanäle, Bytes pro Sample, PCM-Frames)."""
    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            return (
                handle.getframerate(),
                handle.getnchannels(),
                handle.getsampwidth(),
                handle.readframes(handle.getnframes()),
            )
    except (wave.Error, EOFError) as exc:
        raise TtsError("TTS-Audio ist kein gültiges WAV: {}".format(exc)) from exc


def synthesize(cfg: Config, text: str) -> bytes:
    """Sendet Text an ``TTS_URL`` und liefert WAV-Bytes."""
    payload = {
        "model": cfg.tts_model,
        "voice": cfg.tts_voice,
        "input": text,
        "response_format": "wav",
        "speed": cfg.tts_speed,
    }
    try:
        response = requests.post(
            cfg.tts_endpoint,
            headers={"Authorization": "Bearer " + cfg.api_key},
            json=payload,
            timeout=(10.0, cfg.request_timeout_s),
        )
    except requests.RequestException as exc:
        raise TtsError("TTS nicht erreichbar ({}): {}".format(cfg.tts_endpoint, exc)) from exc

    if response.status_code != 200:
        detail = (response.text or "").strip()[:300]
        raise TtsError("TTS antwortete HTTP {}: {}".format(response.status_code, detail))
    if not response.content.startswith(b"RIFF"):
        raise TtsError(
            "TTS lieferte kein WAV (Content-Type {}).".format(
                response.headers.get("content-type", "?")
            )
        )
    return response.content


def frame_rms(frame: bytes) -> float:
    """Effektivwert eines PCM-Frames (16 Bit)."""
    samples = np.frombuffer(frame, dtype=np.int16).astype(np.float64)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))


def output_rms_levels(samples: np.ndarray, rate: int, frame_ms: int) -> List[float]:
    """RMS-Pegel des abgespielten Signals pro Frame (für die Echo-Schätzung)."""
    block = max(1, int(rate * frame_ms / 1000))
    data = samples.astype(np.float64)
    if data.ndim > 1:
        data = data.mean(axis=1)

    levels: List[float] = []
    for start in range(0, len(data), block):
        chunk = data[start:start + block]
        if chunk.size:
            levels.append(float(np.sqrt(np.mean(chunk * chunk))))
    return levels


def barge_in_threshold(
    expected_echo: float,
    gain: float,
    guard: str,
    floor: float = LEVEL_FLOOR,
    factor: float = ECHO_FACTOR,
) -> float:
    """Pegelschwelle für eine Unterbrechung.

    ``strict``: erwartetes Echo (Ausgangspegel × gemessenes Pegelverhältnis)
    mal Sicherheitsfaktor. ``off`` (Kopfhörer): nur die Grundschwelle.
    """
    if guard == "off":
        return floor
    return max(floor, expected_echo * gain * factor)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _percentile(values: Sequence[float], percent: float) -> float:
    """Lineares Perzentil (0..100) einer Messreihe."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(100.0, percent)) / 100.0 * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def play_wav(
    cfg: Config,
    data: bytes,
    echo_guard: Optional[str] = None,
    stop_event: Optional[threading.Event] = None,
    player: Optional[str] = None,
    monitor: bool = True,
    mute_event: Optional[threading.Event] = None,
) -> bool:
    """Spielt WAV ab; ``False``, wenn durch Spracheingabe oder Stopp beendet."""
    rate, channels, width, frames = wav_info(data)
    if width != 2:
        raise TtsError("Nur 16-Bit-WAV wird unterstützt ({} Bit).".format(width * 8))

    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels)

    if stop_event is not None and stop_event.is_set():
        return False

    chosen = (player or cfg.player).lower()
    if chosen == "afplay" and shutil.which("afplay"):
        return _play_afplay(
            cfg, data, samples, rate, echo_guard, stop_event, monitor, mute_event
        )
    return _play_portaudio(
        cfg, samples, rate, echo_guard, stop_event, monitor, mute_event
    )


def _play_portaudio(
    cfg: Config,
    samples: np.ndarray,
    rate: int,
    echo_guard: Optional[str],
    stop_event: Optional[threading.Event],
    monitor: bool = True,
    mute_event: Optional[threading.Event] = None,
) -> bool:
    guard = echo_guard or cfg.echo_guard
    output_levels = output_rms_levels(samples, rate, cfg.frame_ms)
    stop = threading.Event()
    interrupted = threading.Event()
    ready = threading.Event()
    started = threading.Event()
    armed = threading.Event()

    monitor_thread: Optional[threading.Thread] = None
    if monitor:
        monitor_thread = threading.Thread(
            target=_monitor,
            args=(
                cfg,
                guard,
                stop,
                interrupted,
                output_levels,
                ready,
                started,
                armed,
                mute_event,
            ),
            daemon=True,
        )
        # Mikrofon zuerst öffnen. Wird der Eingabestrom erst während der Wiedergabe
        # geöffnet, konfiguriert macOS das Gerät mitten im Ton um -> Knistern.
        monitor_thread.start()
        ready.wait(timeout=1.0)
        # Erst losspielen, wenn der Umgebungspegel erfasst ist.
        armed.wait(timeout=max(0.0, cfg.ambient_ms / 1000.0) + 0.6)

    sd.play(samples, samplerate=rate, latency="high")
    started.set()
    try:
        while True:
            if interrupted.is_set() or (stop_event is not None and stop_event.is_set()):
                sd.stop()
                break
            if not sd.get_stream().active:
                break
            time.sleep(0.03)
    finally:
        stop.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=1.0)
        if sd.get_stream().active:
            sd.stop()

    return not interrupted.is_set() and not (stop_event is not None and stop_event.is_set())


def _play_afplay(
    cfg: Config,
    data: bytes,
    samples: np.ndarray,
    rate: int,
    echo_guard: Optional[str],
    stop_event: Optional[threading.Event],
    monitor: bool = True,
    mute_event: Optional[threading.Event] = None,
) -> bool:
    """Wiedergabe über den System-Player (robuste Geräte- und Ratenbehandlung)."""
    guard = echo_guard or cfg.echo_guard
    output_levels = output_rms_levels(samples, rate, cfg.frame_ms)
    stop = threading.Event()
    interrupted = threading.Event()
    ready = threading.Event()
    started = threading.Event()
    armed = threading.Event()

    monitor_thread: Optional[threading.Thread] = None
    if monitor:
        monitor_thread = threading.Thread(
            target=_monitor,
            args=(
                cfg,
                guard,
                stop,
                interrupted,
                output_levels,
                ready,
                started,
                armed,
                mute_event,
            ),
            daemon=True,
        )
        monitor_thread.start()
        ready.wait(timeout=1.0)
        # Erst losspielen, wenn der Umgebungspegel erfasst ist.
        armed.wait(timeout=max(0.0, cfg.ambient_ms / 1000.0) + 0.6)

    handle = tempfile.NamedTemporaryFile(prefix="voicebridge-", suffix=".wav", delete=False)
    path = handle.name
    process: Optional[subprocess.Popen] = None
    try:
        handle.write(data)
        handle.close()
        process = subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started.set()
        while process.poll() is None:
            if interrupted.is_set() or (stop_event is not None and stop_event.is_set()):
                process.terminate()
                break
            time.sleep(0.03)
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
    finally:
        stop.set()
        if monitor_thread is not None:
            monitor_thread.join(timeout=1.0)
        if process is not None and process.poll() is None:
            process.terminate()
        try:
            os.unlink(path)
        except OSError:
            pass

    return not interrupted.is_set() and not (stop_event is not None and stop_event.is_set())


def _monitor(
    cfg: Config,
    guard: str,
    stop: threading.Event,
    interrupted: threading.Event,
    output_levels: Sequence[float],
    ready: threading.Event,
    playback_started: threading.Event,
    armed: threading.Event,
    mute_event: Optional[threading.Event] = None,
) -> None:
    """Liest parallel das Mikrofon und erkennt echte Unterbrechungen.

    Vor der Wiedergabe wird kurz der Umgebungspegel erfasst. Er bildet zusammen
    mit ``barge_min_rms`` die Grundschwelle, damit Rauschen die Ausgabe nicht
    unterbricht. Unterbrochen wird erst nach ``barge_ms`` durchgehender Sprache
    über der Echo-Schwelle (konservatives p90-Gain).
    """
    import webrtcvad

    def _arm() -> None:
        if not armed.is_set():
            armed.set()

    # Strengerer VAD nur für das Unterbrechen (Aufnahme bleibt bei cfg.vad_aggressiveness).
    try:
        vad = webrtcvad.Vad(cfg.barge_vad)
    except Exception:  # noqa: BLE001 – ohne VAD kein Barge-in
        ready.set()
        _arm()
        return

    started_at: Optional[float] = None

    frame_ms = cfg.frame_ms
    calibration_frames = 0 if guard == "off" else max(1, int(round(CALIBRATION_MS / frame_ms)))
    barge_frames = max(1, int(round(cfg.barge_ms / frame_ms)))
    window_length = max(1, int(round(ECHO_WINDOW_MS / frame_ms)))
    ambient_frames = max(0, int(round(cfg.ambient_ms / frame_ms)))

    envelope = collections.deque(maxlen=window_length)
    ambient_levels: List[float] = []
    ratios: List[float] = []
    echo_gain = 0.0
    speech_run = 0
    index = 0
    noise_floor = float(cfg.barge_min_rms)
    peak_level = 0.0
    max_threshold = 0.0

    stream_frames = audio.frames(cfg, on_open=ready.set)
    try:
        for frame in stream_frames:
            if stop.is_set() or (mute_event is not None and mute_event.is_set()):
                _arm()
                return

            if not playback_started.is_set():
                # Umgebungspegel sammeln, solange noch nichts abgespielt wird.
                ambient_levels.append(frame_rms(frame))
                if len(ambient_levels) >= ambient_frames:
                    noise_floor = max(
                        float(cfg.barge_min_rms),
                        _percentile(ambient_levels, 75) * AMBIENT_MARGIN,
                    )
                    _arm()
                continue
            if not armed.is_set():
                _arm()

            if started_at is None:
                started_at = time.monotonic()
            index += 1

            position = int((time.monotonic() - started_at) * 1000 / frame_ms)
            played = output_levels[position] if 0 <= position < len(output_levels) else 0.0
            envelope.append(played)
            expected = max(envelope)

            if calibration_frames and index <= calibration_frames:
                if expected > 150:
                    ratios.append(frame_rms(frame) / expected)
                continue
            if calibration_frames and index == calibration_frames + 1:
                # Median als typisches Echo-Verhältnis; p90 hat die Schwelle zu
                # weit über normale Sprache gehoben.
                echo_gain = _median(ratios) if ratios else DEFAULT_ECHO_GAIN

            level = frame_rms(frame)
            peak_level = max(peak_level, level)
            if expected < 150 and level < noise_floor * 1.5:
                # Grundpegel in Sprechpausen langsam nachführen.
                noise_floor = max(
                    float(cfg.barge_min_rms), 0.98 * noise_floor + 0.02 * level
                )

            threshold = barge_in_threshold(
                expected,
                echo_gain,
                guard,
                floor=noise_floor,
                factor=cfg.barge_factor,
            )
            max_threshold = max(max_threshold, threshold)
            try:
                speech = vad.is_speech(frame, cfg.sample_rate)
            except Exception:  # noqa: BLE001
                speech = False

            if speech and level > threshold:
                speech_run += 1
                if speech_run >= barge_frames:
                    interrupted.set()
                    return
            else:
                speech_run = 0
    except Exception:  # noqa: BLE001 – z. B. kein Mikrofonzugriff
        return
    finally:
        close = getattr(stream_frames, "close", None)
        if close is not None:
            close()
        if index:
            print(
                "Barge-in: Boden {:.0f} · Gain {:.2f} · Spitze {:.0f} · "
                "Schwelle max {:.0f} · {} ms".format(
                    noise_floor,
                    echo_gain,
                    peak_level,
                    max_threshold,
                    index * frame_ms,
                ),
                flush=True,
            )
