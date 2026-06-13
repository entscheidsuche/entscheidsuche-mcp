"""Tests fuer entscheidsuche_mcp.statistik."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import date
from pathlib import Path

import pytest

from entscheidsuche_mcp import statistik


def _log(ts: str, method: str, tool: str = "-", status: str = "200",
         size: int = 5000, ms: int = 10, client: str = "127.0.0.1",
         app: str = "-", appver: str = "-") -> str:
    return (f"{ts},000 INFO entscheidsuche_mcp.access — method={method} tool={tool} "
            f"id=1 status={status} size={size} ms={ms} client={client} "
            f"app={app} appver={appver} args=")


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Parser + Aggregator
# ---------------------------------------------------------------------------


def test_parse_log_returns_chronological_rows(tmp_path):
    p = tmp_path / "afa.log"
    _write_log(p, [
        _log("2026-06-08 10:00:00", "tools/call", "search"),
        _log("2026-06-08 09:00:00", "initialize", app="Claude", appver="1.0"),
    ])
    rows = statistik.parse_log(p)
    assert [r["method"] for r in rows] == ["initialize", "tools/call"]


def test_parser_resolves_app_for_following_tools_call(tmp_path):
    """Nach initialize gehoeren spaetere Calls von derselben IP zur App."""
    p = tmp_path / "afa.log"
    _write_log(p, [
        _log("2026-06-08 09:00:00", "initialize",
             client="160.79.106.37", app="Claude", appver="1.0"),
        _log("2026-06-08 09:00:05", "tools/call", "search",
             client="160.79.106.38"),  # andere IP, aber gleicher Pool
        _log("2026-06-08 09:00:10", "tools/call", "fetch_document",
             client="160.79.106.39"),
    ])
    rows = statistik.parse_log(p)
    assert rows[0]["resolved_app"] == "Claude"
    assert rows[1]["resolved_app"] == "Claude"
    assert rows[2]["resolved_app"] == "Claude"


def test_parser_session_timeout(tmp_path):
    """Nach 30+ Min Pause keine App-Zuordnung mehr."""
    p = tmp_path / "afa.log"
    _write_log(p, [
        _log("2026-06-08 09:00:00", "initialize",
             client="160.79.106.37", app="Claude", appver="1.0"),
        _log("2026-06-08 10:00:00", "tools/call", "search",
             client="160.79.106.37"),
    ])
    rows = statistik.parse_log(p)
    assert rows[0]["resolved_app"] == "Claude"
    assert rows[1]["resolved_app"] == "-"


def test_aggregate_counts_apps_tool_calls_separately(tmp_path):
    p = tmp_path / "afa.log"
    _write_log(p, [
        _log("2026-06-08 09:00:00", "initialize",
             client="160.79.106.37", app="Claude", appver="1.0"),
        _log("2026-06-08 09:00:01", "tools/list", client="160.79.106.37"),
        _log("2026-06-08 09:00:02", "tools/call", "search",
             client="160.79.106.37"),
        _log("2026-06-08 09:00:03", "tools/call", "fetch_document",
             client="160.79.106.37"),
    ])
    days = statistik.aggregate_per_day(statistik.parse_log(p))
    d = days["2026-06-08"]
    assert d["apps_tool_calls"]["Claude"] == 2
    assert d["apps_setup_calls"]["Claude"] == 2  # initialize + tools/list
    assert d["apps_all_calls"]["Claude"] == 4


def test_aggregate_sessions_with_tools(tmp_path):
    """Eine Session ohne tools/call zaehlt nicht in sessions_with_tools."""
    p = tmp_path / "afa.log"
    _write_log(p, [
        # Session 1: aktiv (mit Tool-Call)
        _log("2026-06-08 09:00:00", "initialize",
             client="160.79.106.37", app="Claude", appver="1.0"),
        _log("2026-06-08 09:00:01", "tools/call", "search",
             client="160.79.106.37"),
        # Session 2: nur initialize, kein Tool-Call (anderer Tag, frische Pool-IP)
        _log("2026-06-08 15:00:00", "initialize",
             client="8.8.8.8", app="ChatGPT", appver="4.0"),
    ])
    days = statistik.aggregate_per_day(statistik.parse_log(p))
    d = days["2026-06-08"]
    assert d["sessions"] == 2
    assert d["sessions_with_tools"] == 1


def test_aggregate_error_breakdown(tmp_path):
    p = tmp_path / "afa.log"
    _write_log(p, [
        _log("2026-06-08 09:00:00", "tools/call", "search", status="400"),
        _log("2026-06-08 09:00:01", "tools/call", "search", status="400"),
        _log("2026-06-08 09:00:02", "initialize", status="500"),
        _log("2026-06-08 09:00:03", "tools/call", "search", status="200"),
    ])
    days = statistik.aggregate_per_day(statistik.parse_log(p))
    d = days["2026-06-08"]
    assert d["errors"] == 3
    assert d["error_breakdown"]["tools/call → 400"] == 2
    assert d["error_breakdown"]["initialize → 500"] == 1


def test_aggregate_hours_setup_separated(tmp_path):
    p = tmp_path / "afa.log"
    _write_log(p, [
        _log("2026-06-08 09:00:00", "initialize"),
        _log("2026-06-08 09:00:01", "tools/call", "search"),
        _log("2026-06-08 09:00:02", "tools/call", "search"),
    ])
    days = statistik.aggregate_per_day(statistik.parse_log(p))
    d = days["2026-06-08"]
    assert d["hours_tools"][9] == 2
    assert d["hours_setup"][9] == 1
    assert d["hours_all"][9] == 3


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def test_cache_roundtrip_preserves_new_fields(tmp_path):
    p = tmp_path / "cache.json"
    agg = statistik._empty_day()
    agg["sessions_with_tools"] = 3
    agg["apps_tool_calls"]["Claude"] = 12
    agg["apps_setup_calls"]["Claude"] = 4
    agg["apps_all_calls"]["Claude"] = 16
    agg["error_breakdown"]["tools/call → 400"] = 2
    agg["hours_setup"][9] = 5
    statistik.save_cache(p, {"2026-06-08": agg})

    loaded = statistik.load_cache(p)
    out = loaded["2026-06-08"]
    assert out["sessions_with_tools"] == 3
    assert out["apps_tool_calls"]["Claude"] == 12
    assert out["apps_setup_calls"]["Claude"] == 4
    assert out["error_breakdown"]["tools/call → 400"] == 2
    assert out["hours_setup"][9] == 5


def test_cache_loads_old_schema_without_new_fields(tmp_path):
    """Caches aus der v1-Zeit ohne apps_tool_calls etc. muessen geladen werden."""
    p = tmp_path / "cache.json"
    p.write_text(json.dumps({
        "schema_version": 1,
        "days": {
            "2026-06-01": {
                "total": 5, "tool_calls": 2, "setup": 3, "sessions": 1,
                "tools": {"search": 2}, "methods": {"tools/call": 2},
                "apps": {"Claude": 1},
                "apps_last_ts": {"Claude": "2026-06-01T09:00:00"},
                "hours_tools": [0]*9 + [2] + [0]*14,
                "hours_all": [0]*9 + [5] + [0]*14,
                "ms_total": 50, "ms_n": 5, "errors": 0,
            }
        }
    }))
    loaded = statistik.load_cache(p)
    d = loaded["2026-06-01"]
    assert d["sessions_with_tools"] == 0  # default
    assert d["apps_tool_calls"] == {}
    assert d["hours_setup"][9] == 3  # = all - tools


def test_refresh_cache_persists_only_past_days(tmp_path):
    log_path = tmp_path / "afa.log"
    cache_path = tmp_path / "stats-cache.json"
    today = date.today().isoformat()
    _write_log(log_path, [
        _log("2026-01-01 09:00:00", "tools/call", "search"),
        _log(f"{today} 10:00:00", "tools/call", "fetch_document"),
    ])
    combined = statistik.refresh_cache(cache_path, log_path, today=today)
    assert "2026-01-01" in combined
    assert today in combined
    on_disk = json.loads(cache_path.read_text())
    assert "2026-01-01" in on_disk["days"]
    assert today not in on_disk["days"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_auth_open_when_no_env(monkeypatch):
    monkeypatch.delenv("ESC_STATS_USER", raising=False)
    monkeypatch.delenv("ESC_STATS_PASS", raising=False)
    assert statistik.check_basic_auth(_FakeRequest()) is None


def test_auth_accepts_correct_credentials(monkeypatch):
    monkeypatch.setenv("ESC_STATS_USER", "admin")
    monkeypatch.setenv("ESC_STATS_PASS", "secret")
    creds = base64.b64encode(b"admin:secret").decode("ascii")
    req = _FakeRequest({"authorization": f"Basic {creds}"})
    assert statistik.check_basic_auth(req) is None


def test_auth_rejects_wrong(monkeypatch):
    monkeypatch.setenv("ESC_STATS_USER", "admin")
    monkeypatch.setenv("ESC_STATS_PASS", "secret")
    assert statistik.check_basic_auth(_FakeRequest()).status_code == 401


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def test_render_html_contains_kpis_and_new_columns(tmp_path):
    today = date.today().isoformat()
    log_path = tmp_path / "afa.log"
    cache_path = tmp_path / "cache.json"
    _write_log(log_path, [
        _log(f"{today} 09:00:00", "initialize",
             client="160.79.106.37", app="Claude", appver="1.0"),
        _log(f"{today} 09:00:01", "tools/call", "search",
             client="160.79.106.37"),
        _log(f"{today} 09:00:02", "tools/call", "search",
             client="160.79.106.37"),
    ])
    days = statistik.refresh_cache(cache_path, log_path, today=today)
    body = statistik.render_html(days, log_path, cache_path)
    assert "Nutzungsstatistik" in body
    assert "Tool-Aufrufe" in body
    # neue Spalten:
    assert "Sessions" in body
    assert "Top-Tools" in body  # bleibt als Panel-Heading
    assert "Tool-Nutzung" in body  # Top-3-Label
    assert "Claude" in body
    # Setup-Spalte sollte NICHT mehr in der Tagesübersicht sein
    # (aber Setup als KPI-Begriff im Methoden-Panel ist ok)
    # Stacked-Bar in KI-Clients-Sektion:
    assert "bar-stack" in body
    # Tooltips:
    assert "has-tooltip" in body or "title=" in body


def test_render_html_handles_empty_log(tmp_path):
    body = statistik.render_html({}, tmp_path / "log", tmp_path / "cache")
    assert "Noch keine Daten" in body
