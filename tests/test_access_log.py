"""Tests fuer entscheidsuche_mcp.access_log."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from entscheidsuche_mcp.access_log import (
    JsonRpcAccessLogMiddleware,
    _describe_rpc,
    _safe_field,
    _summarize_args,
)


def test_safe_field_strips_unsafe_chars():
    assert _safe_field("Claude Desktop 1.0") == "ClaudeDesktop1.0"
    assert _safe_field(None) == "-"
    assert _safe_field("") == "-"
    assert len(_safe_field("x" * 100, max_len=10)) == 10


def test_describe_single_initialize():
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "Claude", "version": "1.0"},
        },
    }
    [r] = _describe_rpc(payload)
    assert r["method"] == "initialize"
    assert r["args"] == {"client": "Claude", "version": "1.0", "protocol": "2025-06-18"}


def test_describe_tools_call_search():
    payload = {
        "jsonrpc": "2.0", "id": 42, "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "BGE 145 IV", "size": 3}},
    }
    [r] = _describe_rpc(payload)
    assert r["tool"] == "search"
    assert r["args"] == {"query": "BGE 145 IV", "size": 3}


def test_describe_batch():
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    ]
    rs = _describe_rpc(payload)
    assert [r["method"] for r in rs] == ["tools/list", "notifications/initialized"]
    assert rs[1]["id"] is None


def test_describe_empty_payload():
    assert _describe_rpc(None) == []
    assert _describe_rpc([]) == []
    assert _describe_rpc("not-a-dict") == []


def test_summarize_args_truncation(monkeypatch):
    monkeypatch.setenv("ESC_ACCESS_LOG_ARGS_MAX", "20")
    s = _summarize_args({"query": "x" * 100})
    assert s.endswith("…")
    assert len(s) == 20


def test_summarize_args_none_returns_empty():
    assert _summarize_args(None) == ""


async def _drive_middleware(payload):
    body = json.dumps(payload).encode("utf-8")
    received_body = bytearray()

    async def inner_app(scope, receive, send):
        while True:
            msg = await receive()
            if msg["type"] != "http.request":
                break
            received_body.extend(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

    mw = JsonRpcAccessLogMiddleware(inner_app, mcp_path="/mcp")
    half = len(body) // 2
    requests = [
        {"type": "http.request", "body": body[:half], "more_body": True},
        {"type": "http.request", "body": body[half:], "more_body": False},
    ]
    queue = iter(requests)

    async def receive():
        return next(queue)

    sent: list[dict[str, Any]] = []

    async def send(msg):
        sent.append(msg)

    scope = {
        "type": "http", "method": "POST", "path": "/mcp",
        "client": ("160.79.106.37", 12345), "headers": [],
    }
    await mw(scope, receive, send)
    return bytes(received_body), sent


def test_middleware_replays_body_intact(caplog):
    caplog.set_level(logging.INFO, logger="entscheidsuche_mcp.access")
    payload = {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "Mietzins"}},
    }
    received, sent = asyncio.run(_drive_middleware(payload))
    assert json.loads(received) == payload
    assert sent[0]["status"] == 200
    line = next(r.getMessage() for r in caplog.records
                if r.name == "entscheidsuche_mcp.access")
    assert "method=tools/call" in line
    assert "tool=search" in line
    assert "id=7" in line
    assert "status=200" in line
    assert "client=160.79.106.37" in line
    assert "Mietzins" in line


def test_middleware_logs_initialize_with_app_info(caplog):
    caplog.set_level(logging.INFO, logger="entscheidsuche_mcp.access")
    payload = {
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "Claude", "version": "1.2.3"},
        },
    }
    asyncio.run(_drive_middleware(payload))
    line = next(r.getMessage() for r in caplog.records
                if r.name == "entscheidsuche_mcp.access")
    assert "method=initialize" in line
    assert "app=Claude" in line
    assert "appver=1.2.3" in line


def test_middleware_logs_app_dash_for_non_initialize(caplog):
    caplog.set_level(logging.INFO, logger="entscheidsuche_mcp.access")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    asyncio.run(_drive_middleware(payload))
    line = next(r.getMessage() for r in caplog.records
                if r.name == "entscheidsuche_mcp.access")
    assert "app=-" in line
    assert "appver=-" in line


def test_middleware_logs_batch_separate_lines(caplog):
    caplog.set_level(logging.INFO, logger="entscheidsuche_mcp.access")
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
    ]
    asyncio.run(_drive_middleware(payload))
    lines = [r.getMessage() for r in caplog.records
             if r.name == "entscheidsuche_mcp.access"]
    assert any("method=tools/list" in l for l in lines)
    assert any("method=resources/list" in l for l in lines)


def test_middleware_skips_non_mcp_path(caplog):
    caplog.set_level(logging.INFO, logger="entscheidsuche_mcp.access")

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = JsonRpcAccessLogMiddleware(inner, mcp_path="/mcp")
    scope = {"type": "http", "method": "GET", "path": "/foo",
             "client": ("1.2.3.4", 0), "headers": []}

    async def recv():
        return {"type": "http.disconnect"}

    sent: list[Any] = []

    async def send(m):
        sent.append(m)

    asyncio.run(mw(scope, recv, send))
    assert sent[0]["status"] == 200
    assert not [r for r in caplog.records
                if r.name == "entscheidsuche_mcp.access"]
