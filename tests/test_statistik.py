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
    return (f"{ts},000 INFO entscheidsuche_mcp.access — method={method} "
            f"tool={tool} id=1 status={status} size={size} ms={ms} "
            f"client={client} app={app} appver={appver} args=")


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_log_returns_chronological_rows(tmp_path):
    p = tmp_path / "acc.log"
    _write_log(p, [
        _log("2026-06-08 10:00:00", "tools/call", "search"),
        _log("2026-06-08 09:00:00", "initialize", app="Claude", appver="1.0"),
    ])
    rows = statistik.parse_log(p)
    assert [r["method"] for r in rows] == ["initialize", "tools/call"]


def test_aggregate_per_day_counts_categories(tmp_path):
    p = tmp_path / "acc.log"
    _write_log(p, [
        _log("2026-06-08 09:00:00", "initialize", app="Claude", appver="1.0"),
        _log("2026-06-08 09:00:01", "tools/list"),
        _log("2026-06-08 09:00:02", "tools/call", "search"),
        _log("2026-06-08 09:00:03", "tools/call", "fetch_document"),
        _log("2026-06-08 09:00:04", "tools/call", "search"),
        _log("2026-06-09 10:00:00", "initialize", app="ChatGPT", appver="4.5"),
    ])
    days = statistik.aggregate_per_day(statistik.parse_log(p))
    assert set(days) == {"2026-06-08", "2026-06-09"}
    d8 = days["2026-06-08"]
    assert d8["tool_calls"] == 3
    assert d8["sessions"] == 1
    assert d8["setup"] == 2
    assert d8["tools"]["search"] == 2
    assert d8["apps"]["Claude"] == 1
    assert d8["hours_tools"][9] == 3
    d9 = days["2026-06-09"]
    assert d9["sessions"] == 1
    assert d9["apps"]["ChatGPT"] == 1


def test_aggregate_handles_4xx_errors(tmp_path):
    p = tmp_path / "acc.log"
    _write_log(p, [
        _log("2026-06-08 09:00:00", "tools/call", "search", status="400"),
        _log("2026-06-08 09:00:01", "tools/call", "search", status="500"),
        _log("2026-06-08 09:00:02", "tools/call", "search", status="200"),
    ])
    days = statistik.aggregate_per_day(statistik.parse_log(p))
    assert days["2026-06-08"]["errors"] == 2


def test_cache_roundtrip_preserves_counters(tmp_path):
    p = tmp_path / "cache.json"
    agg = statistik._empty_day()
    agg["tool_calls"] = 7
    agg["tools"]["search"] = 4
    agg["tools"]["fetch_document"] = 3
    agg["apps"]["Claude"] = 2
    agg["apps_last_ts"]["Claude"] = "2026-06-08T15:30:00"
    agg["hours_tools"][10] = 5
    statistik.save_cache(p, {"2026-06-08": agg})

    loaded = statistik.load_cache(p)
    out = loaded["2026-06-08"]
    assert out["tool_calls"] == 7
    assert out["tools"]["search"] == 4
    assert out["apps"]["Claude"] == 2
    assert out["hours_tools"][10] == 5


def test_load_cache_missing_returns_empty(tmp_path):
    assert statistik.load_cache(tmp_path / "nope.json") == {}


def test_load_cache_corrupt_returns_empty(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("not json", encoding="utf-8")
    assert statistik.load_cache(p) == {}


def test_refresh_cache_persists_only_past_days(tmp_path):
    log_path = tmp_path / "acc.log"
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


def test_refresh_cache_idempotent_if_log_unchanged(tmp_path):
    log_path = tmp_path / "acc.log"
    cache_path = tmp_path / "stats-cache.json"
    today = date.today().isoformat()
    _write_log(log_path, [_log("2026-01-01 09:00:00", "tools/call", "search")])
    statistik.refresh_cache(cache_path, log_path, today=today)
    mtime_first = cache_path.stat().st_mtime
    statistik.refresh_cache(cache_path, log_path, today=today)
    assert cache_path.stat().st_mtime == mtime_first


def test_refresh_cache_uses_cache_when_log_rotated_away(tmp_path):
    log_path = tmp_path / "acc.log"
    cache_path = tmp_path / "stats-cache.json"
    today = date.today().isoformat()
    _write_log(log_path, [_log("2026-01-01 09:00:00", "tools/call", "search")])
    statistik.refresh_cache(cache_path, log_path, today=today)
    _write_log(log_path, [_log(f"{today} 10:00:00", "tools/call", "search")])
    combined = statistik.refresh_cache(cache_path, log_path, today=today)
    assert combined["2026-01-01"]["tool_calls"] == 1


class _FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_auth_open_when_no_env(monkeypatch):
    monkeypatch.delenv("ESC_STATS_USER", raising=False)
    monkeypatch.delenv("ESC_STATS_PASS", raising=False)
    assert statistik.check_basic_auth(_FakeRequest()) is None


def test_auth_requires_header(monkeypatch):
    monkeypatch.setenv("ESC_STATS_USER", "admin")
    monkeypatch.setenv("ESC_STATS_PASS", "secret")
    resp = statistik.check_basic_auth(_FakeRequest())
    assert resp.status_code == 401


def test_auth_rejects_wrong_password(monkeypatch):
    monkeypatch.setenv("ESC_STATS_USER", "admin")
    monkeypatch.setenv("ESC_STATS_PASS", "secret")
    creds = base64.b64encode(b"admin:nope").decode("ascii")
    req = _FakeRequest({"authorization": f"Basic {creds}"})
    assert statistik.check_basic_auth(req).status_code == 401


def test_auth_accepts_correct_credentials(monkeypatch):
    monkeypatch.setenv("ESC_STATS_USER", "admin")
    monkeypatch.setenv("ESC_STATS_PASS", "secret")
    creds = base64.b64encode(b"admin:secret").decode("ascii")
    req = _FakeRequest({"authorization": f"Basic {creds}"})
    assert statistik.check_basic_auth(req) is None


def test_render_html_contains_kpis_for_today(tmp_path):
    today = date.today().isoformat()
    log_path = tmp_path / "acc.log"
    cache_path = tmp_path / "cache.json"
    _write_log(log_path, [
        _log(f"{today} 09:00:00", "initialize", app="Claude", appver="1.0"),
        _log(f"{today} 09:00:01", "tools/call", "search"),
        _log(f"{today} 09:00:02", "tools/call", "search"),
    ])
    days = statistik.refresh_cache(cache_path, log_path, today=today)
    body = statistik.render_html(days, log_path, cache_path)
    assert "Nutzungsstatistik" in body
    assert ">2<" in body
    assert "search" in body
    assert "Claude" in body


def test_endpoint_returns_html_when_auth_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("ESC_STATS_USER", raising=False)
    monkeypatch.delenv("ESC_STATS_PASS", raising=False)
    log_path = tmp_path / "acc.log"
    cache_path = tmp_path / "cache.json"
    log_path.write_text("", encoding="utf-8")
    monkeypatch.setenv("ESC_ACCESS_LOG_FILE", str(log_path))
    monkeypatch.setenv("ESC_STATS_CACHE", str(cache_path))

    class _Req:
        headers: dict = {}

    resp = asyncio.run(statistik.statistik_endpoint(_Req()))
    assert resp.status_code == 200
    assert b"Nutzungsstatistik" in resp.body


def test_endpoint_returns_401_when_auth_enabled_and_missing(monkeypatch):
    monkeypatch.setenv("ESC_STATS_USER", "admin")
    monkeypatch.setenv("ESC_STATS_PASS", "secret")

    class _Req:
        headers: dict = {}

    resp = asyncio.run(statistik.statistik_endpoint(_Req()))
    assert resp.status_code == 401
