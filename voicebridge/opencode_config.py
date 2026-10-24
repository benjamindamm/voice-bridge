"""Schaltet MCP-Server in der OpenCode-Konfiguration ein und aus.

Die Datei wird atomar geschrieben und vor der ersten Änderung gesichert.
Unterstützt das V1-Format (``mcp.<name>.enabled``) und das V2-Format
(``mcp.servers.<name>.disabled``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_CONFIG = Path("~/.config/opencode/opencode.json").expanduser()


class ConfigEditError(RuntimeError):
    """Config nicht lesbar, unerwartet aufgebaut oder Server unbekannt."""


def _read(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigEditError("Config nicht lesbar: {}".format(exc)) from exc
    except ValueError as exc:
        raise ConfigEditError("Config ist kein gültiges JSON: {}".format(exc)) from exc
    if not isinstance(payload, dict):
        raise ConfigEditError("Config hat ein unerwartetes Format.")
    return payload


def _servers(payload: Dict[str, Any]):
    mcp = payload.get("mcp")
    if not isinstance(mcp, dict):
        raise ConfigEditError("Keine mcp-Sektion in der Config.")
    if isinstance(mcp.get("servers"), dict):
        return mcp, mcp["servers"], True
    return mcp, mcp, False


def set_mcp_enabled(name: str, enabled: bool, path: Optional[Path] = None) -> None:
    """Setzt ``disabled`` (V2) bzw. ``enabled`` (V1) für einen MCP-Server."""
    target = Path(path).expanduser() if path is not None else DEFAULT_CONFIG
    payload = _read(target)
    _mcp, servers, v2 = _servers(payload)
    entry = servers.get(name)
    if not isinstance(entry, dict):
        raise ConfigEditError(
            "MCP-Server {!r} steht nicht in der Config.".format(name)
        )

    if v2:
        entry["disabled"] = not enabled
    else:
        entry["enabled"] = enabled

    backup = target.with_name(target.name + ".bak-voicebridge")
    if not backup.exists():
        backup.write_bytes(target.read_bytes())

    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(target)
