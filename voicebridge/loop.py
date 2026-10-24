"""Zustandsmaschine der Sprachsitzung.

Ablauf: zuhören → transkribieren → denken (mit Fortschritts-Ansagen) → sprechen.
Die Klasse ist ohne UI: ``start()``/``stop()``/``toggle()``/``cancel()`` reichen
für das Widget; ``run_forever()`` ist der Terminal-Modus.
"""

from __future__ import annotations

import math
import threading
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import audio, events, sounds, speech, state, stt, tts
from .config import Config
from .lock import AlreadyRunningError, InstanceLock, default_loop_lock
from .opencode_client import OpenCodeClient, OpenCodeError


class State(str, Enum):
    OFF = "aus"
    MUTED = "stumm"
    LISTENING = "hört zu"
    TRANSCRIBING = "transkribiert"
    THINKING = "denkt"
    SPEAKING = "spricht"
    ERROR = "Fehler"


class VoiceLoop:
    def __init__(
        self,
        cfg: Config,
        on_event: Optional[Callable[[str], None]] = None,
        on_state: Optional[Callable[[State], None]] = None,
        on_transcript: Optional[Callable[[str, str], None]] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        resume: bool = True,
        state_path: Optional[Path] = None,
        lock_path: Optional[Path] = None,
    ) -> None:
        self._cfg = cfg
        self._on_event = on_event or (lambda message: None)
        self._on_state = on_state or (lambda state: None)
        self._on_transcript = on_transcript or (lambda role, text: None)
        self._transcript: List[Tuple[float, str, str]] = []
        self._transcript_lock = threading.Lock()
        self._stop = threading.Event()
        self._cancel = threading.Event()
        self._send = threading.Event()
        self._muted = False
        self._mute_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._session_id = session_id
        self._model = (model or cfg.model or "").strip() or None
        self._resume = resume
        self._state_path = state_path
        self._lock = InstanceLock(lock_path or default_loop_lock())
        self._client: Optional[OpenCodeClient] = None
        self._narrator: Optional[events.Narrator] = None
        self._warm = False
        self._state = State.OFF
        self._level = 0.0
        self._timings: Dict[str, Optional[float]] = {"stt": None, "llm": None, "tts": None}
        self._speak_enabled = cfg.speak
        self._thinking = sounds.ThinkingLoop(volume=cfg.signal_volume)

    @property
    def state(self) -> State:
        return self._state

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def model(self) -> Optional[str]:
        return self._model

    def set_model(self, model: str) -> None:
        """Setzt das Modell für die laufende und alle neuen Sitzungen."""
        value = (model or "").strip() or None
        self._model = value
        client = self._client
        if client is not None and self._session_id is not None and value is not None:
            try:
                client.set_model(self._session_id, value)
                self._emit("Modell: {}".format(value))
            except OpenCodeError as exc:
                self._emit("Modellwechsel fehlgeschlagen: {}".format(exc))

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def busy(self) -> bool:
        return self._state in (State.THINKING, State.SPEAKING)

    @property
    def muted(self) -> bool:
        return self._muted

    @muted.setter
    def muted(self, value: bool) -> None:
        self._muted = bool(value)
        if self._muted:
            self._mute_event.set()
        else:
            self._mute_event.clear()

    @property
    def level(self) -> float:
        """Mikrofonpegel 0..1 (für die Pegelanzeige)."""
        return self._level

    @property
    def timings(self) -> Dict[str, Optional[float]]:
        return dict(self._timings)

    @property
    def speak_enabled(self) -> bool:
        return self._speak_enabled

    @speak_enabled.setter
    def speak_enabled(self, value: bool) -> None:
        self._speak_enabled = bool(value)
        if not self._speak_enabled:
            self._stop_thinking()

    def start(self) -> None:
        if self.running:
            return
        try:
            self._lock.acquire()
        except AlreadyRunningError as exc:
            self._emit(str(exc))
            self._set_state(State.ERROR)
            return
        self._stop.clear()
        self._cancel.clear()
        self._thread = threading.Thread(target=self._run, name="voice-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # Auch das Abbruchsignal setzen: ask()/play_wav() reagieren darauf sofort.
        self._cancel.set()
        self._stop.set()
        narrator = self._narrator
        if narrator is not None:
            narrator.stop()
        self._stop_thinking()
        client = self._client
        session_id = self._session_id
        if client is not None and session_id is not None:
            client.interrupt(session_id)
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001 – ohne Audio trotzdem beenden
            pass
        if self._thread is not None:
            self._thread.join(timeout=10)
        self._lock.release()
        self._set_state(State.OFF)

    def cancel(self) -> None:
        """Bricht die laufende Aufgabe ab und bleibt im Zuhören.

        Wird aus einem Worker-Thread aufgerufen (nie aus dem UI-Thread), weil
        ``interrupt`` und ``narrator.stop`` Netzwerk bzw. Joins enthalten.
        """
        if not self.busy:
            return
        # Signal zuerst: Wiedergabe und Warteschleife reagieren sofort.
        self._cancel.set()
        try:
            self._stop_thinking()
        except Exception:  # noqa: BLE001 – Ton ist optional
            pass
        try:
            import sounddevice as sd

            sd.stop()
        except Exception:  # noqa: BLE001 – ohne Audio trotzdem abbrechen
            pass
        narrator = self._narrator
        if narrator is not None:
            try:
                narrator.stop()
            except Exception:  # noqa: BLE001
                pass
        client = self._client
        if client is not None and self._session_id is not None:
            try:
                client.interrupt(self._session_id)
            except Exception:  # noqa: BLE001 – Abbruch bleibt bestehen
                pass

    def send(self) -> None:
        """Beendet die laufende Aufnahme sofort und schickt sie ab."""
        if self._state != State.LISTENING:
            return
        self._send.set()

    def toggle(self) -> None:
        if self.running:
            self.stop()
        else:
            self.start()

    def run_forever(self) -> None:
        """Blockiert bis Ctrl-C, Stopp oder Fehler."""
        self.start()
        try:
            while self.running:
                time.sleep(0.2)
        except KeyboardInterrupt:
            self.stop()

    # intern

    def _emit(self, message: str) -> None:
        self._on_event(message)

    def _set_state(self, state: State) -> None:
        self._state = state
        self._on_state(state)

    def _set_level(self, rms: float) -> None:
        if rms <= 1.0:
            self._level = 0.0
            return
        decibels = 20.0 * math.log10(rms / 32768.0)
        self._level = max(0.0, min(1.0, (decibels + 60.0) / 60.0))

    def transcript(self) -> List[Tuple[float, str, str]]:
        with self._transcript_lock:
            return list(self._transcript)

    def _record(self, role: str, text: str) -> None:
        if not text:
            return
        entry = (time.time(), role, text)
        with self._transcript_lock:
            self._transcript.append(entry)
            if len(self._transcript) > 200:
                del self._transcript[:-200]
        try:
            self._on_transcript(role, text)
        except Exception:  # noqa: BLE001 – Anzeige ist optional
            pass

    def _narrate(self, text: str, stop_event: threading.Event) -> None:
        """Kurze Fortschritts-Ansage (ohne Zustandswechsel)."""
        if stop_event.is_set() or not self._speak_enabled:
            return
        self._record("info", text)
        self._emit("… {}".format(text))
        try:
            wav = tts.synthesize(self._cfg, text)
        except tts.TtsError:
            return
        tts.play_wav(
            self._cfg,
            wav,
            stop_event=stop_event,
            monitor=not self._muted,
            mute_event=self._mute_event,
        )

    def _start_thinking(self) -> None:
        """Startet die leise Klangschleife während des Denkens."""
        if not self._speak_enabled:
            return
        try:
            if not self._thinking.running:
                self._emit("♪ Denk-Schleife an {}".format(time.strftime("%H:%M:%S")))
            self._thinking.start()
        except Exception:  # noqa: BLE001 – Ton ist optional
            pass

    def _stop_thinking(self) -> None:
        """Stoppt die Klangschleife (vor Ansagen, Antworten oder Stopp)."""
        try:
            if self._thinking.running:
                self._emit("♪ Denk-Schleife aus {}".format(time.strftime("%H:%M:%S")))
            self._thinking.stop()
        except Exception:  # noqa: BLE001
            pass

    def _remember(self) -> None:
        if self._session_id is None:
            return
        try:
            state.save(
                state.SessionState(
                    session_id=self._session_id,
                    directory=str(self._cfg.session_dir),
                    agent=self._cfg.agent,
                    updated_at=time.time(),
                ),
                self._state_path,
            )
        except OSError as exc:
            self._emit("Sitzung konnte nicht gespeichert werden: {}".format(exc))

    def _acquire_session(self, client: OpenCodeClient) -> None:
        if self._session_id is not None:
            return

        if self._resume:
            stored = state.load(self._state_path)
            if (
                stored is not None
                and Path(stored.directory) == self._cfg.session_dir
                and stored.agent == self._cfg.agent
                and client.session_exists(stored.session_id)
            ):
                self._session_id = stored.session_id
                self._emit("Session fortgesetzt: {}".format(self._session_id))
                return

        self._session_id = client.create_session(model=self._model)
        self._emit("Session: {}".format(self._session_id))
        self._remember()

    def _speak(self, text: str) -> None:
        if self._stop.is_set():
            return
        self._set_state(State.SPEAKING)
        self._emit(("🔊 " if self._speak_enabled else "💬 ") + text)
        if not self._speak_enabled:
            self._set_state(State.LISTENING)
            return

        try:
            started = time.monotonic()
            wav = tts.synthesize(self._cfg, text)
            self._timings["tts"] = time.monotonic() - started
        except tts.TtsError as exc:
            self._emit("TTS-Fehler: {}".format(exc))
            self._set_state(State.LISTENING)
            return

        if self._stop.is_set():
            return

        if not tts.play_wav(
            self._cfg,
            wav,
            stop_event=self._cancel,
            monitor=not self._muted,
            mute_event=self._mute_event,
        ):
            self._emit("Wiedergabe unterbrochen.")
        # Kurz warten, damit der Echo-Ausklang nicht als Sprache aufgenommen wird.
        self._settle(0.25)
        self._set_state(State.LISTENING)

    def _settle(self, seconds: float) -> None:
        """Pause nach der Wiedergabe; bricht bei Stopp/Abbruch sofort ab."""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self._stop.is_set() or self._cancel.is_set():
                return
            time.sleep(0.02)

    def _run(self) -> None:
        cfg = self._cfg
        try:
            if not self._warm:
                self._emit("STT wird aufgewärmt (erster Start kann ~2 min dauern) …")
                started = time.monotonic()
                stt.warmup(cfg)
                self._emit("STT bereit nach {:.1f} s.".format(time.monotonic() - started))
                self._warm = True

            client = OpenCodeClient(cfg)
            self._client = client
            self._emit("OpenCode: {}".format(client.endpoint.url))
            self._acquire_session(client)

            while not self._stop.is_set():
                self._cancel.clear()
                self._send.clear()
                if self._muted:
                    self._set_state(State.MUTED)
                    self._stop.wait(0.2)
                    continue
                self._set_state(State.LISTENING)
                self._emit("🎤 hört zu …")
                utterance = audio.record_utterance(
                    cfg,
                    stop_event=self._stop,
                    send_event=self._send,
                    mute_event=self._mute_event,
                    min_frame_rms=cfg.record_min_rms,
                    on_level=self._set_level,
                )
                self._set_level(0.0)
                if self._stop.is_set():
                    break
                if self._muted:
                    continue
                if utterance is None:
                    self._emit("Keine Sprache erkannt.")
                    continue

                self._emit(
                    "Audio: {:.1f} s ({:.1f} s Sprache)".format(
                        utterance.duration_s, utterance.speech_s
                    )
                )
                if utterance.capped:
                    self._emit(
                        "Hinweis: {:.0f}-s-Grenze erreicht – der Rest wurde abgeschnitten.".format(
                            cfg.max_utterance_s
                        )
                    )

                self._set_state(State.TRANSCRIBING)
                started = time.monotonic()
                text = stt.transcribe(cfg, utterance.to_wav(cfg.sample_rate))
                self._timings["stt"] = time.monotonic() - started
                self._emit(
                    "📝 {} ({:.2f} s STT)".format(
                        text or "(leer)", self._timings["stt"] or 0.0
                    )
                )
                if not text:
                    self._speak("Ich habe nichts verstanden.")
                    continue

                self._record("user", text)
                self._set_state(State.THINKING)
                started = time.monotonic()
                narrator: Optional[events.Narrator] = None
                if cfg.progress and self._speak_enabled:
                    narrator = events.Narrator(
                        cfg,
                        client,
                        self._session_id,
                        self._narrate,
                        signal_start=self._start_thinking,
                        signal_stop=self._stop_thinking,
                    )
                    self._narrator = narrator
                    narrator.start()
                    narrator.wait_ready(1.0)
                try:
                    result = client.ask(text, self._session_id, cancel_event=self._cancel)
                finally:
                    if narrator is not None:
                        narrator.stop()
                        self._narrator = None

                self._timings["llm"] = time.monotonic() - started
                cancelled = self._cancel.is_set()
                interrupted = result.outcome == "interrupted"
                self._cancel.clear()
                self._session_id = result.session_id
                self._remember()

                # Stopp: leise beenden, keine Fehlermeldung mehr vorlesen.
                if self._stop.is_set():
                    break

                if cancelled or interrupted:
                    self._emit("⏹ Abgebrochen.")
                    self._record("info", "Abgebrochen.")
                    continue

                self._emit(
                    "🤔 {} ({:.1f} s)".format(result.outcome, self._timings["llm"] or 0.0)
                )
                if result.outcome != "succeeded":
                    if not self._stop.is_set():
                        self._speak("Die Anfrage ist fehlgeschlagen.")
                    continue

                if result.text:
                    self._record("assistant", result.text)

                spoken = speech.to_speech(result.text, max_chars=cfg.tts_max_chars)
                if not spoken:
                    self._speak("Die Antwort war leer.")
                    continue
                self._speak(spoken)
        except stt.SttError as exc:
            self._emit("STT-Fehler: {}".format(exc))
            self._set_state(State.ERROR)
        except OpenCodeError as exc:
            self._emit("OpenCode-Fehler: {}".format(exc))
            self._set_state(State.ERROR)
        except Exception as exc:  # noqa: BLE001 – letzte Sicherung
            self._emit("Unerwarteter Fehler: {}".format(exc))
            self._set_state(State.ERROR)
        finally:
            self._stop_thinking()
            self._lock.release()
            if self._state not in (State.OFF, State.ERROR):
                self._set_state(State.OFF)
