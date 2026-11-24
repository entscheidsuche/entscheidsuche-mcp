"""Tests für den Betrieb mit MCP-SDK 2.x: Protokollversion 2026-07-28
(zustandslos, `server/discover`) neben dem bisherigen `initialize`-Handshake."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
import pytest

from entscheidsuche_mcp import __version__, access_log, statistik
from entscheidsuche_mcp.access_log import _describe_rpc
from entscheidsuche_mcp.server import build_server, create_app

MODERN = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "probe", "version": "9"},
}
BASE_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class _Server:
    """Der ASGI-Server im Test, ohne Netzwerk und ohne Access-Log."""

    async def __aenter__(self):
        self.app = create_app(build_server())
        self._lifespan = self.app.router.lifespan_context(self.app)
        await self._lifespan.__aenter__()
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1"
        )
        return self

    async def __aexit__(self, *exc):
        await self.http.aclose()
        await self._lifespan.__aexit__(*exc)

    async def legacy(self, method: str, params: dict | None = None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method}
        if params is not None:
            body["params"] = params
        return await self.http.post("/mcp", json=body, headers=BASE_HEADERS)

    async def modern(self, method: str, params: dict | None = None, name: str | None = None):
        headers = {**BASE_HEADERS, "MCP-Protocol-Version": MODERN, "Mcp-Method": method}
        if name:
            headers["Mcp-Name"] = name
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": {**(params or {}), "_meta": META},
        }
        return await self.http.post("/mcp", json=body, headers=headers)


@pytest.fixture(autouse=True)
def _no_access_log(monkeypatch):
    monkeypatch.setenv("ESC_ACCESS_LOG", "false")


async def test_discover_answers_modern_protocol():
    async with _Server() as server:
        response = await server.modern("server/discover")
    assert response.status_code == 200
    result = response.json()["result"]
    assert MODERN in result["supportedVersions"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"] == {
        "name": "entscheidsuche",
        "version": __version__,
    }
    # der Server kündigt keine Abonnements und keine Listenänderungen an
    assert not result["capabilities"].get("resources", {}).get("subscribe")
    # die Antwort darf zwischengespeichert werden
    assert result["ttlMs"] == 3_600_000
    assert result["cacheScope"] == "public"


async def test_modern_tools_list_and_call_without_initialize():
    async with _Server() as server:
        listing = await server.modern("tools/list")
        call = await server.modern(
            "tools/call", {"name": "fetch", "arguments": {"id": ""}}, name="fetch"
        )
    assert listing.status_code == 200
    tools = listing.json()["result"]["tools"]
    assert len(tools) == 9
    assert tools[0]["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    assert call.status_code == 200
    result = call.json()["result"]
    assert result["isError"] is True
    assert "must not be empty" in result["content"][0]["text"]


async def test_legacy_handshake_still_works():
    async with _Server() as server:
        init = await server.legacy(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "old", "version": "1"},
            },
        )
        listing = await server.legacy("tools/list", {})
    result = init.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["version"] == __version__
    assert len(listing.json()["result"]["tools"]) == 9


@pytest.mark.parametrize("modern", [False, True])
async def test_tool_errors_keep_their_message(modern):
    """SDK 2.x reicht nur `ToolError`-Texte durch; alles andere wird zu
    „Error executing tool …"."""
    arguments = {"name": "fetch", "arguments": {"id": "   "}}
    async with _Server() as server:
        if modern:
            response = await server.modern("tools/call", arguments, name="fetch")
        else:
            response = await server.legacy("tools/call", arguments)
    result = response.json()["result"]
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert text.endswith(
        "The document id must not be empty. Pass the `id` of a hit returned "
        "by one of the search tools."
    )


async def test_empty_case_number_is_reported():
    async with _Server() as server:
        response = await server.legacy(
            "tools/call",
            {"name": "search_by_case_number", "arguments": {"case_number": '""'}},
        )
    result = response.json()["result"]
    assert result["isError"] is True
    assert "`case_number` must not be empty" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# Access-Log und Statistik
# ---------------------------------------------------------------------------


def test_access_log_reads_client_from_meta():
    [row] = _describe_rpc(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "x"}, "_meta": META},
        }
    )
    assert (row["app"], row["appver"]) == ("probe", "9")
    assert row["args"] == {"query": "x"}

    [init] = _describe_rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "Claude", "version": "1.0"}},
        }
    )
    assert (init["app"], init["appver"]) == ("Claude", "1.0")

    [plain] = _describe_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert (plain["app"], plain["appver"]) == ("-", "-")


def test_configure_file_handler_does_not_duplicate_lines(tmp_path, monkeypatch):
    """Mit `ESC_ACCESS_LOG_FILE` stand früher jede Zeile doppelt im Log."""
    path = tmp_path / "access.log"
    monkeypatch.setenv("ESC_ACCESS_LOG_FILE", str(path))
    log = access_log.log
    saved = list(log.handlers)
    for handler in saved:
        log.removeHandler(handler)
    try:
        access_log._ensure_logger_has_handler()  # geschieht beim Import von `server`
        access_log.configure_file_handler()  # geschieht in `__main__`
        assert len(log.handlers) == 1
        log.info("method=ping tool=- id=1 status=200 size=2 ms=0 client=- app=- appver=- args=")
        for handler in log.handlers:
            handler.flush()
        assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        for handler in list(log.handlers):
            log.removeHandler(handler)
            handler.close()
        for handler in saved:
            log.addHandler(handler)
        log.setLevel(logging.INFO)


def _row(ts: str, method: str, app: str = "-", tool: str = "-", client: str = "10.0.0.1") -> dict:
    stamp = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    return {
        "ts": stamp, "date": ts[:10], "hour": stamp.hour, "method": method, "tool": tool,
        "status": "200", "size": 100, "ms": 5, "client": client, "app": app, "appver": "-",
    }


def test_statistik_counts_sessions_without_initialize():
    rows = [
        # zustandsloser Client: nennt sich in jedem Request
        _row("2026-11-24 10:00:00", "server/discover", app="ChatGPT"),
        _row("2026-11-24 10:00:01", "tools/list", app="ChatGPT"),
        _row("2026-11-24 10:00:05", "tools/call", app="ChatGPT", tool="search"),
        # nach mehr als 30 Minuten Pause: neue Session, ohne Tool-Aufruf
        _row("2026-11-24 11:00:00", "tools/list", app="ChatGPT"),
        # bisheriger Client mit Handshake aus einem anderen Pool
        _row("2026-11-24 10:10:00", "initialize", app="Claude", client="10.9.9.9"),
        _row("2026-11-24 10:10:02", "tools/call", tool="fetch", client="10.9.9.9"),
    ]
    rows.sort(key=lambda r: r["ts"])
    statistik._resolve_apps(rows)
    day = statistik.aggregate_per_day(rows)["2026-11-24"]

    assert day["sessions"] == 3
    assert day["sessions_with_tools"] == 2
    assert day["apps"] == {"ChatGPT": 2, "Claude": 1}
    assert day["apps_tool_calls"] == {"ChatGPT": 1, "Claude": 1}
    assert day["tool_calls"] == 2
    assert day["setup"] == 4  # discover, 2 × tools/list, initialize
