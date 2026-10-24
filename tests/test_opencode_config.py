"""Tests für das Ein-/Ausschalten von MCP-Servern in der Config."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from voicebridge.opencode_config import ConfigEditError, set_mcp_enabled


class ConfigEditTests(unittest.TestCase):
    def test_v1_enabled_schalten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.json"
            path.write_text(
                json.dumps({"mcp": {"mermaid": {"type": "local", "enabled": True}}}),
                encoding="utf-8",
            )

            set_mcp_enabled("mermaid", False, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(data["mcp"]["mermaid"]["enabled"])
            self.assertTrue((Path(tmp) / "opencode.json.bak-voicebridge").is_file())

            set_mcp_enabled("mermaid", True, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(data["mcp"]["mermaid"]["enabled"])

    def test_v2_disabled_schalten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.json"
            path.write_text(
                json.dumps({"mcp": {"servers": {"mermaid": {"disabled": False}}}}),
                encoding="utf-8",
            )

            set_mcp_enabled("mermaid", False, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(data["mcp"]["servers"]["mermaid"]["disabled"])

            set_mcp_enabled("mermaid", True, path)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertFalse(data["mcp"]["servers"]["mermaid"]["disabled"])

    def test_unbekannter_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.json"
            path.write_text(json.dumps({"mcp": {"brave": {}}}), encoding="utf-8")
            with self.assertRaises(ConfigEditError):
                set_mcp_enabled("mermaid", True, path)

    def test_kaputtes_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "opencode.json"
            path.write_text("{kaputt", encoding="utf-8")
            with self.assertRaises(ConfigEditError):
                set_mcp_enabled("mermaid", True, path)


if __name__ == "__main__":
    unittest.main()
