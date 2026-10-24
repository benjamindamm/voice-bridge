"""Client für die OpenCode-v2-HTTP-API.

Ablauf je Äußerung:

1. Session anlegen (``location`` = Arbeitsordner) oder vorhandene ID nutzen
2. Prompt senden
3. Nachrichten pollen, bis eine ``idle``-Nachricht den Abschluss meldet
4. Text der letzten Assistenten-Nachricht zurückgeben

Der Dienst wird standardmäßig über ``opencode service status`` und
``~/.config/opencode/service.json`` gefunden; ``VOICE_OPENCODE_URL`` und
``VOICE_OPENCODE_PASSWORD`` überschreiben das.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .config import Config

SERVICE_USER = "opencode"
SERVICE_FILE = Path("~/.config/opencode/service.json")


class OpenCodeError(RuntimeError):
    """OpenCode nicht erreichbar, abgelehnt oder unerwartete Antwort."""


@dataclass
class Endpoint:
    url: str
    password: str


@dataclass
class AskResult:
    session_id: str
    text: str
    outcome: str
    duration_s: float


def _status_url(timeout_s: float = 15.0) -> str:
    try:
        proc = subprocess.run(
            ["opencode", "service", "status"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OpenCodeError("opencode service status nicht ausführbar: {}".format(exc)) from exc
    for line in proc.stdout.splitlines():
        candidate = line.strip()
        if candidate.startswith("http"):
            return candidate
    return ""


def _service_password() -> str:
    path = SERVICE_FILE.expanduser()
    if not path.is_file():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return ""
    password = payload.get("password")
    return password if isinstance(password, str) else ""


def discover_endpoint(cfg: Config) -> Endpoint:
    """Ermittelt URL und Passwort des OpenCode-Dienstes."""
    if cfg.opencode_url:
        return Endpoint(cfg.opencode_url.rstrip("/"), cfg.opencode_password or _service_password())

    url = _status_url()
    if not url:
        try:
            subprocess.run(
                ["opencode", "service", "start"],
                capture_output=True,
                text=True,
                timeout=30.0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OpenCodeError("OpenCode-Dienst nicht startbar: {}".format(exc)) from exc
        url = _status_url()
    if not url:
        raise OpenCodeError(
            "OpenCode-Dienst läuft nicht. Starten mit: opencode service start"
        )

    return Endpoint(url.rstrip("/"), cfg.opencode_password or _service_password())


def _message_time(message: Dict[str, Any]) -> float:
    return float((message.get("time") or {}).get("created", 0))


def _message_text(message: Dict[str, Any]) -> str:
    """Sichtbarer Text einer Assistenten-Nachricht (ohne Reasoning/Tools)."""
    parts = message.get("content") or []
    return "".join(
        str(part.get("text", ""))
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()


def _split_model(value: Optional[str]) -> Optional[Dict[str, str]]:
    """Wandelt ``provider/model`` in eine Model.Ref um."""
    if not value:
        return None
    provider, separator, model_id = value.strip().partition("/")
    if not separator or not provider or not model_id:
        return None
    return {"providerID": provider, "id": model_id}


class OpenCodeClient:
    def __init__(
        self,
        cfg: Config,
        endpoint: Optional[Endpoint] = None,
        http: Optional[requests.Session] = None,
    ) -> None:
        self._cfg = cfg
        self._endpoint = endpoint
        self._http = http or requests.Session()
        self._agents: Optional[List[str]] = None

    @property
    def endpoint(self) -> Endpoint:
        if self._endpoint is None:
            self._endpoint = discover_endpoint(self._cfg)
        if getattr(self._http, "auth", None) is None and self._endpoint.password:
            self._http.auth = (SERVICE_USER, self._endpoint.password)
        return self._endpoint

    def _get(self, path: str, timeout: tuple = (5.0, 15.0)) -> requests.Response:
        try:
            return self._http.get(self.endpoint.url + path, timeout=timeout)
        except requests.RequestException as exc:
            raise OpenCodeError(
                "OpenCode nicht erreichbar ({}): {}".format(self.endpoint.url, exc)
            ) from exc

    def _post(self, path: str, payload: Dict[str, Any]) -> requests.Response:
        try:
            return self._http.post(
                self.endpoint.url + path,
                json=payload,
                timeout=(5.0, self._cfg.request_timeout_s),
            )
        except requests.RequestException as exc:
            raise OpenCodeError(
                "OpenCode nicht erreichbar ({}): {}".format(self.endpoint.url, exc)
            ) from exc

    @staticmethod
    def _check(response: requests.Response) -> None:
        if response.status_code < 400:
            return
        detail = (response.text or "").strip()[:300]
        hint = " (VOICE_OPENCODE_PASSWORD prüfen)" if response.status_code == 401 else ""
        raise OpenCodeError("OpenCode HTTP {}{}: {}".format(response.status_code, hint, detail))

    def _agent_ids(self) -> List[str]:
        if self._agents is None:
            response = self._get("/api/agent")
            self._check(response)
            payload = response.json()
            self._agents = [
                item["id"]
                for item in payload.get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
        return self._agents

    def create_session(self, title: str = "Voice", model: Optional[str] = None) -> str:
        payload: Dict[str, Any] = {
            "title": title,
            "location": {"directory": str(self._cfg.session_dir)},
        }
        if self._cfg.agent and self._cfg.agent in self._agent_ids():
            payload["agent"] = self._cfg.agent
        ref = _split_model(model)
        if ref is not None:
            payload["model"] = ref

        response = self._post("/api/session", payload)
        self._check(response)
        session_id = (response.json().get("data") or {}).get("id")
        if not session_id:
            raise OpenCodeError("OpenCode lieferte keine Session-ID.")
        return str(session_id)

    def session_exists(self, session_id: str) -> bool:
        """Prüft, ob eine Session noch existiert (für die Persistenz)."""
        try:
            response = self._get("/api/session/{}".format(session_id), timeout=(5.0, 8.0))
        except OpenCodeError:
            return False
        return response.status_code == 200

    def interrupt(self, session_id: str) -> bool:
        """Bricht eine laufende Ausführung ab (best effort)."""
        try:
            response = self._http.post(
                self.endpoint.url + "/api/session/{}/interrupt".format(session_id),
                timeout=(5.0, 10.0),
            )
        except requests.RequestException:
            return False
        return response.status_code < 400

    def list_sessions(
        self, directory: Optional[str] = None, limit: int = 6
    ) -> List[Dict[str, Any]]:
        """Listet die jüngsten Sitzungen (optional auf einen Ordner gefiltert)."""
        response = self._get("/api/session", timeout=(5.0, 15.0))
        self._check(response)
        data = response.json().get("data", [])
        sessions = []
        for item in data:
            if not isinstance(item, dict):
                continue
            # Subagenten laufen in eigenen Kind-Sitzungen (parentID). Die gehören
            # nicht in die Auswahl; verfolgt werden sie über die Helfer-Leiste.
            if item.get("parentID"):
                continue
            if directory is not None:
                location = item.get("location") or {}
                if str(location.get("directory") or "") != directory:
                    continue
            sessions.append(item)

        def updated(session: Dict[str, Any]) -> float:
            info = session.get("time") or {}
            return float(info.get("updated") or info.get("created") or 0.0)

        sessions.sort(key=updated, reverse=True)
        return sessions[:limit]

    def list_models(self) -> List[Dict[str, str]]:
        """Listet Modelle aktivierter Provider, die Werkzeuge unterstützen."""
        providers_response = self._get("/api/provider", timeout=(5.0, 15.0))
        self._check(providers_response)
        enabled = {
            str(item.get("id"))
            for item in providers_response.json().get("data", [])
            if isinstance(item, dict) and item.get("activation") == "enabled"
        }

        models_response = self._get("/api/model", timeout=(5.0, 15.0))
        self._check(models_response)
        models: List[Dict[str, str]] = []
        for item in models_response.json().get("data", []):
            if not isinstance(item, dict):
                continue
            provider = str(item.get("providerID") or "")
            model_id = str(item.get("modelID") or item.get("id") or "")
            if provider not in enabled or not model_id:
                continue
            if not item.get("enabled", True):
                continue
            capabilities = item.get("capabilities") or {}
            if capabilities.get("tools") is False:
                continue
            models.append(
                {
                    "providerID": provider,
                    "modelID": model_id,
                    "name": str(item.get("name") or model_id),
                    "value": "{}/{}".format(provider, model_id),
                }
            )

        models.sort(key=lambda entry: (entry["providerID"], entry["name"]))
        return models

    def set_model(self, session_id: str, model: str) -> bool:
        """Wechselt das Modell einer bestehenden Sitzung."""
        ref = _split_model(model)
        if ref is None:
            return False
        response = self._post(
            "/api/session/{}/model".format(session_id), {"model": ref}
        )
        self._check(response)
        return True

    def list_mcp(self) -> List[Dict[str, Any]]:
        """Listet MCP-Server mit Verbindungsstatus und Ein-/Aus-Zustand."""
        statuses: Dict[str, str] = {}
        try:
            status_response = self._get("/api/mcp", timeout=(5.0, 15.0))
            self._check(status_response)
            for item in status_response.json().get("data", []):
                if not isinstance(item, dict) or not item.get("name"):
                    continue
                status = item.get("status") or {}
                statuses[str(item["name"])] = str(status.get("status") or "unknown")
        except OpenCodeError:
            statuses = {}

        servers: Dict[str, Any] = {}
        try:
            config_response = self._get("/api/config", timeout=(5.0, 15.0))
            self._check(config_response)
            entries = config_response.json()
            if isinstance(entries, dict):
                entries = entries.get("data", [])
            document = next(
                (
                    entry
                    for entry in entries
                    if isinstance(entry, dict) and entry.get("type") == "document"
                ),
                None,
            )
            mcp = ((document or {}).get("info") or {}).get("mcp") or {}
            if isinstance(mcp.get("servers"), dict):
                servers = mcp["servers"]
            elif isinstance(mcp, dict):
                servers = {
                    key: value for key, value in mcp.items() if isinstance(value, dict)
                }
        except Exception:  # noqa: BLE001 – Statusliste funktioniert auch ohne Config
            servers = {}

        rows: List[Dict[str, Any]] = []
        for name, entry in servers.items():
            if name == "servers":
                continue
            disabled = bool(entry.get("disabled")) or entry.get("enabled") is False
            status = "disabled" if disabled else statuses.get(name, "unknown")
            rows.append(
                {
                    "name": name,
                    "status": status,
                    "enabled": not disabled,
                    "configurable": True,
                }
            )

        known = {row["name"] for row in rows}
        for name, status in statuses.items():
            if name not in known:
                rows.append(
                    {
                        "name": name,
                        "status": status,
                        "enabled": True,
                        "configurable": False,
                    }
                )

        rows.sort(key=lambda row: row["name"].lower())
        return rows

    def history(
        self, session_id: str, max_turns: int = 20
    ) -> List[Dict[str, Any]]:
        """Lädt den Verlauf als Züge: Nutzer-Text + letzte Assistenten-Antwort."""
        response = self._get(
            "/api/session/{}/message".format(session_id), timeout=(5.0, 20.0)
        )
        self._check(response)
        messages = [
            message
            for message in response.json().get("data", [])
            if isinstance(message, dict)
        ]
        messages.sort(key=_message_time)

        turns: List[Dict[str, Any]] = []
        current: Optional[Dict[str, Any]] = None
        for message in messages:
            kind = message.get("type")
            if kind == "user":
                text = str(message.get("text") or "").strip()
                current = {
                    "user": text,
                    "time": _message_time(message) / 1000.0,
                    "answers": [],
                }
                turns.append(current)
            elif kind == "assistant" and current is not None:
                text = _message_text(message)
                if text:
                    current["answers"].append(
                        (_message_time(message) / 1000.0, text)
                    )

        rows: List[Dict[str, Any]] = []
        for turn in turns[-max(1, max_turns):]:
            if turn["user"]:
                rows.append(
                    {"role": "user", "text": turn["user"], "time": turn["time"]}
                )
            if turn["answers"]:
                stamp, text = turn["answers"][-1]
                rows.append(
                    {"role": "assistant", "text": text, "time": stamp}
                )

        if not rows:
            # Reine TUI-Sitzung ohne Nutzer-Nachrichten: letzte Antworten zeigen.
            for message in messages:
                if message.get("type") != "assistant":
                    continue
                text = _message_text(message)
                if text:
                    rows.append(
                        {
                            "role": "assistant",
                            "text": text,
                            "time": _message_time(message) / 1000.0,
                        }
                    )
            rows = rows[-max(1, max_turns):]
        return rows

    def ask(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        model: Optional[str] = None,
    ) -> AskResult:
        started = time.monotonic()
        sid = session_id or self.create_session(model=model)

        response = self._post(
            "/api/session/{}/prompt".format(sid), {"text": prompt}
        )
        self._check(response)
        user_time = _message_time(response.json().get("data") or {})

        text, outcome = self._wait_for_idle(sid, user_time, cancel_event)
        return AskResult(
            session_id=sid,
            text=text,
            outcome=outcome,
            duration_s=time.monotonic() - started,
        )

    def _wait_for_idle(
        self,
        sid: str,
        user_time: float,
        cancel_event: Optional[threading.Event] = None,
    ) -> tuple:
        deadline = time.monotonic() + self._cfg.request_timeout_s
        poll_s = max(0.05, self._cfg.opencode_poll_ms / 1000.0)

        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                return "", "interrupted"
            time.sleep(poll_s)
            response = self._get("/api/session/{}/message".format(sid))
            self._check(response)
            messages = response.json().get("data", [])
            idle = [
                message
                for message in messages
                if message.get("type") == "idle" and _message_time(message) >= user_time
            ]
            if idle:
                return (
                    _answer_text(messages, user_time),
                    str(idle[-1].get("outcome", "unknown")),
                )

        raise OpenCodeError(
            "Zeitüberschreitung beim Warten auf die Antwort ({:.0f} s).".format(
                self._cfg.request_timeout_s
            )
        )


def _answer_text(messages: List[Dict[str, Any]], user_time: float) -> str:
    """Text der letzten Assistenten-Nachricht mit Inhalt, sonst Zusammenfassung."""
    assistants = [
        message
        for message in messages
        if message.get("type") == "assistant" and _message_time(message) >= user_time
    ]
    assistants.sort(key=_message_time)

    for message in reversed(assistants):
        candidate = _message_text(message)
        if candidate:
            return candidate

    return "\n\n".join(
        filter(None, (_message_text(message) for message in assistants))
    ).strip()
