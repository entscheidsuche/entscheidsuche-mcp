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


async def test_search_tools_tell_the_model_to_read_the_full_text():
    """Ohne den Hinweis werten Modelle oft nur die Trefferliste aus."""
    mcp = build_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name in ("search", "semantic_search", "keyword_search", "search_by_case_number"):
        description = tools[name].description
        assert "only an excerpt" in description, name
        assert "`fetch`" in description, name
    assert "only an excerpt" in mcp.instructions


async def test_conversation_id_is_logged_only_when_switched_on(tmp_path, monkeypatch):
    """`conv=` fasst die Aufrufe einer ChatGPT-Unterhaltung zusammen; ohne den
    Schalter bleibt das Zeilenformat unverändert."""
    path = tmp_path / "access.log"
    log = access_log.log
    saved = list(log.handlers)
    for handler in saved:
        log.removeHandler(handler)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    app = access_log.JsonRpcAccessLogMiddleware(inner, mcp_path="/mcp")
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    def body(session):
        return {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "x"},
                       "_meta": {"openai/session": session}},
        }

    try:
        monkeypatch.setenv("ESC_ACCESS_LOG_SESSION", "false")
        await client.post("/mcp", json=body("conversation-A"))
        monkeypatch.setenv("ESC_ACCESS_LOG_SESSION", "true")
        await client.post("/mcp", json=body("conversation-A"))
        await client.post("/mcp", json=body("conversation-A"))
        await client.post("/mcp", json=body("conversation-B"))
        await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={"x-openai-session": "conversation-A"},
        )
        await client.aclose()
        handler.flush()
        lines = path.read_text(encoding="utf-8").splitlines()
    finally:
        log.removeHandler(handler)
        handler.close()
        for old in saved:
            log.addHandler(old)

    assert "conv=" not in lines[0]
    ids = [line.split(" conv=")[1].split(" ")[0] for line in lines[1:]]
    assert ids[0] == ids[1] == ids[3] != ids[2]
    assert len(ids[0]) == 12 and "conversation" not in "".join(lines)
    # die Statistik liest die Zeilen weiterhin
    assert all(statistik.LOG_LINE.match(line) for line in lines)


async def test_rejected_requests_log_the_reason(tmp_path, monkeypatch):
    """Bei HTTP-Fehlern steht der Grund der Ablehnung im Log (`err=`)."""
    monkeypatch.setenv("ESC_ACCESS_LOG", "true")
    path = tmp_path / "access.log"
    log = access_log.log
    saved = list(log.handlers)
    for handler in saved:
        log.removeHandler(handler)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s — %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        async with _Server() as server:
            headers = {**BASE_HEADERS, "MCP-Protocol-Version": MODERN}
            # neues Protokoll ohne die Pflichtangaben in `_meta`
            await server.http.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers
            )
            # kein JSON
            await server.http.post("/mcp", content=b"hallo", headers=BASE_HEADERS)
            # gültiger Aufruf
            await server.legacy("tools/list", {})
        handler.flush()
        lines = path.read_text(encoding="utf-8").splitlines()
    finally:
        log.removeHandler(handler)
        handler.close()
        for old in saved:
            log.addHandler(old)

    assert len(lines) == 3
    assert "status=400" in lines[0] and " err=-32602:params._meta-must-be-an-object" in lines[0]
    assert "method=- " in lines[1] and " err=-32700:Parse-error" in lines[1]
    assert "status=200" in lines[2] and " err=" not in lines[2]
    rows = [statistik._parse_line(line) for line in lines]
    assert rows[0]["err"].startswith("-32602:") and rows[2]["err"] == "-"


def test_statistik_separates_probe_errors():
    def row(method, status, app="-", rid="1", err="-"):
        r = _row("2026-11-26 06:00:00", method, app=app)
        r.update(status=status, rid=rid, err=err)
        return r

    rows = [
        row("server/discover", "400", app="rokmcp-probe", rid="probe-badversion",
            err="-32022:Unsupported-protocol-version"),
        row("server/discover", "400", app="rokmcp-probe", rid="probe-badversion"),
        row("server/discover", "400", app="VibeShippedHealthCheck"),
        row("tools/call", "400", err="-32020:mcp-name-header-does-not-match"),
        row("tools/list", "400"),
        row("tools/list", "200"),
    ]
    statistik._resolve_apps(rows)
    day = statistik.aggregate_per_day(rows)["2026-11-26"]

    assert day["errors"] == 2
    assert day["probe_errors"] == 3
    assert day["error_breakdown"] == {
        "Prüfanfragen: rokmcp-probe": 2,
        "Prüfanfragen: VibeShippedHealthCheck": 1,
        "tools/call → 400 (-32020:mcp-name-header-does-not-match)": 1,
        "tools/list → 400": 1,
    }
    # Hin- und Rückweg durch den Cache; alte Caches ohne das Feld laden mit 0
    restored = statistik._aggregate_from_json(statistik._aggregate_to_json(day))
    assert restored["probe_errors"] == 3
    old = statistik._aggregate_to_json(day)
    del old["probe_errors"]
    assert statistik._aggregate_from_json(old)["probe_errors"] == 0
