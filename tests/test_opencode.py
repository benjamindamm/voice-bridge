"""Tests für den OpenCode-Client (ohne echten Dienst)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from voicebridge.config import Config
from voicebridge.opencode_client import Endpoint, OpenCodeClient, OpenCodeError

BASIS = (
    "VOICE_STT_URL=http://stt.test:8792/v1\n"
    "VOICE_TTS_URL=http://tts.test:8791/v1\n"
    "VOICE_API_KEY=test-key-1234\n"
    "VOICE_OPENCODE_POLL_MS=50\n"
)
BASE = "http://test"


def make_config(tmp: str, extra: str = "") -> Config:
    path = Path(tmp) / ".env"
    path.write_text(BASIS + extra, encoding="utf-8")
    return Config.load(path)


class FakeResponse:
    def __init__(self, status: int = 200, payload=None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("kein JSON")
        return self._payload


class Sequence:
    """Liefert bei jedem Aufruf den nächsten Eintrag (letzter wird wiederholt)."""

    def __init__(self, items) -> None:
        self.items = list(items)
        self.index = 0

    def __call__(self, **_kwargs) -> FakeResponse:
        item = self.items[min(self.index, len(self.items) - 1)]
        self.index += 1
        return item


class FakeHttp:
    def __init__(self, routes) -> None:
        self.routes = routes
        self.auth = None
        self.calls = []

    def _call(self, method: str, url: str, **kwargs) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        route = self.routes[(method, url)]
        return route(**kwargs) if callable(route) else route

    def get(self, url: str, **kwargs) -> FakeResponse:
        return self._call("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> FakeResponse:
        return self._call("POST", url, **kwargs)


def build(tmp: str, routes, extra: str = ""):
    cfg = make_config(tmp, extra)
    http = FakeHttp(routes)
    return OpenCodeClient(cfg, endpoint=Endpoint(BASE, "pw"), http=http), http, cfg


def session_call(http):
    return [call for call in http.calls if call[0] == "POST" and call[1] == BASE + "/api/session"][0]


class AskTests(unittest.TestCase):
    def test_ask_liefert_letzte_antwort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("GET", BASE + "/api/agent"): FakeResponse(200, {"data": [{"id": "build"}]}),
                ("POST", BASE + "/api/session"): FakeResponse(200, {"data": {"id": "ses_1"}}),
                ("POST", BASE + "/api/session/ses_1/prompt"): FakeResponse(
                    200, {"data": {"id": "msg_u", "type": "user", "time": {"created": 1000}}}
                ),
                ("GET", BASE + "/api/session/ses_1/message"): Sequence(
                    [
                        FakeResponse(
                            200,
                            {
                                "data": [
                                    {"id": "u", "type": "user", "time": {"created": 1000}, "text": "hi"}
                                ],
                                "cursor": {},
                            },
                        ),
                        FakeResponse(
                            200,
                            {
                                "data": [
                                    {"id": "i", "type": "idle", "time": {"created": 3000}, "outcome": "succeeded"},
                                    {
                                        "id": "a2",
                                        "type": "assistant",
                                        "time": {"created": 2000},
                                        "content": [{"type": "text", "text": "Zweite"}],
                                    },
                                    {
                                        "id": "a1",
                                        "type": "assistant",
                                        "time": {"created": 1500},
                                        "content": [{"type": "text", "text": "Erste"}],
                                    },
                                    {"id": "u", "type": "user", "time": {"created": 1000}, "text": "hi"},
                                ],
                                "cursor": {},
                            },
                        ),
                    ]
                ),
            }
            client, http, cfg = build(tmp, routes)
            result = client.ask("hi")

        self.assertEqual(result.text, "Zweite")
        self.assertEqual(result.outcome, "succeeded")
        self.assertEqual(result.session_id, "ses_1")
        self.assertEqual(http.auth, ("opencode", "pw"))
        payload = session_call(http)[2]["json"]
        self.assertEqual(payload["location"]["directory"], str(cfg.session_dir))
        self.assertNotIn("agent", payload)

    def test_vorhandener_agent_wird_gesetzt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("GET", BASE + "/api/agent"): FakeResponse(
                    200, {"data": [{"id": "voice-orchestrator"}]}
                ),
                ("POST", BASE + "/api/session"): FakeResponse(200, {"data": {"id": "ses_2"}}),
                ("POST", BASE + "/api/session/ses_2/prompt"): FakeResponse(
                    200, {"data": {"id": "u", "time": {"created": 1}}}
                ),
                ("GET", BASE + "/api/session/ses_2/message"): FakeResponse(
                    200,
                    {
                        "data": [
                            {"id": "i", "type": "idle", "time": {"created": 2}, "outcome": "succeeded"}
                        ],
                        "cursor": {},
                    },
                ),
            }
            client, http, _cfg = build(tmp, routes)
            client.ask("hi")

        self.assertEqual(session_call(http)[2]["json"]["agent"], "voice-orchestrator")

    def test_bestehende_session_wird_genutzt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("POST", BASE + "/api/session/ses_9/prompt"): FakeResponse(
                    200, {"data": {"id": "u", "time": {"created": 5}}}
                ),
                ("GET", BASE + "/api/session/ses_9/message"): FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "id": "a",
                                "type": "assistant",
                                "time": {"created": 6},
                                "content": [{"type": "text", "text": "ok"}],
                            },
                            {"id": "i", "type": "idle", "time": {"created": 7}, "outcome": "succeeded"},
                        ],
                        "cursor": {},
                    },
                ),
            }
            client, http, _cfg = build(tmp, routes)
            result = client.ask("weiter", session_id="ses_9")

        self.assertEqual(result.session_id, "ses_9")
        self.assertEqual(result.text, "ok")
        self.assertFalse(any(url.endswith("/api/session") for _m, url, _k in http.calls))

    def test_zeitueberschreitung(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("GET", BASE + "/api/agent"): FakeResponse(200, {"data": []}),
                ("POST", BASE + "/api/session"): FakeResponse(200, {"data": {"id": "ses_3"}}),
                ("POST", BASE + "/api/session/ses_3/prompt"): FakeResponse(
                    200, {"data": {"id": "u", "time": {"created": 1}}}
                ),
                ("GET", BASE + "/api/session/ses_3/message"): FakeResponse(200, {"data": [], "cursor": {}}),
            }
            client, _http, _cfg = build(tmp, routes, "VOICE_REQUEST_TIMEOUT_S=1\n")
            with self.assertRaises(OpenCodeError) as ctx:
                client.ask("hi")

        self.assertIn("Zeitüberschreitung", str(ctx.exception))

    def test_401_nennt_passwort_hinweis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("GET", BASE + "/api/agent"): FakeResponse(200, {"data": []}),
                ("POST", BASE + "/api/session"): FakeResponse(200, {"data": {"id": "ses_4"}}),
                ("POST", BASE + "/api/session/ses_4/prompt"): FakeResponse(
                    401, text="unauthorized"
                ),
            }
            client, _http, _cfg = build(tmp, routes)
            with self.assertRaises(OpenCodeError) as ctx:
                client.ask("hi")

        self.assertIn("401", str(ctx.exception))
        self.assertIn("VOICE_OPENCODE_PASSWORD", str(ctx.exception))


class HistoryTests(unittest.TestCase):
    def test_zuege_mit_letzter_antwort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("GET", BASE + "/api/session/ses_h/message"): FakeResponse(
                    200,
                    {
                        "data": [
                            {"type": "user", "time": {"created": 2000}, "text": "Danke"},
                            {
                                "type": "assistant",
                                "time": {"created": 1200},
                                "content": [
                                    {"type": "reasoning", "text": "intern"},
                                    {"type": "text", "text": "Zweite Nachricht"},
                                ],
                            },
                            {"type": "user", "time": {"created": 1000}, "text": "Hallo"},
                            {
                                "type": "assistant",
                                "time": {"created": 2100},
                                "content": [{"type": "text", "text": "Gern."}],
                            },
                            {"type": "idle", "time": {"created": 2200}},
                            {
                                "type": "assistant",
                                "time": {"created": 1100},
                                "content": [
                                    {"type": "text", "text": "Hallo! Wie kann ich helfen?"},
                                    {"type": "tool", "name": "read"},
                                ],
                            },
                        ]
                    },
                )
            }
            client, _http, _cfg = build(tmp, routes)
            rows = client.history("ses_h")
            last_only = client.history("ses_h", max_turns=1)

        self.assertEqual(
            [(row["role"], row["text"]) for row in rows],
            [
                ("user", "Hallo"),
                ("assistant", "Zweite Nachricht"),
                ("user", "Danke"),
                ("assistant", "Gern."),
            ],
        )
        self.assertAlmostEqual(rows[0]["time"], 1.0)
        self.assertEqual(
            [(row["role"], row["text"]) for row in last_only],
            [("user", "Danke"), ("assistant", "Gern.")],
        )

    def test_fallback_ohne_nutzer_nachrichten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("GET", BASE + "/api/session/ses_t/message"): FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "type": "assistant",
                                "time": {"created": 3000},
                                "content": [{"type": "text", "text": "B"}],
                            },
                            {
                                "type": "assistant",
                                "time": {"created": 1000},
                                "content": [{"type": "reasoning", "text": "nur intern"}],
                            },
                            {
                                "type": "assistant",
                                "time": {"created": 2000},
                                "content": [{"type": "text", "text": "A"}],
                            },
                        ]
                    },
                )
            }
            client, _http, _cfg = build(tmp, routes)
            rows = client.history("ses_t")

        self.assertEqual(
            [(row["role"], row["text"]) for row in rows],
            [("assistant", "A"), ("assistant", "B")],
        )


class SessionListTests(unittest.TestCase):
    def test_filter_sort_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("GET", BASE + "/api/session"): FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "id": "a",
                                "title": "A",
                                "location": {"directory": "/w"},
                                "time": {"updated": 100},
                            },
                            {
                                "id": "b",
                                "title": "B",
                                "location": {"directory": "/x"},
                                "time": {"updated": 300},
                            },
                            {
                                "id": "c",
                                "title": "C",
                                "location": {"directory": "/w"},
                                "time": {"updated": 200},
                            },
                        ]
                    },
                )
            }
            client, _http, _cfg = build(tmp, routes)
            rows = client.list_sessions(directory="/w", limit=5)
            limited = client.list_sessions(directory="/w", limit=1)

        self.assertEqual([row["id"] for row in rows], ["c", "a"])
        self.assertEqual([row["id"] for row in limited], ["c"])


class ModelTests(unittest.TestCase):
    def test_create_session_mit_modell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("GET", BASE + "/api/agent"): FakeResponse(200, {"data": []}),
                ("POST", BASE + "/api/session"): FakeResponse(200, {"data": {"id": "ses_1"}}),
            }
            client, http, _cfg = build(tmp, routes)
            client.create_session(model="treno/local:default")

        payload = session_call(http)[2]["json"]
        self.assertEqual(
            payload["model"], {"providerID": "treno", "id": "local:default"}
        )

    def test_set_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("POST", BASE + "/api/session/ses_1/model"): FakeResponse(204, text=""),
            }
            client, http, _cfg = build(tmp, routes)
            self.assertTrue(client.set_model("ses_1", "treno/remote:default"))
            self.assertFalse(client.set_model("ses_1", "kaputt"))

        call = [entry for entry in http.calls if entry[1].endswith("/model")][0]
        self.assertEqual(
            call[2]["json"],
            {"model": {"providerID": "treno", "id": "remote:default"}},
        )

    def test_list_models_filtert_provider_und_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("GET", BASE + "/api/provider"): FakeResponse(
                    200,
                    {
                        "data": [
                            {"id": "treno", "activation": "enabled"},
                            {"id": "openrouter", "activation": "auto"},
                        ]
                    },
                ),
                ("GET", BASE + "/api/model"): FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "providerID": "treno",
                                "modelID": "local:default",
                                "name": "Local",
                                "enabled": True,
                                "capabilities": {"tools": True},
                            },
                            {
                                "providerID": "treno",
                                "modelID": "remote:default",
                                "name": "Remote",
                                "enabled": True,
                                "capabilities": {"tools": True},
                            },
                            {
                                "providerID": "openrouter",
                                "modelID": "x",
                                "name": "X",
                                "enabled": True,
                                "capabilities": {"tools": True},
                            },
                            {
                                "providerID": "treno",
                                "modelID": "image",
                                "name": "Image",
                                "enabled": True,
                                "capabilities": {"tools": False},
                            },
                        ]
                    },
                ),
            }
            client, _http, _cfg = build(tmp, routes)
            rows = client.list_models()

        self.assertEqual(
            [row["value"] for row in rows],
            ["treno/local:default", "treno/remote:default"],
        )


class McpTests(unittest.TestCase):
    def test_list_mcp_mit_config_zustand(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            routes = {
                ("GET", BASE + "/api/mcp"): FakeResponse(
                    200,
                    {
                        "data": [
                            {"name": "brave", "status": {"status": "connected"}},
                            {"name": "figma", "status": {"status": "connected"}},
                            {"name": "mermaid", "status": {"status": "connected"}},
                        ]
                    },
                ),
                ("GET", BASE + "/api/config"): FakeResponse(
                    200,
                    [
                        {
                            "type": "document",
                            "path": "/x/opencode.json",
                            "info": {
                                "mcp": {
                                    "servers": {
                                        "brave": {"type": "local", "disabled": False},
                                        "mermaid": {"type": "local", "disabled": True},
                                    }
                                }
                            },
                        }
                    ],
                ),
            }
            client, _http, _cfg = build(tmp, routes)
            rows = client.list_mcp()

        self.assertEqual(
            rows,
            [
                {
                    "name": "brave",
                    "status": "connected",
                    "enabled": True,
                    "configurable": True,
                },
                {
                    "name": "figma",
                    "status": "connected",
                    "enabled": True,
                    "configurable": False,
                },
                {
                    "name": "mermaid",
                    "status": "disabled",
                    "enabled": False,
                    "configurable": True,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
