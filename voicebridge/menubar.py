"""VoiceBridge-Cockpit: Menüleiste + WKWebView-UI.

Die Oberfläche ist HTML/CSS/JS (assets/cockpit.html) in einem WKWebView.
Der Python-Teil liefert Live-State (Status, Pegel, Latenz, Sitzungen,
Modelle, MCP-Dienste, Transkript) und verarbeitet die Aktionen aus der UI.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import rumps
from AppKit import (
    NSBackingStoreBuffered,
    NSFloatingWindowLevel,
    NSImage,
    NSMakeRect,
    NSObject,
    NSApp,
    NSPanel,
    NSStatusWindowLevel,
    NSScreen,
    NSURL,
    NSViewHeightSizable,
    NSViewWidthSizable,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorMoveToActiveSpace,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from WebKit import WKUserContentController, WKWebView, WKWebViewConfiguration

from . import sounds, state
from .config import Config
from .lock import AlreadyRunningError, InstanceLock, default_widget_lock
from .loop import State, VoiceLoop
from .opencode_client import OpenCodeClient
from .opencode_config import set_mcp_enabled

ASSET = Path(__file__).resolve().parent / "assets" / "cockpit.html"

SYMBOLS = {
    State.OFF: "waveform.circle.fill",
    State.MUTED: "mic.slash.circle.fill",
    State.LISTENING: "mic.circle.fill",
    State.TRANSCRIBING: "waveform.circle.fill",
    State.THINKING: "ellipsis.circle.fill",
    State.SPEAKING: "speaker.wave.2.circle.fill",
    State.ERROR: "exclamationmark.circle.fill",
}

LABELS = {
    State.OFF: "Bereit",
    State.MUTED: "Mikrofon stumm",
    State.LISTENING: "Hört zu",
    State.TRANSCRIBING: "Transkribiert",
    State.THINKING: "Denkt nach",
    State.SPEAKING: "Spricht",
    State.ERROR: "Fehler",
}

STATE_KEYS = {
    State.OFF: "off",
    State.MUTED: "muted",
    State.LISTENING: "listening",
    State.TRANSCRIBING: "transcribing",
    State.THINKING: "thinking",
    State.SPEAKING: "speaking",
    State.ERROR: "error",
}


def _symbol_image(name: str, size: float = 16.0):
    from AppKit import NSImageSymbolConfiguration

    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if image is None:
        return None
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        max(8.0, size - 1.0), 0.0
    )
    configured = image.imageWithSymbolConfiguration_(config)
    if configured is not None:
        image = configured
    image.setTemplate_(True)
    image.setSize_((size, size))
    return image


class _VoiceMessageHandler(NSObject):
    def initWithCallback_(self, callback):
        self = self.init()
        if self is None:
            return None
        self._callback = callback
        return self

    def userContentController_didReceiveScriptMessage_(self, _controller, message):
        try:
            body = message.body()
            data = {str(key): body.objectForKey_(key) for key in body.allKeys()}
        except Exception:  # noqa: BLE001
            data = {}
        try:
            self._callback(data)
        except Exception:  # noqa: BLE001
            pass


class MenubarApp(rumps.App):
    def __init__(self, cfg: Config) -> None:
        super().__init__("VoiceBridge", title=" ", quit_button=None)
        self._cfg = cfg
        self._settings = state.load_settings()
        self._client = OpenCodeClient(cfg)
        self._state = State.OFF
        self._last_message = ""
        self._lock = threading.Lock()
        self._loop: Optional[VoiceLoop] = None
        self._images = {state: _symbol_image(name) for state, name in SYMBOLS.items()}
        self._shown_state: Optional[State] = None

        self._panel = None
        self._webview: Optional[WKWebView] = None
        self._message_handler: Optional[_VoiceMessageHandler] = None
        self._web_ready = False
        self._panel_ready = False
        self._panel_shown_at = 0.0
        self._web_timeout_reported = False
        self._closing = False

        self._pending_sessions = None
        self._pending_models = None
        self._pending_mcps = None
        self._ui_sessions: List[Dict[str, str]] = []
        self._ui_models: List[Dict[str, str]] = []
        self._ui_mcps: List[Dict[str, str]] = []
        self._ui_subagents: List[Dict[str, str]] = [
            {"name": "scout", "status": "idle", "description": ""},
            {"name": "coder", "status": "idle", "description": ""},
            {"name": "voice-reviewer", "status": "idle", "description": ""},
        ]
        self._pending_subagents = None
        self._subagent_lock = threading.Lock()
        self._last_state_json = ""
        self._sent_transcript = -1

        self._transcript_lines = []
        self._transcript_lock = threading.Lock()
        self._clear_requested = False
        self._pending_transcript = None
        self._history_session: Optional[str] = None
        self._history_token = 0

        self._status = rumps.MenuItem("Status: Bereit")
        self._status.enabled = False
        self._window_item = rumps.MenuItem(
            "Cockpit anzeigen", callback=self._on_window
        )
        self._toggle = rumps.MenuItem("Zuhören starten", callback=self._on_toggle)
        self._stop_item = rumps.MenuItem("Stopp", callback=self._on_stop)
        self._stop_item.enabled = False
        self._send_item = rumps.MenuItem("Jetzt senden", callback=self._on_send)
        self._send_item.enabled = False
        self._new = rumps.MenuItem("Neue Sitzung", callback=self._on_new_session)
        self._test_signal_item = rumps.MenuItem(
            "Denk-Ton testen", callback=self._on_test_signal
        )
        self._quit = rumps.MenuItem("Beenden", callback=self._on_quit)

        self.menu = [
            self._status,
            None,
            self._window_item,
            None,
            self._toggle,
            self._stop_item,
            self._send_item,
            self._new,
            None,
            self._test_signal_item,
            None,
            self._quit,
        ]

        self._make_loop(resume=self._settings.resume)
        self._timer = rumps.Timer(self._tick, 0.3)
        self._timer.start()
        threading.Thread(target=self._refresh_loop, daemon=True).start()

    # Loop/Callbacks

    def _make_loop(self, resume: bool, session_id: Optional[str] = None) -> None:
        self._loop = VoiceLoop(
            self._cfg,
            on_event=self._on_event,
            on_state=self._on_state,
            on_transcript=self._on_transcript,
            on_subagent=self._on_subagent,
            resume=resume,
            session_id=session_id,
            model=self._settings.model or None,
        )
        self._reset_subagents()

    def _on_state(self, state_now: State) -> None:
        with self._lock:
            self._state = state_now

    def _on_event(self, message: str) -> None:
        with self._lock:
            self._last_message = message
        print(message, flush=True)

    def _on_transcript(self, role: str, text: str) -> None:
        with self._transcript_lock:
            self._transcript_lines.append((time.time(), role, text))

    def _on_subagent(self, info: Dict[str, Any]) -> None:
        """Hält die Helfer-Leiste aktuell (aufgerufen aus dem Loop-Thread)."""
        agent = str(info.get("agent") or "")
        state_now = str(info.get("state") or "")
        description = str(info.get("description") or "")
        with self._subagent_lock:
            if agent == "*" or state_now == "reset":
                for row in self._ui_subagents:
                    row["status"] = "idle"
                    row["description"] = ""
            else:
                row = next(
                    (item for item in self._ui_subagents if item["name"] == agent),
                    None,
                )
                if row is None:
                    self._ui_subagents.append(
                        {
                            "name": agent,
                            "status": state_now,
                            "description": description,
                        }
                    )
                else:
                    row["status"] = state_now
                    row["description"] = description
            self._pending_subagents = [dict(item) for item in self._ui_subagents]

    def _reset_subagents(self) -> None:
        self._on_subagent({"agent": "*", "state": "reset", "description": ""})

    # WebView

    def _eval_js(self, function: str, payload: Any) -> None:
        if not self._web_ready or self._webview is None:
            return
        script = "window.voice.{}({});".format(
            function, json.dumps(payload, ensure_ascii=False)
        )
        self._webview.evaluateJavaScript_completionHandler_(script, None)

    def _send_state(self) -> None:
        loop = self._loop
        state_now = self._state
        timings = loop.timings if loop is not None else {"stt": None, "llm": None, "tts": None}
        payload = {
            "state": STATE_KEYS.get(state_now, "off"),
            "label": LABELS.get(state_now, state_now.value),
            "running": bool(loop is not None and loop.running),
            "busy": bool(loop is not None and loop.busy),
            "listening": bool(
                loop is not None and loop.running and state_now == State.LISTENING
            ),
            "muted": bool(loop is not None and loop.muted),
            "speak": True if loop is None else loop.speak_enabled,
            "level": float(loop.level) if loop is not None else 0.0,
            "latency": timings,
            "session": loop.session_id if loop is not None else "",
            "model": (loop.model if loop is not None else self._settings.model) or "",
            "resume": bool(self._settings.resume),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if encoded != self._last_state_json:
            self._last_state_json = encoded
            self._eval_js("update", payload)

    def _send_transcript(self, lines, force_bottom: bool = False) -> None:
        payload = {
            "lines": [
                {
                    "time": time.strftime("%H:%M", time.localtime(stamp)),
                    "role": role,
                    "text": text,
                }
                for stamp, role, text in lines
            ],
            "forceBottom": force_bottom,
        }
        self._eval_js("setTranscript", payload)

    def _on_ready(self) -> None:
        self._web_ready = True
        self._last_state_json = ""
        self._sent_transcript = -1
        self._on_event("Cockpit bereit")
        self._send_state()
        self._eval_js("setModels", self._ui_models)
        self._eval_js("setSessions", self._ui_sessions)
        self._eval_js("setMcps", self._ui_mcps)
        with self._subagent_lock:
            subagents = [dict(item) for item in self._ui_subagents]
        self._eval_js("setSubagents", subagents)
        with self._transcript_lock:
            lines = list(self._transcript_lines)
        self._send_transcript(lines, force_bottom=True)
        self._sent_transcript = len(lines)

    def _on_js_message(self, data: Dict[str, Any]) -> None:
        action = str(data.get("action") or "")
        if action == "ready":
            self._on_ready()
        elif action == "toggle":
            self._on_toggle(None)
        elif action == "stop":
            self._on_stop(None)
        elif action == "send":
            self._on_send(None)
        elif action == "mute":
            self._on_toggle_mic(None)
        elif action == "speak":
            self._on_toggle_speak(None)
        elif action == "newSession":
            self._on_new_session(None)
        elif action == "refresh":
            threading.Thread(target=self._fetch_all, daemon=True).start()
        elif action == "resume":
            self._set_resume(bool(data.get("value")))
        elif action == "model":
            self._on_select_model(str(data.get("value") or ""))
        elif action == "session":
            self._on_select_session(str(data.get("id") or ""))
        elif action == "mcp":
            self._on_toggle_mcp(str(data.get("name") or ""), bool(data.get("enabled")))
        elif action == "jsError":
            self._on_event("JS-Fehler: {}".format(data.get("message")))

    # Timer/Menü

    def _tick(self, _timer=None) -> None:
        with self._lock:
            state_now = self._state
            message = self._last_message

        self._apply_icon(state_now)
        self._status.title = "Status: {}".format(LABELS.get(state_now, state_now.value))

        loop = self._loop
        running = loop is not None and loop.running
        busy = loop is not None and loop.busy
        listening = running and state_now == State.LISTENING
        self._toggle.title = "Zuhören stoppen" if running else "Zuhören starten"
        self._stop_item.enabled = busy
        self._send_item.enabled = listening

        if not self._panel_ready:
            self._show_panel()
            self._panel_ready = True

        if self._panel is not None:
            self._window_item.title = "Cockpit anzeigen"
            if (
                not self._web_ready
                and self._panel_shown_at
                and not self._web_timeout_reported
                and time.monotonic() - self._panel_shown_at > 8.0
            ):
                self._web_timeout_reported = True
                self._on_event("Cockpit: JavaScript hat sich nicht gemeldet.")

        pending_sessions = self._pending_sessions
        if pending_sessions is not None:
            self._pending_sessions = None
            self._ui_sessions = pending_sessions
            self._eval_js("setSessions", self._ui_sessions)
        pending_models = self._pending_models
        if pending_models is not None:
            self._pending_models = None
            self._ui_models = pending_models
            self._eval_js("setModels", self._ui_models)
        pending_mcps = self._pending_mcps
        if pending_mcps is not None:
            self._pending_mcps = None
            self._ui_mcps = pending_mcps
            self._eval_js("setMcps", self._ui_mcps)
        pending_subagents = self._pending_subagents
        if pending_subagents is not None:
            self._pending_subagents = None
            with self._subagent_lock:
                self._ui_subagents = pending_subagents
            self._eval_js("setSubagents", pending_subagents)

        if self._clear_requested:
            self._clear_requested = False
            self._pending_transcript = None
            with self._transcript_lock:
                self._transcript_lines = []
            self._send_transcript([], force_bottom=True)
            self._sent_transcript = 0

        pending_transcript = self._pending_transcript
        if pending_transcript is not None:
            self._pending_transcript = None
            self._sent_transcript = len(pending_transcript)
            self._send_transcript(pending_transcript, force_bottom=True)

        loop = self._loop
        current_session = loop.session_id if loop is not None else None
        if current_session and current_session != self._history_session:
            self._history_session = current_session
            with self._transcript_lock:
                empty = not self._transcript_lines
            if empty:
                self._load_history_async(current_session)

        with self._transcript_lock:
            lines = list(self._transcript_lines)
        if len(lines) != self._sent_transcript:
            self._send_transcript(lines)
            self._sent_transcript = len(lines)

        self._send_state()

    def _apply_icon(self, state_now: State) -> None:
        if state_now == self._shown_state:
            return
        image = self._images.get(state_now)
        nsapp = getattr(self, "_nsapp", None)
        if image is None or nsapp is None:
            return
        nsapp.nsstatusitem.setImage_(image)
        nsapp.nsstatusitem.setTitle_("")
        self._shown_state = state_now

    # Daten für die UI

    def _fetch_all(self) -> None:
        try:
            sessions = self._client.list_sessions(
                directory=str(self._cfg.session_dir), limit=8
            )
        except Exception:  # noqa: BLE001
            sessions = []
        try:
            models = self._client.list_models()
        except Exception:  # noqa: BLE001
            models = []
        try:
            mcps = self._client.list_mcp()
        except Exception:  # noqa: BLE001
            mcps = []

        self._pending_sessions = [self._session_row(row) for row in sessions]
        self._pending_models = [
            {
                "value": row["value"],
                "label": row["name"],
                "providerID": row["providerID"],
                "providerName": row.get("providerName", row["providerID"]),
                "free": bool(row.get("free")),
            }
            for row in models
        ]
        self._pending_mcps = [
            {
                "name": row["name"],
                "status": row["status"],
                "enabled": row.get("enabled", True),
                "configurable": row.get("configurable", True),
            }
            for row in mcps
        ]

    def _refresh_loop(self) -> None:
        while not self._closing:
            self._fetch_all()
            for _ in range(100):
                if self._closing:
                    return
                time.sleep(0.1)

    @staticmethod
    def _session_row(row) -> Dict[str, str]:
        title = str(row.get("title") or "Sitzung").strip() or "Sitzung"
        info = row.get("time") or {}
        stamp = info.get("updated") or info.get("created") or 0
        when = ""
        if stamp:
            moment = time.localtime(float(stamp) / 1000.0)
            if time.strftime("%Y-%m-%d", moment) == time.strftime("%Y-%m-%d"):
                when = time.strftime("%H:%M", moment)
            else:
                when = time.strftime("%d.%m. %H:%M", moment)
        session_id = str(row.get("id") or "")
        suffix = "…" + session_id[-6:] if session_id else ""
        label = title[:24]
        if when:
            label += " · " + when
        if suffix:
            label += " · " + suffix
        return {"id": session_id, "label": label}

    # Cockpit-Fenster

    def _build_panel(self) -> None:
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0.0, 0.0, 460.0, 680.0),
            NSWindowStyleMaskTitled
            | NSWindowStyleMaskClosable
            | NSWindowStyleMaskMiniaturizable
            | NSWindowStyleMaskResizable,
            NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_("VoiceBridge")
        panel.setFloatingPanel_(True)
        panel.setLevel_(NSStatusWindowLevel)
        panel.setCollectionBehavior_(
            NSWindowCollectionBehaviorMoveToActiveSpace
            | NSWindowCollectionBehaviorFullScreenAuxiliary
        )
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)
        panel.setContentMinSize_((380.0, 560.0))

        config = WKWebViewConfiguration.alloc().init()
        controller = WKUserContentController.alloc().init()
        handler = _VoiceMessageHandler.alloc().initWithCallback_(self._on_js_message)
        controller.addScriptMessageHandler_name_(handler, "voice")
        config.setUserContentController_(controller)

        webview = WKWebView.alloc().initWithFrame_configuration_(
            NSMakeRect(0.0, 0.0, 460.0, 680.0), config
        )
        webview.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        panel.contentView().addSubview_(webview)

        asset = NSURL.fileURLWithPath_(str(ASSET))
        folder = NSURL.fileURLWithPath_(str(ASSET.parent))
        webview.loadFileURL_allowingReadAccessToURL_(asset, folder)

        self._message_handler = handler
        self._webview = webview
        self._panel = panel
        self._panel_shown_at = time.monotonic()
        self._place_panel(panel)

    def _place_panel(self, panel) -> None:
        screen = NSScreen.mainScreen()
        if screen is None:
            return
        frame = screen.visibleFrame()
        x = frame.origin.x + frame.size.width - 460.0 - 24.0
        y = frame.origin.y + frame.size.height - 680.0 - 12.0
        panel.setFrameOrigin_((x, y))

    def _show_panel(self) -> None:
        if self._panel is None:
            self._build_panel()
        if self._panel is not None:
            NSApp.activateIgnoringOtherApps_(True)
            self._panel.makeKeyAndOrderFront_(None)

    def _on_window(self, _sender) -> None:
        self._show_panel()

    # Aktionen

    def _on_toggle(self, _sender) -> None:
        loop = self._loop
        if loop is None:
            return
        if loop.running:
            threading.Thread(target=loop.stop, daemon=True).start()
        else:
            loop.start()

    def _on_stop(self, _sender=None) -> None:
        loop = self._loop
        if loop is not None:
            # Nie im UI-Thread: cancel() spricht mit OpenCode und joint Threads.
            threading.Thread(target=loop.cancel, daemon=True).start()

    def _on_send(self, _sender=None) -> None:
        loop = self._loop
        if loop is not None:
            loop.send()

    def _on_toggle_mic(self, _sender=None) -> None:
        loop = self._loop
        if loop is None:
            return
        loop.muted = not loop.muted
        self._on_transcript(
            "info", "Mikrofon stumm" if loop.muted else "Mikrofon wieder an"
        )

    def _on_toggle_speak(self, _sender=None) -> None:
        loop = self._loop
        if loop is not None:
            loop.speak_enabled = not loop.speak_enabled

    def _set_resume(self, value: bool) -> None:
        self._settings.resume = bool(value)
        try:
            state.save_settings(self._settings)
        except OSError as exc:
            self._on_event("Einstellung nicht speicherbar: {}".format(exc))

    def _on_select_model(self, value: str) -> None:
        self._settings.model = value
        try:
            state.save_settings(self._settings)
        except OSError as exc:
            self._on_event("Einstellung nicht speicherbar: {}".format(exc))
        loop = self._loop
        if loop is not None:
            loop.set_model(value)
        self._on_transcript("info", "Modell: {}".format(value or "Standard"))

    def _history_lines(self, session_id: str):
        """Holt den Verlauf als (Zeitstempel, Rolle, Text) für die Anzeige."""
        rows = OpenCodeClient(self._cfg).history(session_id)
        lines = []
        for row in rows:
            text = str(row.get("text") or "").strip()
            role = str(row.get("role") or "")
            if not text or role not in ("user", "assistant"):
                continue
            lines.append((float(row.get("time") or time.time()), role, text))
        return lines

    def _load_history_async(self, session_id: str) -> None:
        """Lädt den Verlauf im Hintergrund, ohne neue Nachrichten zu überschreiben."""
        self._history_token += 1
        token = self._history_token

        def worker() -> None:
            try:
                lines = self._history_lines(session_id)
            except Exception as exc:  # noqa: BLE001 – Verlauf ist optional
                self._on_event("Verlauf nicht ladbar: {}".format(exc))
                return
            if token != self._history_token or not lines:
                return
            with self._transcript_lock:
                if self._transcript_lines:
                    return
                self._transcript_lines = lines
            self._pending_transcript = list(lines)
            self._on_event("Verlauf geladen: {} Nachrichten".format(len(lines)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_select_session(self, session_id: str) -> None:
        if not session_id:
            return

        self._clear_requested = False
        self._history_token += 1
        token = self._history_token

        def worker() -> None:
            current = self._loop
            if current is not None:
                current.stop()
            try:
                lines = self._history_lines(session_id)
            except Exception as exc:  # noqa: BLE001 – Sitzung startet trotzdem
                self._on_event("Verlauf nicht ladbar: {}".format(exc))
                lines = []
            if token != self._history_token:
                return
            with self._transcript_lock:
                self._transcript_lines = lines
            self._pending_transcript = list(lines)
            self._on_event("Verlauf geladen: {} Nachrichten".format(len(lines)))
            self._history_session = session_id
            self._make_loop(resume=True, session_id=session_id)
            if self._loop is not None:
                self._loop.start()

        threading.Thread(target=worker, daemon=True).start()

    def _on_toggle_mcp(self, name: str, enabled: bool) -> None:
        if not name:
            return

        def worker() -> None:
            try:
                set_mcp_enabled(name, enabled)
            except Exception as exc:  # noqa: BLE001
                self._on_event("MCP {}: {}".format(name, exc))
                return
            self._on_transcript(
                "info",
                "Dienst {} {}".format(name, "eingeschaltet" if enabled else "ausgeschaltet"),
            )
            self._on_transcript(
                "info",
                "Neue Sitzung nötig, damit der Kontext die Dienste neu lädt.",
            )
            self._fetch_all()

        threading.Thread(target=worker, daemon=True).start()

    def _on_test_signal(self, _sender=None) -> None:
        def worker() -> None:
            loop = sounds.ThinkingLoop(volume=self._cfg.signal_volume)
            loop.start()
            time.sleep(2.5)
            loop.stop()

        threading.Thread(target=worker, daemon=True).start()

    def _on_new_session(self, _sender) -> None:
        self._history_token += 1

        def worker() -> None:
            current = self._loop
            if current is not None:
                current.stop()
            self._clear_requested = True
            self._history_session = None
            self._make_loop(resume=False)
            if self._loop is not None:
                self._loop.start()
            self._fetch_all()

        threading.Thread(target=worker, daemon=True).start()

    def _on_quit(self, _sender) -> None:
        self._closing = True
        loop = self._loop
        if loop is not None:
            loop.stop()
        rumps.quit_application()


def run(cfg: Config) -> None:
    from Foundation import NSProcessInfo

    NSProcessInfo.processInfo().setProcessName_("VoiceBridge")
    lock = InstanceLock(default_widget_lock())
    try:
        lock.acquire()
    except AlreadyRunningError as exc:
        rumps.alert(title="VoiceBridge läuft bereits", message=str(exc))
        return
    print("VoiceBridge: Cockpit startet", flush=True)
    try:
        MenubarApp(cfg).run()
    finally:
        lock.release()
