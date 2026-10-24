"""Integrationstest der Zustandsmaschine (ohne Audio und Netzwerk)."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from voicebridge import audio, state
from voicebridge.config import Config
from voicebridge.loop import State, VoiceLoop
from voicebridge.opencode_client import AskResult

BASIS = (
    "VOICE_STT_URL=http://h:8792/v1\n"
    "VOICE_TTS_URL=http://h:8791/v1\n"
    "VOICE_API_KEY=test-key-1234\n"
    "VOICE_OPENCODE_POLL_MS=50\n"
)


def make_config(tmp: str) -> Config:
    path = Path(tmp) / ".env"
    path.write_text(BASIS, encoding="utf-8")
    return Config.load(path)


class FakeClient:
    def __init__(self, cfg) -> None:
        self.endpoint = SimpleNamespace(url="http://test")

    def create_session(self, title: str = "Voice", model=None) -> str:
        return "ses_1"

    def ask(self, prompt: str, session_id=None, cancel_event=None) -> AskResult:
        return AskResult(
            session_id="ses_1", text="Hallo Welt!", outcome="succeeded", duration_s=0.01
        )

    def interrupt(self, session_id: str) -> bool:
        return True


class FakeNarrator:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def start(self) -> None:
        pass

    def wait_ready(self, timeout: float = 1.0) -> bool:
        return True

    def stop(self) -> None:
        pass


def blocking_record(cfg, idle_timeout_s=None, stop_event=None, send_event=None, mute_event=None, on_level=None):
    while stop_event is not None and not stop_event.is_set():
        time.sleep(0.01)
    return None


class LoopTests(unittest.TestCase):
    def test_durchlauf_bis_stopp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            utterance = audio.Collected(
                pcm=b"\x00\x00" * 160, duration_s=0.02, speech_s=0.02, capped=True
            )
            calls = {"record": 0}

            def fake_record(
                cfg, idle_timeout_s=None, stop_event=None, send_event=None,
                mute_event=None, min_frame_rms=None, on_level=None,
            ):
                calls["record"] += 1
                if calls["record"] == 1:
                    return utterance
                return blocking_record(cfg, idle_timeout_s, stop_event)

            events = []
            states = []

            with mock.patch("voicebridge.events.Narrator", FakeNarrator), mock.patch(
                "voicebridge.loop.stt.warmup"
            ), mock.patch(
                "voicebridge.loop.audio.record_utterance", side_effect=fake_record
            ), mock.patch(
                "voicebridge.loop.stt.transcribe", return_value="hallo welt"
            ), mock.patch(
                "voicebridge.loop.tts.synthesize", return_value=b"RIFF"
            ), mock.patch(
                "voicebridge.loop.tts.play_wav", return_value=True
            ), mock.patch(
                "voicebridge.loop.OpenCodeClient", FakeClient
            ):
                loop = VoiceLoop(
                    cfg,
                    on_event=events.append,
                    on_state=states.append,
                    resume=False,
                    state_path=Path(tmp) / "session.json",
                    lock_path=Path(tmp) / "loop.lock",
                )
                loop.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not any(
                    "Hallo Welt!" in event for event in events
                ):
                    time.sleep(0.02)
                loop.stop()

        self.assertIn(State.LISTENING, states)
        self.assertIn(State.TRANSCRIBING, states)
        self.assertIn(State.THINKING, states)
        self.assertIn(State.SPEAKING, states)
        self.assertIn(State.OFF, states)
        self.assertTrue(any("Hinweis: 25-s-Grenze" in event for event in events))
        self.assertTrue(any("📝 hallo welt" in event for event in events))
        self.assertTrue(any("🔊 Hallo Welt!" in event for event in events))
        self.assertFalse(loop.running)
        self.assertEqual(loop.session_id, "ses_1")

        entries = [(role, text) for _stamp, role, text in loop.transcript()]
        self.assertIn(("user", "hallo welt"), entries)
        self.assertIn(("assistant", "Hallo Welt!"), entries)

    def test_gespeicherte_session_wird_fortgesetzt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            state_path = Path(tmp) / "session.json"
            state.save(
                state.SessionState("ses_old", str(cfg.session_dir), cfg.agent, time.time()),
                state_path,
            )

            class ResumingClient:
                def __init__(self, cfg) -> None:
                    self.endpoint = SimpleNamespace(url="http://test")

                def session_exists(self, session_id: str) -> bool:
                    return session_id == "ses_old"

                def create_session(self, title: str = "Voice", model=None) -> str:
                    raise AssertionError("hätte nicht neu anlegen dürfen")

                def interrupt(self, session_id: str) -> bool:
                    return True

            with mock.patch("voicebridge.events.Narrator", FakeNarrator), mock.patch(
                "voicebridge.loop.stt.warmup"
            ), mock.patch(
                "voicebridge.loop.audio.record_utterance", side_effect=blocking_record
            ), mock.patch(
                "voicebridge.loop.OpenCodeClient", ResumingClient
            ):
                loop = VoiceLoop(
                    cfg,
                    resume=True,
                    state_path=state_path,
                    lock_path=Path(tmp) / "loop.lock",
                )
                loop.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and loop.session_id != "ses_old":
                    time.sleep(0.02)
                loop.stop()

        self.assertEqual(loop.session_id, "ses_old")


    def test_zweite_instanz_wird_abgelehnt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            lock_path = Path(tmp) / "loop.lock"
            events = []
            states = []

            with mock.patch("voicebridge.events.Narrator", FakeNarrator), mock.patch(
                "voicebridge.loop.stt.warmup"
            ), mock.patch(
                "voicebridge.loop.audio.record_utterance", side_effect=blocking_record
            ), mock.patch(
                "voicebridge.loop.OpenCodeClient", FakeClient
            ):
                first = VoiceLoop(
                    cfg,
                    resume=False,
                    state_path=Path(tmp) / "first.json",
                    lock_path=lock_path,
                )
                first.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not first.running:
                    time.sleep(0.02)

                second = VoiceLoop(
                    cfg,
                    on_event=events.append,
                    on_state=states.append,
                    resume=False,
                    state_path=Path(tmp) / "second.json",
                    lock_path=lock_path,
                )
                second.start()

                self.assertFalse(second.running)
                first.stop()
                second.stop()

        self.assertTrue(any("bereits" in event for event in events))
        self.assertIn(State.ERROR, states)


    def test_cancel_bricht_die_aufgabe_ab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            utterance = audio.Collected(
                pcm=b"\x00\x00" * 160, duration_s=0.02, speech_s=0.02, capped=False
            )
            calls = {"record": 0}

            def fake_record(
                cfg, idle_timeout_s=None, stop_event=None, send_event=None,
                mute_event=None, min_frame_rms=None, on_level=None,
            ):
                calls["record"] += 1
                if calls["record"] == 1:
                    return utterance
                return blocking_record(cfg, idle_timeout_s, stop_event)

            class CancelClient(FakeClient):
                def ask(self, prompt, session_id=None, cancel_event=None):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if cancel_event is not None and cancel_event.is_set():
                            return AskResult("ses_1", "", "interrupted", 0.01)
                        time.sleep(0.01)
                    raise AssertionError("cancel_event wurde nie gesetzt")

            events = []
            with mock.patch("voicebridge.events.Narrator", FakeNarrator), mock.patch(
                "voicebridge.loop.stt.warmup"
            ), mock.patch(
                "voicebridge.loop.audio.record_utterance", side_effect=fake_record
            ), mock.patch(
                "voicebridge.loop.stt.transcribe", return_value="hallo welt"
            ), mock.patch(
                "voicebridge.loop.tts.synthesize", return_value=b"RIFF"
            ), mock.patch(
                "voicebridge.loop.tts.play_wav", return_value=True
            ), mock.patch(
                "voicebridge.loop.OpenCodeClient", CancelClient
            ):
                loop = VoiceLoop(
                    cfg,
                    on_event=events.append,
                    resume=False,
                    state_path=Path(tmp) / "session.json",
                    lock_path=Path(tmp) / "loop.lock",
                )
                loop.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not loop.busy:
                    time.sleep(0.02)
                self.assertTrue(loop.busy)
                loop.cancel()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not any(
                    "Abgebrochen" in event for event in events
                ):
                    time.sleep(0.02)
                loop.stop()

        self.assertTrue(any("⏹ Abgebrochen." in event for event in events))


    def test_stopp_waehrend_denken_meldet_keinen_fehler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            utterance = audio.Collected(
                pcm=b"\x00\x00" * 160, duration_s=0.02, speech_s=0.02, capped=False
            )
            calls = {"record": 0}

            def fake_record(
                cfg, idle_timeout_s=None, stop_event=None, send_event=None,
                mute_event=None, min_frame_rms=None, on_level=None,
            ):
                calls["record"] += 1
                if calls["record"] == 1:
                    return utterance
                return blocking_record(cfg, idle_timeout_s, stop_event)

            class InterruptedClient(FakeClient):
                def ask(self, prompt, session_id=None, cancel_event=None):
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if cancel_event is not None and cancel_event.is_set():
                            return AskResult("ses_1", "", "interrupted", 0.01)
                        time.sleep(0.01)
                    raise AssertionError("cancel_event wurde nie gesetzt")

            events = []
            with mock.patch("voicebridge.events.Narrator", FakeNarrator), mock.patch(
                "voicebridge.loop.stt.warmup"
            ), mock.patch(
                "voicebridge.loop.audio.record_utterance", side_effect=fake_record
            ), mock.patch(
                "voicebridge.loop.stt.transcribe", return_value="hallo welt"
            ), mock.patch(
                "voicebridge.loop.tts.synthesize", return_value=b"RIFF"
            ), mock.patch(
                "voicebridge.loop.tts.play_wav", return_value=True
            ), mock.patch(
                "voicebridge.loop.OpenCodeClient", InterruptedClient
            ):
                loop = VoiceLoop(
                    cfg,
                    on_event=events.append,
                    resume=False,
                    state_path=Path(tmp) / "session.json",
                    lock_path=Path(tmp) / "loop.lock",
                )
                loop.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not loop.busy:
                    time.sleep(0.02)
                self.assertTrue(loop.busy)
                loop.stop()

        self.assertFalse(loop.running)
        self.assertFalse(any("fehlgeschlagen" in event for event in events))


    def test_senden_schickt_die_aufnahme_sofort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            utterance = audio.Collected(
                pcm=b"\x00\x00" * 160, duration_s=0.02, speech_s=0.02, capped=False
            )
            sent = {"value": False}

            def fake_record(
                cfg, idle_timeout_s=None, stop_event=None, send_event=None,
                mute_event=None, min_frame_rms=None, on_level=None,
            ):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if send_event is not None and send_event.is_set():
                        sent["value"] = True
                        return utterance
                    if stop_event is not None and stop_event.is_set():
                        return None
                    time.sleep(0.01)
                return None

            events = []
            with mock.patch("voicebridge.events.Narrator", FakeNarrator), mock.patch(
                "voicebridge.loop.stt.warmup"
            ), mock.patch(
                "voicebridge.loop.audio.record_utterance", side_effect=fake_record
            ), mock.patch(
                "voicebridge.loop.stt.transcribe", return_value="hallo welt"
            ), mock.patch(
                "voicebridge.loop.tts.synthesize", return_value=b"RIFF"
            ), mock.patch(
                "voicebridge.loop.tts.play_wav", return_value=True
            ), mock.patch(
                "voicebridge.loop.OpenCodeClient", FakeClient
            ):
                loop = VoiceLoop(
                    cfg,
                    on_event=events.append,
                    resume=False,
                    state_path=Path(tmp) / "session.json",
                    lock_path=Path(tmp) / "loop.lock",
                )
                loop.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and loop.state != State.LISTENING:
                    time.sleep(0.02)
                loop.send()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not any(
                    "📝" in event for event in events
                ):
                    time.sleep(0.02)
                loop.stop()

        self.assertTrue(sent["value"])
        self.assertTrue(any("📝 hallo welt" in event for event in events))


    def test_mikrofon_stumm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(tmp)
            calls = {"record": 0}

            def fake_record(
                cfg, idle_timeout_s=None, stop_event=None, send_event=None,
                mute_event=None, min_frame_rms=None, on_level=None,
            ):
                calls["record"] += 1
                while stop_event is not None and not stop_event.is_set():
                    if mute_event is not None and mute_event.is_set():
                        return None
                    time.sleep(0.01)
                return None

            with mock.patch("voicebridge.events.Narrator", FakeNarrator), mock.patch(
                "voicebridge.loop.stt.warmup"
            ), mock.patch(
                "voicebridge.loop.audio.record_utterance", side_effect=fake_record
            ), mock.patch(
                "voicebridge.loop.OpenCodeClient", FakeClient
            ):
                loop = VoiceLoop(
                    cfg,
                    resume=False,
                    state_path=Path(tmp) / "session.json",
                    lock_path=Path(tmp) / "loop.lock",
                )
                loop.start()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and loop.state != State.LISTENING:
                    time.sleep(0.02)

                loop.muted = True
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and loop.state != State.MUTED:
                    time.sleep(0.02)
                self.assertEqual(loop.state, State.MUTED)

                loop.muted = False
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and loop.state != State.LISTENING:
                    time.sleep(0.02)
                loop.stop()

        self.assertGreaterEqual(calls["record"], 1)


if __name__ == "__main__":
    unittest.main()
