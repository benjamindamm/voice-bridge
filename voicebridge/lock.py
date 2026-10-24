"""Harte Sperren gegen mehrere gleichzeitig aktive Instanzen.

- ``loop.lock``: nur eine aktive Sprachsitzung (Widget oder ``--run``).
- ``widget.lock``: nur ein Menüleisten-Widget.

Die Sperre ist ein ``flock`` und wird vom Betriebssystem automatisch
freigegeben, wenn der Prozess endet – auch nach einem Absturz. Zusätzlich
verhindert eine prozessweite Menge doppelte Sperren im selben Prozess.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from typing import Optional, Set

_HELD: Set[str] = set()


class AlreadyRunningError(RuntimeError):
    """Eine andere Instanz hält die Sperre bereits."""

    def __init__(self, pid: Optional[int] = None) -> None:
        message = "Es läuft bereits eine Sprachsitzung"
        if pid:
            message += " (PID {})".format(pid)
        message += ". Bitte zuerst die andere Instanz beenden."
        super().__init__(message)


def state_dir() -> Path:
    override = os.environ.get("VOICE_BRIDGE_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/.local/state/voice-bridge").expanduser()


def default_loop_lock() -> Path:
    return state_dir() / "loop.lock"


def default_widget_lock() -> Path:
    return state_dir() / "widget.lock"


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self._path = Path(path).expanduser()
        self._key = str(self._path.resolve())
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        if self._key in _HELD:
            raise AlreadyRunningError(os.getpid())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunningError(self._read_pid()) from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode("ascii"))
        self._fd = fd
        _HELD.add(self._key)

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            _HELD.discard(self._key)

    def _read_pid(self) -> Optional[int]:
        try:
            value = self._path.read_text(encoding="ascii").strip()
        except OSError:
            return None
        try:
            return int(value) if value else None
        except ValueError:
            return None
