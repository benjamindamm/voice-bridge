"""Einstiegspunkt: ``python -m voicebridge`` mit Prüf- und Testbefehlen."""

from __future__ import annotations

import argparse
import importlib
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import audio, sounds, speech, state, stt, tts
from .config import Config, ConfigError
from .opencode_client import OpenCodeClient, OpenCodeError
from .loop import State, VoiceLoop

DEPENDENCIES = ("sounddevice", "webrtcvad", "numpy", "requests")


def _print_config(cfg: Config) -> None:
    print("Konfiguration")
    status = "" if cfg.env_path.is_file() else "  (fehlt)"
    print("  .env                {}{}".format(cfg.env_path, status))
    print("  STT-Endpunkt        {}".format(cfg.stt_endpoint))
    print("  STT-Modell          {} (Sprache {})".format(cfg.stt_model, cfg.stt_language))
    print("  TTS-Endpunkt        {}".format(cfg.tts_endpoint))
    print("  TTS-Modell          {}".format(cfg.tts_model))
    print("  TTS-Stimme          {} (Tempo {})".format(cfg.tts_voice, cfg.tts_speed))
    print(
        "  Ausgabe             max. {} Zeichen, Echo-Sperre {}".format(
            cfg.tts_max_chars, cfg.echo_guard
        )
    )
    print(
        "  Fortschritt         {} (alle {:.1f} s)".format(
            "an" if cfg.progress else "aus", cfg.progress_interval_s
        )
    )
    print("  Ausgabe-Weg         {}".format(cfg.player))
    print(
        "  Barge-in            Faktor {:.1f}, {} ms, VAD {}, Grundpegel {:.0f}".format(
            cfg.barge_factor, cfg.barge_ms, cfg.barge_vad, cfg.barge_min_rms
        )
    )
    print(
        "  Sprachausgabe       {} (Signal {:.2f})".format(
            "an" if cfg.speak else "aus", cfg.signal_volume
        )
    )
    print(
        "  Startverhalten      {}".format(
            "letzte Sitzung fortsetzen" if state.load_settings().resume else "immer neu"
        )
    )
    print("  OpenCode            {}".format(cfg.opencode_url or "(automatisch)"))
    print("  Agent               {}".format(cfg.agent))
    print("  Arbeitsordner       {}".format(cfg.session_dir))
    print("  Modell              {}".format(cfg.model or "(global)"))
    print(
        "  Modell-Provider     {}".format(
            ", ".join(cfg.model_providers) or "(alle verbundenen)"
        )
    )
    print("  API-Key             {}".format(cfg.masked_api_key))
    print(
        "  Audio               {} Hz, Frame {} ms, VAD {}".format(
            cfg.sample_rate, cfg.frame_ms, cfg.vad_aggressiveness
        )
    )
    print(
        "  VAD                 Vorlauf {} ms, Stille {} ms, min. Sprache {} ms, max. {} s".format(
            cfg.pre_roll_ms, cfg.silence_ms, cfg.min_speech_ms, cfg.max_utterance_s
        )
    )


def ensure_session_dir(cfg: Config) -> bool:
    """Legt den Arbeitsordner an, falls er fehlt. ``True`` = neu angelegt."""
    if cfg.session_dir.is_dir():
        return False
    cfg.session_dir.mkdir(parents=True, exist_ok=True)
    return True


def cmd_check(cfg: Config) -> int:
    _print_config(cfg)
    problems: List[str] = []

    print()
    print("Umgebung")
    print("  Python              {}".format(platform.python_version()))
    print("  Interpreter         {}".format(sys.executable))

    print()
    print("Abhängigkeiten")
    for name in DEPENDENCIES:
        try:
            module = importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 – jede Importstörung ist relevant
            problems.append("{} nicht importierbar: {}".format(name, exc))
            print("  {:<12}FEHLT".format(name))
            continue
        version = getattr(module, "__version__", "installiert")
        print("  {:<12}{}".format(name, version))

    ffmpeg = shutil.which("ffmpeg")
    print("  {:<12}{}".format("ffmpeg", ffmpeg or "FEHLT (Homebrew: brew install ffmpeg)"))
    if not ffmpeg:
        problems.append("ffmpeg nicht im PATH (für MP3-Wiedergabe nötig).")

    try:
        created = ensure_session_dir(cfg)
    except OSError as exc:
        problems.append(
            "Arbeitsordner {} lässt sich nicht anlegen: {}".format(cfg.session_dir, exc)
        )
    else:
        if created:
            print("Arbeitsordner {} neu angelegt.".format(cfg.session_dir))

    print()
    if problems:
        print("Befunde mit Handlungsbedarf:")
        for problem in problems:
            print("  - {}".format(problem))
        return 1
    print("Alles bereit.")
    return 0


def cmd_warmup(cfg: Config) -> int:
    print("Warmup an {} (erster Request kann bis ~2 min dauern) …".format(cfg.stt_endpoint))
    started = time.monotonic()
    try:
        stt.warmup(cfg)
    except stt.SttError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Warmup ok nach {:.1f} s.".format(time.monotonic() - started))
    return 0


def cmd_stt(cfg: Config, path: Path) -> int:
    if not path.is_file():
        print("Datei nicht gefunden: {}".format(path), file=sys.stderr)
        return 1
    print("STT für {} …".format(path))
    try:
        text = stt.transcribe(cfg, path.read_bytes())
    except stt.SttError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not text:
        print("Kein Text erkannt.", file=sys.stderr)
        return 1
    print("📝 {}".format(text))
    return 0


def cmd_test_record(cfg: Config) -> int:
    print("Aufnahme – bitte sprechen.")
    print("(macOS fragt beim ersten Mal nach der Mikrofon-Freigabe für das Terminal.)")
    result = audio.record_utterance(cfg)
    if result is None:
        print("Keine Sprache erkannt.", file=sys.stderr)
        return 1

    wav = result.to_wav(cfg.sample_rate)
    target = Path("utterance-{}.wav".format(time.strftime("%Y%m%d-%H%M%S")))
    target.write_bytes(wav)
    print(
        "Aufnahme: {} ({:.2f} s, Sprache {:.2f} s)".format(
            target, result.duration_s, result.speech_s
        )
    )
    if result.capped:
        print(
            "Hinweis: Grenze von {:.0f} s erreicht – Whisper hätte den Rest abgeschnitten.".format(
                cfg.max_utterance_s
            )
        )

    print("STT …")
    try:
        text = stt.transcribe(cfg, wav)
    except stt.SttError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if text:
        print("📝 {}".format(text))
        return 0
    print("Kein Text erkannt.", file=sys.stderr)
    return 1


def cmd_ask(
    cfg: Config, text: str, session_id: Optional[str], model: Optional[str] = None
) -> int:
    client = OpenCodeClient(cfg)
    try:
        print("Dienst:  {}".format(client.endpoint.url))
        result = client.ask(text, session_id, model=model)
    except OpenCodeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("Session: {}".format(result.session_id))
    print("Status:  {} ({:.1f} s)".format(result.outcome, result.duration_s))
    print("Antwort: {}".format(result.text or "(leer)"))
    return 0 if result.outcome == "succeeded" else 1


def cmd_run(
    cfg: Config,
    session_id: Optional[str],
    new_session: bool = False,
    model: Optional[str] = None,
) -> int:
    settings = state.load_settings()
    loop = VoiceLoop(
        cfg,
        on_event=lambda message: print(message, flush=True),
        session_id=session_id,
        model=model or settings.model or cfg.model or None,
        resume=(not new_session) and settings.resume,
    )
    print("Sprachsitzung – beenden mit Ctrl-C.")
    loop.run_forever()
    if loop.state == State.ERROR:
        print("Start abgebrochen.", file=sys.stderr)
        return 1
    print("Beendet.")
    return 0


def cmd_menubar(cfg: Config) -> int:
    ensure_session_dir(cfg)
    try:
        from .menubar import run
    except ImportError as exc:
        print("Menüleiste nicht verfügbar: {}".format(exc), file=sys.stderr)
        print("Installieren mit: ~/voice-venv/bin/pip install rumps", file=sys.stderr)
        return 1
    run(cfg)
    return 0


def cmd_test_tts(cfg: Config, text: str, player: Optional[str] = None) -> int:
    spoken = speech.to_speech(text, max_chars=cfg.tts_max_chars)
    if spoken != text.strip():
        print("Sprechfassung: {}".format(spoken))

    started = time.monotonic()
    try:
        wav = tts.synthesize(cfg, spoken)
    except tts.TtsError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    rate, channels, _width, frames = tts.wav_info(wav)
    seconds = len(frames) / 2 / channels / rate
    print(
        "TTS: {:.2f} s, {} Hz, {} Kanal/Kanäle, {:.2f} s Audio".format(
            time.monotonic() - started, rate, channels, seconds
        )
    )

    print("Wiedergabe … (Sprechen bricht ab)")
    try:
        completed = tts.play_wav(cfg, wav, player=player)
    except tts.TtsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Wiedergabe beendet." if completed else "Wiedergabe abgebrochen.")
    return 0


def cmd_test_signal(cfg: Config) -> int:
    print("Denk-Ton 3 s (Lautstärke {:.2f}) …".format(cfg.signal_volume))
    loop = sounds.ThinkingLoop(volume=cfg.signal_volume)
    loop.start()
    time.sleep(3.0)
    loop.stop()
    print("Ende.")
    return 0


def cmd_play_wav(cfg: Config, path: Path, player: Optional[str]) -> int:
    if not path.is_file():
        print("Datei nicht gefunden: {}".format(path), file=sys.stderr)
        return 1
    print("Wiedergabe von {} ({}).".format(path, player or cfg.player))
    try:
        completed = tts.play_wav(cfg, path.read_bytes(), player=player)
    except tts.TtsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Wiedergabe beendet." if completed else "Wiedergabe abgebrochen.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="voicebridge",
        description="Voice-Bridge: Mikrofon → STT/TTS im LAN → OpenCode-Session.",
    )
    parser.add_argument("--env", type=Path, default=None, help="Pfad zur .env-Datei")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Konfiguration und Abhängigkeiten prüfen, dann beenden",
    )
    parser.add_argument(
        "--warmup",
        action="store_true",
        help="STT mit 1 s Stille aufwärmen (erster Request kann ~2 min dauern)",
    )
    parser.add_argument(
        "--stt",
        type=Path,
        metavar="WAV",
        help="Vorhandene WAV-Datei transkribieren",
    )
    parser.add_argument(
        "--test-record",
        action="store_true",
        help="Eine Äußerung aufnehmen, speichern und transkribieren",
    )
    parser.add_argument(
        "--ask",
        metavar="TEXT",
        help="Prompt an OpenCode senden und Antwort ausgeben",
    )
    parser.add_argument(
        "--session",
        metavar="ID",
        default=None,
        help="Bestehende Session verwenden (mit --ask)",
    )
    parser.add_argument(
        "--model",
        metavar="PROVIDER/MODEL",
        default=None,
        help="Modell für die Sitzung (z. B. treno/local:default)",
    )
    parser.add_argument(
        "--test-tts",
        metavar="TEXT",
        help="Text sprechen (Sprechfassung + Wiedergabe testen)",
    )
    parser.add_argument(
        "--play-wav",
        type=Path,
        metavar="FILE",
        help="Vorhandene WAV-Datei abspielen (Player-Test)",
    )
    parser.add_argument(
        "--test-signal",
        action="store_true",
        help="Denk-Ton 3 s abspielen (Lautstärke prüfen)",
    )
    parser.add_argument(
        "--player",
        choices=("afplay", "portaudio"),
        default=None,
        help="Wiedergabe-Weg für --test-tts/--play-wav",
    )
    parser.add_argument(
        "--menubar",
        action="store_true",
        help="Menüleisten-Widget starten (Start/Stopp)",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Sprachsitzung starten (zuhören → denken → sprechen, bis Ctrl-C)",
    )
    parser.add_argument(
        "--new-session",
        action="store_true",
        help="Gespeicherte Sitzung ignorieren und neu beginnen (mit --run)",
    )
    args = parser.parse_args(argv)

    try:
        cfg = Config.load(args.env)
    except ConfigError as exc:
        print("Konfiguration unvollständig:", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        return 1

    if args.check:
        return cmd_check(cfg)
    if args.warmup:
        return cmd_warmup(cfg)
    if args.stt is not None:
        return cmd_stt(cfg, args.stt)
    if args.test_record:
        return cmd_test_record(cfg)
    if args.play_wav is not None:
        return cmd_play_wav(cfg, args.play_wav, args.player)
    if args.test_signal:
        return cmd_test_signal(cfg)
    if args.test_tts is not None:
        return cmd_test_tts(cfg, args.test_tts, args.player)
    if args.menubar:
        return cmd_menubar(cfg)
    if args.run:
        return cmd_run(cfg, args.session, args.new_session, args.model)
    if args.ask is not None:
        return cmd_ask(cfg, args.ask, args.session, args.model)

    print(
        "Verfügbar: --check, --warmup, --stt DATEI, --test-record, --ask TEXT, "
        "--test-tts TEXT, --test-signal, --run, --menubar"
    )
    print("Konfiguration prüfen mit: python -m voicebridge --check")
    return 0


if __name__ == "__main__":
    sys.exit(main())
