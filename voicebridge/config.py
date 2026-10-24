"""Konfiguration der Voice-Bridge.

Alle Schlüssel tragen das Präfix ``VOICE_``, damit sie nicht mit allgemeinen
Shell-Variablen (z. B. ``AGENT``) kollidieren. Umgebungsvariablen haben Vorrang
vor der .env-Datei.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PLACEHOLDER = "CHANGE_ME"

KEY_STT_URL = "VOICE_STT_URL"
KEY_TTS_URL = "VOICE_TTS_URL"
KEY_OPENCODE_URL = "VOICE_OPENCODE_URL"
KEY_OPENCODE_PASSWORD = "VOICE_OPENCODE_PASSWORD"
KEY_OPENCODE_POLL_MS = "VOICE_OPENCODE_POLL_MS"
KEY_API_KEY = "VOICE_API_KEY"
KEY_STT_MODEL = "VOICE_STT_MODEL"
KEY_STT_LANGUAGE = "VOICE_STT_LANGUAGE"
KEY_STT_PROMPT = "VOICE_STT_PROMPT"
KEY_TTS_MODEL = "VOICE_TTS_MODEL"
KEY_TTS_VOICE = "VOICE_TTS_VOICE"
KEY_TTS_SPEED = "VOICE_TTS_SPEED"
KEY_TTS_MAX_CHARS = "VOICE_TTS_MAX_CHARS"
KEY_ECHO_GUARD = "VOICE_ECHO_GUARD"
KEY_PLAYER = "VOICE_PLAYER"
KEY_SPEAK = "VOICE_SPEAK"
KEY_SIGNAL_VOLUME = "VOICE_SIGNAL_VOLUME"
KEY_PROGRESS = "VOICE_PROGRESS"
KEY_PROGRESS_INTERVAL_S = "VOICE_PROGRESS_INTERVAL_S"
KEY_SESSION_DIR = "VOICE_SESSION_DIR"
KEY_AGENT = "VOICE_AGENT"
KEY_MODEL = "VOICE_MODEL"
KEY_SAMPLE_RATE = "VOICE_SAMPLE_RATE"
KEY_FRAME_MS = "VOICE_FRAME_MS"
KEY_VAD_AGGRESSIVENESS = "VOICE_VAD_AGGRESSIVENESS"
KEY_PRE_ROLL_MS = "VOICE_PRE_ROLL_MS"
KEY_SILENCE_MS = "VOICE_SILENCE_MS"
KEY_MIN_SPEECH_MS = "VOICE_MIN_SPEECH_MS"
KEY_MAX_UTTERANCE_S = "VOICE_MAX_UTTERANCE_S"
KEY_REQUEST_TIMEOUT_S = "VOICE_REQUEST_TIMEOUT_S"
KEY_BARGE_FACTOR = "VOICE_BARGE_FACTOR"
KEY_BARGE_MS = "VOICE_BARGE_MS"
KEY_BARGE_MIN_RMS = "VOICE_BARGE_MIN_RMS"
KEY_BARGE_VAD = "VOICE_BARGE_VAD"
KEY_AMBIENT_MS = "VOICE_AMBIENT_MS"
KEY_RECORD_MIN_RMS = "VOICE_RECORD_MIN_RMS"

DEFAULT_SESSION_DIR = "~/voice-workspace"

REQUIRED_KEYS = (KEY_STT_URL, KEY_TTS_URL, KEY_API_KEY)

STT_PATH = "audio/transcriptions"
TTS_PATH = "audio/speech"

ECHO_GUARDS = ("strict", "off")
PLAYERS = ("afplay", "portaudio")


class ConfigError(RuntimeError):
    """Konfiguration fehlt, ist unvollständig oder enthält Platzhalter."""


def parse_dotenv(path: Path) -> Dict[str, str]:
    """Liest KEY=VALUE-Zeilen; unterstützt Kommentare, ``export`` und Quotes."""
    values: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            for marker in (" #", "\t#"):
                cut = value.find(marker)
                if cut != -1:
                    value = value[:cut].rstrip()
                    break
        values[key] = value
    return values


def default_env_path() -> Path:
    """Pfad zur .env; ``VOICE_BRIDGE_ENV`` hat Vorrang."""
    override = os.environ.get("VOICE_BRIDGE_ENV")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / ".env"


def endpoint(base: str, path: str) -> str:
    """Hängt ``path`` an eine Basis-URL; eine bereits vollständige URL bleibt unverändert."""
    base = base.rstrip("/")
    if base.endswith("/" + path):
        return base
    return base + "/" + path


@dataclass(frozen=True)
class Config:
    stt_url: str
    tts_url: str
    opencode_url: str
    opencode_password: str
    opencode_poll_ms: int
    api_key: str
    stt_model: str
    stt_language: str
    stt_prompt: str
    tts_model: str
    tts_voice: str
    tts_speed: float
    tts_max_chars: int
    echo_guard: str
    player: str
    speak: bool
    signal_volume: float
    progress: bool
    progress_interval_s: float
    session_dir: Path
    agent: str
    model: str
    env_path: Path
    sample_rate: int
    frame_ms: int
    vad_aggressiveness: int
    pre_roll_ms: int
    silence_ms: int
    min_speech_ms: int
    max_utterance_s: float
    request_timeout_s: float
    barge_factor: float
    barge_ms: int
    barge_min_rms: float
    barge_vad: int
    ambient_ms: int
    record_min_rms: float

    @property
    def stt_endpoint(self) -> str:
        return endpoint(self.stt_url, STT_PATH)

    @property
    def tts_endpoint(self) -> str:
        return endpoint(self.tts_url, TTS_PATH)

    @property
    def masked_api_key(self) -> str:
        if not self.api_key:
            return "(nicht gesetzt)"
        if len(self.api_key) <= 4:
            return "gesetzt"
        return "gesetzt (…{})".format(self.api_key[-4:])

    @classmethod
    def load(cls, env_path: Optional[Path] = None) -> "Config":
        path = default_env_path() if env_path is None else Path(env_path).expanduser().resolve()
        file_values: Dict[str, str] = parse_dotenv(path) if path.is_file() else {}

        def raw(key: str, default: Optional[str] = None) -> Optional[str]:
            value = os.environ.get(key)
            if value:
                return value
            value = file_values.get(key)
            if value:
                return value
            return default

        problems: List[str] = []
        if not path.is_file():
            problems.append(
                "{} fehlt – kopiere .env.example nach .env und fülle die Werte aus.".format(path)
            )

        def text(key: str, default: Optional[str] = None) -> Optional[str]:
            value = raw(key, default)
            if value is None or not value.strip():
                problems.append("{} fehlt (Pflichtwert).".format(key))
                return None
            if PLACEHOLDER in value:
                problems.append("{} enthält noch den Platzhalter {}.".format(key, PLACEHOLDER))
                return None
            return value.strip()

        def optional(key: str, default: str = "") -> str:
            value = raw(key)
            return default if value is None else value.strip()

        def number(
            key: str,
            default: Any,
            cast: Callable[[str], Any],
            minimum: Optional[float] = None,
            maximum: Optional[float] = None,
        ) -> Any:
            value = raw(key)
            if value is None:
                return default
            try:
                parsed = cast(value)
            except ValueError:
                problems.append("{}={!r} ist keine gültige Zahl.".format(key, value))
                return default
            if minimum is not None and parsed < minimum:
                problems.append("{}={} ist kleiner als {}.".format(key, parsed, minimum))
                return default
            if maximum is not None and parsed > maximum:
                problems.append("{}={} ist größer als {}.".format(key, parsed, maximum))
                return default
            return parsed

        def flag(key: str, default: bool) -> bool:
            value = raw(key)
            if value is None:
                return default
            normalized = value.strip().lower()
            if normalized in ("1", "true", "yes", "on", "an", "ja"):
                return True
            if normalized in ("0", "false", "no", "off", "aus", "nein"):
                return False
            problems.append("{}={!r} ist kein gültiger Schalter (on/off).".format(key, value))
            return default

        def choice(key: str, default: str, allowed: tuple) -> str:
            value = optional(key, default).lower()
            if value not in allowed:
                problems.append(
                    "{} muss {} sein (gefunden: {!r}).".format(
                        key, " oder ".join(repr(item) for item in allowed), value
                    )
                )
                return default
            return value

        echo_guard = choice(KEY_ECHO_GUARD, "strict", ECHO_GUARDS)
        player = choice(KEY_PLAYER, "afplay", PLAYERS)

        session_dir = Path(raw(KEY_SESSION_DIR) or DEFAULT_SESSION_DIR).expanduser().resolve()
        agent = optional(KEY_AGENT, "voice-orchestrator") or "voice-orchestrator"

        cfg = cls(
            stt_url=text(KEY_STT_URL) or "",
            tts_url=text(KEY_TTS_URL) or "",
            opencode_url=optional(KEY_OPENCODE_URL, ""),
            opencode_password=optional(KEY_OPENCODE_PASSWORD, ""),
            opencode_poll_ms=number(KEY_OPENCODE_POLL_MS, 250, int, 50, 2000),
            api_key=text(KEY_API_KEY) or "",
            stt_model=optional(KEY_STT_MODEL, "whisper-turbo"),
            stt_language=optional(KEY_STT_LANGUAGE, "de"),
            stt_prompt=optional(KEY_STT_PROMPT, ""),
            tts_model=optional(KEY_TTS_MODEL, "speaches-ai/piper-de_DE-thorsten-medium"),
            tts_voice=optional(KEY_TTS_VOICE, "de_DE-thorsten-medium"),
            tts_speed=number(KEY_TTS_SPEED, 1.0, float, 0.5, 2.0),
            tts_max_chars=number(KEY_TTS_MAX_CHARS, 350, int, 120, 1200),
            echo_guard=echo_guard,
            player=player,
            speak=flag(KEY_SPEAK, True),
            signal_volume=number(KEY_SIGNAL_VOLUME, 0.5, float, 0.0, 1.0),
            progress=flag(KEY_PROGRESS, True),
            progress_interval_s=number(KEY_PROGRESS_INTERVAL_S, 2.5, float, 1.0, 10.0),
            session_dir=session_dir,
            agent=agent,
            model=optional(KEY_MODEL, ""),
            env_path=path,
            sample_rate=number(KEY_SAMPLE_RATE, 16000, int, 8000, 48000),
            frame_ms=number(KEY_FRAME_MS, 20, int, 10, 30),
            vad_aggressiveness=number(KEY_VAD_AGGRESSIVENESS, 2, int, 0, 3),
            pre_roll_ms=number(KEY_PRE_ROLL_MS, 300, int, 0, 2000),
            silence_ms=number(KEY_SILENCE_MS, 700, int, 100, 5000),
            min_speech_ms=number(KEY_MIN_SPEECH_MS, 250, int, 0, 2000),
            max_utterance_s=number(KEY_MAX_UTTERANCE_S, 25.0, float, 1.0, 600.0),
            request_timeout_s=number(KEY_REQUEST_TIMEOUT_S, 120.0, float, 1.0, 3600.0),
            barge_factor=number(KEY_BARGE_FACTOR, 1.8, float, 1.2, 8.0),
            barge_ms=number(KEY_BARGE_MS, 400, int, 150, 1200),
            barge_min_rms=number(KEY_BARGE_MIN_RMS, 600.0, float, 100.0, 5000.0),
            barge_vad=number(KEY_BARGE_VAD, 2, int, 0, 3),
            ambient_ms=number(KEY_AMBIENT_MS, 250, int, 0, 1000),
            record_min_rms=number(KEY_RECORD_MIN_RMS, 0.0, float, 0.0, 5000.0),
        )
        if problems:
            raise ConfigError("\n".join("- " + problem for problem in problems))
        return cfg
