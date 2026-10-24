"""Speech-to-Text-Client (vLLM, Whisper-large-v3-turbo).

Vertrag (verifiziert 2026-10-24):
- ``POST {VOICE_STT_URL}/audio/transcriptions`` als multipart/form-data
- Felder: ``file`` (WAV), ``model``, ``language``, optional ``prompt``
- Antwort: ``{"text": " …"}`` – führendes Leerzeichen wird entfernt
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import requests

from .audio import to_wav
from .config import Config

WARMUP_TIMEOUT_S = 180.0


class SttError(RuntimeError):
    """STT nicht erreichbar, abgelehnt oder unbrauchbar."""


def silence_wav(sample_rate: int, seconds: float = 1.0) -> bytes:
    """Erzeugt Stille als WAV (für den Warmup-Call)."""
    return to_wav(b"\x00\x00" * int(sample_rate * seconds), sample_rate)


def _headers(cfg: Config) -> Dict[str, str]:
    return {"Authorization": "Bearer " + cfg.api_key}


def _post(
    cfg: Config, wav: bytes, timeout: Tuple[float, float]
) -> requests.Response:
    data: Dict[str, str] = {"model": cfg.stt_model}
    if cfg.stt_language:
        data["language"] = cfg.stt_language
    if cfg.stt_prompt:
        data["prompt"] = cfg.stt_prompt
    try:
        return requests.post(
            cfg.stt_endpoint,
            headers=_headers(cfg),
            files={"file": ("audio.wav", wav, "audio/wav")},
            data=data,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SttError("STT nicht erreichbar ({}): {}".format(cfg.stt_endpoint, exc)) from exc


def _json(response: requests.Response) -> Dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise SttError("STT lieferte kein gültiges JSON.") from exc
    if not isinstance(payload, dict):
        raise SttError("STT lieferte unerwartetes JSON.")
    return payload


def _raise_for_status(response: requests.Response) -> None:
    if response.status_code == 200:
        return
    detail = (response.text or "").strip()
    if len(detail) > 300:
        detail = detail[:300] + "…"
    raise SttError("STT antwortete HTTP {}: {}".format(response.status_code, detail))


def transcribe(cfg: Config, wav: bytes) -> str:
    """Sendet WAV-Bytes und liefert den erkannten Text (getrimmt)."""
    response = _post(cfg, wav, (10.0, cfg.request_timeout_s))
    _raise_for_status(response)
    payload = _json(response)
    text: Optional[str] = payload.get("text")
    return (text or "").strip()


def warmup(cfg: Config) -> None:
    """Erster Request nach Containerstart kann ~2 Minuten dauern."""
    response = _post(cfg, silence_wav(cfg.sample_rate), (10.0, WARMUP_TIMEOUT_S))
    _raise_for_status(response)
