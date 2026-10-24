"""Live-Events von OpenCode (SSE) und dezente Fortschritts-Signale.

Der Narrator liest ``GET /api/event``, filtert auf die eigene Session und
startet beim Denken eine leise Klangschleife. Gesprochen werden nur echte
Tool-Meilensteine (und Fehler), höchstens alle
``VOICE_PROGRESS_INTERVAL_S`` Sekunden. Die Endantwort spricht der Hauptloop.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Optional

import requests

from .config import Config
from .opencode_client import OpenCodeClient, SERVICE_USER

TOOL_PHRASES = (
    (("read", "glob", "grep", "list", "find", "search"), "Ich schaue nach."),
    (("edit", "write", "patch"), "Ich ändere etwas."),
    (("shell", "bash"), "Ich führe einen Befehl aus."),
    (("subagent", "task"), "Ich starte einen Helfer."),
    (("webfetch", "websearch"), "Ich recherchiere im Web."),
)

SUBAGENT_TOOLS = ("subagent", "task")
SUBAGENT_PHRASES = {
    "scout": "Ich gebe das an den Scout.",
    "coder": "Ich gebe das an den Coder.",
    "voice-reviewer": "Ich lasse das prüfen.",
}


def phrase_for_tool(name: str) -> str:
    """Übersetzt einen Tool-Namen in einen kurzen Sprech-Meilenstein."""
    lowered = (name or "").lower()
    for needles, phrase in TOOL_PHRASES:
        if any(needle in lowered for needle in needles):
            return phrase
    return "Ich arbeite weiter."


def phrase_for_subagent(agent: str) -> str:
    """Sprech-Meilenstein für einen namentlich bekannten Subagenten."""
    return SUBAGENT_PHRASES.get((agent or "").lower(), "Ich gebe das an einen Helfer.")


def subagent_call(name: str, data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Liest ``agent``/``description`` aus einem Subagenten-Toolaufruf."""
    if (name or "").lower() not in SUBAGENT_TOOLS:
        return None
    payload = data.get("input")
    if not isinstance(payload, dict):
        return None
    agent = str(
        payload.get("agent") or payload.get("subagent_type") or payload.get("name") or ""
    ).strip()
    if not agent:
        return None
    return {
        "agent": agent,
        "description": str(payload.get("description") or "").strip(),
    }


class Narrator:
    def __init__(
        self,
        cfg: Config,
        client: OpenCodeClient,
        session_id: str,
        speak: Callable[[str, threading.Event], None],
        signal_start: Optional[Callable[[], None]] = None,
        signal_stop: Optional[Callable[[], None]] = None,
        interval_s: Optional[float] = None,
        on_subagent: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self._cfg = cfg
        self._session_id = session_id
        self._speak = speak
        self._signal_start = signal_start
        self._signal_stop = signal_stop
        self._interval = cfg.progress_interval_s if interval_s is None else interval_s
        self._on_subagent = on_subagent
        self._subagents: Dict[str, Dict[str, str]] = {}
        self._force_next = False
        self._endpoint = client.endpoint
        self._auth = (SERVICE_USER, self._endpoint.password)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._pending: Optional[str] = None
        self._pending_signal = False
        self._active = False
        self._lock = threading.Lock()
        self._response: Optional[requests.Response] = None
        self._reader: Optional[threading.Thread] = None
        self._speaker: Optional[threading.Thread] = None
        self._tool_names: Dict[str, str] = {}

    def start(self) -> None:
        self._reader = threading.Thread(target=self._read, name="narrator-reader", daemon=True)
        self._speaker = threading.Thread(
            target=self._announce, name="narrator-speaker", daemon=True
        )
        self._reader.start()
        self._speaker.start()

    def wait_ready(self, timeout: float = 1.0) -> bool:
        """Wartet, bis der Ereignisstrom verbunden ist (best effort)."""
        return self._ready.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        self._safe_stop()
        response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass
        if self._speaker is not None:
            self._speaker.join(timeout=2.0)
        if self._reader is not None:
            self._reader.join(timeout=1.0)

    def _safe_start(self) -> None:
        if self._signal_start is not None:
            try:
                self._signal_start()
            except Exception:  # noqa: BLE001
                pass

    def _safe_stop(self) -> None:
        if self._signal_stop is not None:
            try:
                self._signal_stop()
            except Exception:  # noqa: BLE001
                pass

    def _read(self) -> None:
        try:
            response = requests.get(
                self._endpoint.url + "/api/event",
                auth=self._auth,
                stream=True,
                timeout=(5.0, 300.0),
            )
            self._response = response
            self._ready.set()
            with response:
                for line in response.iter_lines(decode_unicode=True):
                    if self._stop.is_set():
                        return
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except ValueError:
                        continue
                    self._handle(event)
        except Exception:  # noqa: BLE001 – der Ereignisstrom ist optional
            self._ready.set()
            return

    def _handle(self, event: Dict[str, Any]) -> None:
        data = event.get("data") or {}
        if str(data.get("sessionID") or "") != self._session_id:
            return

        kind = str(event.get("type") or "")
        phrase: Optional[str] = None
        force = False

        if kind == "session.tool.input.started":
            name = data.get("name")
            call_id = data.get("id")
            if name and call_id:
                self._tool_names[str(call_id)] = str(name)
            return

        if kind == "session.execution.started":
            self._active = True
            with self._lock:
                self._pending_signal = True
            return

        if kind == "session.execution.succeeded":
            # Kann auch für Teil-/Sub-Ausführungen kommen: Schleife weiterlaufen
            # lassen, bis die Antwort wirklich fertig ist (narrator.stop()).
            return
        if kind in ("session.execution.failed", "session.execution.interrupted"):
            self._active = False
            self._safe_stop()
            for info in self._subagents.values():
                self._emit_subagent(
                    info["agent"], "failed", info.get("description", "")
                )
            self._subagents.clear()
            phrase = (
                "Das ist fehlgeschlagen."
                if kind == "session.execution.failed"
                else "Ich habe abgebrochen."
            )
        elif kind == "session.tool.called":
            call_id = str(data.get("id") or "")
            name = self._tool_names.get(call_id, "")
            info = subagent_call(name, data)
            if info is not None:
                self._subagents[call_id] = info
                self._emit_subagent(
                    info["agent"], "running", info.get("description", "")
                )
                phrase = phrase_for_subagent(info["agent"])
                force = True
            else:
                phrase = phrase_for_tool(name)
        elif kind in ("session.tool.success", "session.tool.error", "session.tool.failed"):
            call_id = str(data.get("id") or "")
            info = self._subagents.pop(call_id, None)
            if info is None:
                return
            if kind == "session.tool.success":
                self._emit_subagent(
                    info["agent"], "done", info.get("description", "")
                )
            else:
                self._emit_subagent(
                    info["agent"], "failed", info.get("description", "")
                )
                phrase = "Das ist fehlgeschlagen."

        if phrase:
            with self._lock:
                self._pending = phrase
                if force:
                    self._force_next = True

    def _emit_subagent(self, agent: str, state: str, description: str = "") -> None:
        """Meldet einen Subagenten-Status an die UI (best effort)."""
        if self._on_subagent is None:
            return
        try:
            self._on_subagent(
                {"agent": agent, "state": state, "description": description or ""}
            )
        except Exception:  # noqa: BLE001 – Anzeige ist optional
            pass

    def _announce(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            time.sleep(0.1)
            with self._lock:
                signal = self._pending_signal
                self._pending_signal = False
                force = self._force_next
                self._force_next = False
                phrase = self._pending
                self._pending = None

            if signal:
                self._safe_start()
                last = time.monotonic()
                continue

            if phrase is None:
                continue
            if not force and time.monotonic() - last < self._interval:
                continue

            # Klangschleife für die Ansage kurz aussetzen
            self._safe_stop()
            self._speak(phrase, self._stop)
            if self._active and not self._stop.is_set():
                self._safe_start()
            last = time.monotonic()
