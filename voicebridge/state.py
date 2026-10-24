"""Persistenz der Sprachsitzung.

Speichert Session-ID, Arbeitsordner und Agent, damit ein Neustart der Bridge
das Gespräch fortsetzt, statt eine neue Session anzulegen.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SessionState:
    session_id: str
    directory: str
    agent: str
    updated_at: float


def default_path() -> Path:
    override = os.environ.get("VOICE_BRIDGE_STATE")
    if override:
        return Path(override).expanduser()
    return Path("~/.local/state/voice-bridge/session.json").expanduser()


def load(path: Optional[Path] = None) -> Optional[SessionState]:
    target = path or default_path()
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    session_id = payload.get("session_id")
    directory = payload.get("directory")
    if not session_id or not directory:
        return None
    return SessionState(
        session_id=str(session_id),
        directory=str(directory),
        agent=str(payload.get("agent") or ""),
        updated_at=float(payload.get("updated_at") or 0.0),
    )


def save(state: SessionState, path: Optional[Path] = None) -> None:
    target = path or default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def clear(path: Optional[Path] = None) -> None:
    target = path or default_path()
    try:
        target.unlink()
    except FileNotFoundError:
        pass


@dataclass
class Settings:
    resume: bool = True
    model: str = ""


def default_settings_path() -> Path:
    return default_path().parent / "settings.json"


def load_settings(path: Optional[Path] = None) -> Settings:
    target = path or default_settings_path()
    if not target.is_file():
        return Settings()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Settings()
    return Settings(
        resume=bool(payload.get("resume", True)),
        model=str(payload.get("model") or ""),
    )


def save_settings(settings: Settings, path: Optional[Path] = None) -> None:
    target = path or default_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps({"resume": settings.resume, "model": settings.model}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
