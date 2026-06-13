"""Statistik-Endpoint + JSON-Tagescache fuer den entscheidsuche-MCP-Server.

Verarbeitet das Access-Log und liefert eine selbst-enthaltene HTML-Seite
mit Tageszahlen, KI-Client-Klassifizierung (mit Aufschluesselung in
Tool-Calls vs. Setup-Calls), Top-Tools, Methoden-Verteilung und Stunden-
Sparklines. Wird **bei jedem Aufruf live** generiert; Vortage werden in
einer JSON-Cache-Datei festgehalten.

Konfiguration via Env-Variablen:

* ``ESC_ACCESS_LOG_FILE`` — Pfad zum Access-Log
* ``ESC_STATS_CACHE``     — Pfad zum JSON-Cache
* ``ESC_STATS_USER``      — Basic-Auth-Benutzer (leer = Auth aus)
* ``ESC_STATS_PASS``      — Basic-Auth-Passwort
"""

from __future__ import annotations

import base64
import gzip
import hmac
import html
import json
import logging
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

log = logging.getLogger("entscheidsuche_mcp.statistik")

LOG_LINE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}),\d+"
    r"\s+INFO\s+entscheidsuche_mcp\.access\s+[—-]+\s+"
    r"method=(?P<method>\S+)\s+"
    r"tool=(?P<tool>\S+)\s+"
    r"id=(?P<id>\S+)\s+"
    r"status=(?P<status>\S+)\s+"
    r"size=(?P<size>\d+)\s+"
    r"ms=(?P<ms>\d+)\s+"
    r"client=(?P<client>\S+)"
    r"(?:\s+app=(?P<app>\S+))?"
    r"(?:\s+appver=(?P<appver>\S+))?"
)

SETUP_METHODS = {"initialize", "notifications/initialized",
                 "tools/list", "resources/list", "prompts/list"}

def _default_log_path() -> Path:
    sys_path = Path("/var/log/entscheidsuche-mcp/access.log")
    if sys_path.parent.exists():
        return sys_path
    return Path.home() / "entscheidsuche-mcp.access.log"


def _default_cache_path() -> Path:
    sys_dir = Path("/var/lib/entscheidsuche-mcp")
    if sys_dir.exists() and os.access(sys_dir, os.W_OK):
        return sys_dir / "stats-cache.json"
    return Path.home() / ".entscheidsuche-mcp" / "stats-cache.json"


DEFAULT_LOG = _default_log_path()
DEFAULT_CACHE = _default_cache_path()

# Wenn nach einem initialize laenger als dieses Fenster keine Aktivitaet
# kommt, gilt die Session als beendet (fuer Client-Heuristik).
SESSION_TIMEOUT = timedelta(minutes=30)


# ---------------------------------------------------------------------------
# Client-Pool-Normalisierung
# ---------------------------------------------------------------------------


def _normalize_client_pool(ip: str) -> str:
    """Anthropic rotiert ueber 160.79.106.35-39 — alle als ein Pool."""
    if ip.startswith("160.79.106."):
        return "anthropic-pool"
    if ip.startswith("127."):
        return "localhost"
    return ip


# ---------------------------------------------------------------------------
# Log-Parsing
# ---------------------------------------------------------------------------


def iter_log_lines(path: Path) -> Iterable[str]:
    parent = path.parent
    base = path.name
    candidates: list[Path] = []
    if path.exists():
        for p in sorted(parent.glob(base + "*")):
            if p == path or p.name.startswith(base + "."):
                candidates.append(p)
    candidates.sort(key=lambda p: (p.suffix != "", p.name), reverse=True)
    for p in candidates:
        try:
            opener = gzip.open if p.suffix == ".gz" else open
            with opener(p, "rt", encoding="utf-8", errors="replace") as f:
                yield from f
        except FileNotFoundError:
            continue


def parse_log(path: Path) -> list[dict]:
    """Parst das Logfile und reichert jeden Eintrag um ``resolved_app`` an —
    die App-Zuordnung per Heuristik (zeitlich + Client-Pool)."""
    out: list[dict] = []
    for line in iter_log_lines(path):
        m = LOG_LINE.match(line)
        if not m:
            continue
        d = m.groupdict()
        try:
            ts = datetime.strptime(d["date"] + " " + d["time"],
                                   "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        out.append({
            "ts": ts, "date": d["date"], "hour": ts.hour,
            "method": d["method"], "tool": d["tool"],
            "status": d["status"], "size": int(d["size"]), "ms": int(d["ms"]),
            "client": d["client"],
            "app": d.get("app") or "-",
            "appver": d.get("appver") or "-",
        })
    out.sort(key=lambda r: r["ts"])

    # Heuristik: pro Pool-IP die "aktive" App tracken.
    active: dict[str, tuple[str, datetime]] = {}
    for r in out:
        pool = _normalize_client_pool(r["client"])
        r["pool"] = pool
        if r["method"] == "initialize" and r["app"] != "-":
            active[pool] = (r["app"], r["ts"])
            r["resolved_app"] = r["app"]
        else:
            entry = active.get(pool)
            if entry and (r["ts"] - entry[1]) <= SESSION_TIMEOUT:
                r["resolved_app"] = entry[0]
                active[pool] = (entry[0], r["ts"])  # Lebenszeit verlaengern
            else:
                r["resolved_app"] = "-"
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _empty_day() -> dict[str, Any]:
    return {
        "total": 0, "setup": 0, "tool_calls": 0, "other": 0,
        "sessions": 0, "sessions_with_tools": 0,
        "tools": Counter(),
        "methods": Counter(),
        "apps": Counter(),               # initialize-Counts (= Sessions)
        "apps_tool_calls": Counter(),    # tools/call zugeordnet
        "apps_setup_calls": Counter(),   # setup-Methoden zugeordnet
        "apps_all_calls": Counter(),     # alle Calls zugeordnet
        "apps_last_ts": {},
        "hours_tools": [0] * 24,
        "hours_setup": [0] * 24,
        "hours_all": [0] * 24,
        "ms_total": 0, "ms_n": 0, "errors": 0,
        "error_breakdown": Counter(),    # (method, status) -> count
    }


def aggregate_per_day(rows: list[dict]) -> dict[str, dict]:
    days: dict[str, dict] = defaultdict(_empty_day)

    # Pool-IP -> (date, became_active_with_tool_call)
    open_sessions: dict[str, dict] = {}

    def _close_session(pool: str) -> None:
        sess = open_sessions.get(pool)
        if sess and sess["active"]:
            days[sess["date"]]["sessions_with_tools"] += 1
        if sess:
            del open_sessions[pool]

    for r in rows:
        d = days[r["date"]]
        d["total"] += 1
        d["hours_all"][r["hour"]] += 1
        d["methods"][r["method"]] += 1
        resolved = r.get("resolved_app", "-")
        pool = r.get("pool", _normalize_client_pool(r["client"]))

        if resolved != "-":
            d["apps_all_calls"][resolved] += 1

        if r["method"] == "initialize":
            _close_session(pool)  # vorherige Session abschliessen, falls offen
            d["sessions"] += 1
            d["setup"] += 1
            d["hours_setup"][r["hour"]] += 1
            if r["app"] != "-":
                d["apps"][r["app"]] += 1
                d["apps_setup_calls"][r["app"]] += 1
                d["apps_last_ts"][r["app"]] = r["ts"].isoformat()
                open_sessions[pool] = {"date": r["date"], "active": False}
        elif r["method"] in SETUP_METHODS:
            d["setup"] += 1
            d["hours_setup"][r["hour"]] += 1
            if resolved != "-":
                d["apps_setup_calls"][resolved] += 1
        elif r["method"] == "tools/call":
            d["tool_calls"] += 1
            d["hours_tools"][r["hour"]] += 1
            if r["tool"] != "-":
                d["tools"][r["tool"]] += 1
            if resolved != "-":
                d["apps_tool_calls"][resolved] += 1
            if pool in open_sessions:
                open_sessions[pool]["active"] = True
        else:
            d["other"] += 1
            if resolved != "-":
                d["apps_setup_calls"][resolved] += 1  # zur Setup-Seite zaehlen

        d["ms_total"] += r["ms"]
        d["ms_n"] += 1
        try:
            status_int = int(r["status"])
            if status_int >= 400:
                d["errors"] += 1
                d["error_breakdown"][f"{r['method']} → {r['status']}"] += 1
        except ValueError:
            pass

    # noch offene Sessions abschliessen (z. B. die aktuelle laufende heute)
    for pool, sess in list(open_sessions.items()):
        if sess["active"]:
            days[sess["date"]]["sessions_with_tools"] += 1

    return dict(days)


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def _aggregate_to_json(agg: dict) -> dict:
    return {
        **{k: agg[k] for k in ("total", "setup", "tool_calls", "other",
                                "sessions", "sessions_with_tools",
                                "ms_total", "ms_n", "errors")},
        "tools": dict(agg["tools"]),
        "methods": dict(agg["methods"]),
        "apps": dict(agg["apps"]),
        "apps_tool_calls": dict(agg["apps_tool_calls"]),
        "apps_setup_calls": dict(agg["apps_setup_calls"]),
        "apps_all_calls": dict(agg["apps_all_calls"]),
        "apps_last_ts": dict(agg["apps_last_ts"]),
        "hours_tools": list(agg["hours_tools"]),
        "hours_setup": list(agg["hours_setup"]),
        "hours_all": list(agg["hours_all"]),
        "error_breakdown": dict(agg["error_breakdown"]),
    }


def _fix_24(values: Any) -> list[int]:
    """Sicherstellen, dass es genau 24 Eintraege sind."""
    if not values:
        return [0] * 24
    lst = list(values)[:24]
    return lst + [0] * max(0, 24 - len(lst))


def _aggregate_from_json(d: dict) -> dict:
    """Robustes Laden — fehlende Felder (alte Cache-Versionen) werden
    default-initialisiert. Stundenarrays werden auf 24 normalisiert."""
    agg = _empty_day()
    for k in ("total", "setup", "tool_calls", "other", "sessions",
              "sessions_with_tools", "ms_total", "ms_n", "errors"):
        agg[k] = int(d.get(k, 0))
    agg["tools"] = Counter(d.get("tools") or {})
    agg["methods"] = Counter(d.get("methods") or {})
    agg["apps"] = Counter(d.get("apps") or {})
    agg["apps_tool_calls"] = Counter(d.get("apps_tool_calls") or {})
    agg["apps_setup_calls"] = Counter(d.get("apps_setup_calls") or {})
    agg["apps_all_calls"] = Counter(d.get("apps_all_calls") or {})
    agg["apps_last_ts"] = dict(d.get("apps_last_ts") or {})
    agg["hours_tools"] = _fix_24(d.get("hours_tools"))
    agg["hours_all"] = _fix_24(d.get("hours_all"))
    # hours_setup koennte in alten Caches fehlen — herleiten aus all - tools.
    if "hours_setup" in d:
        agg["hours_setup"] = _fix_24(d["hours_setup"])
    else:
        agg["hours_setup"] = [a - t for a, t in
                              zip(agg["hours_all"], agg["hours_tools"])]
    agg["error_breakdown"] = Counter(d.get("error_breakdown") or {})
    return agg


def load_cache(path: Path) -> dict[str, dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Cache nicht lesbar (%s) — starte mit leerem Cache.", exc)
        return {}
    if not isinstance(raw, dict) or "days" not in raw:
        return {}
    return {d: _aggregate_from_json(v) for d, v in raw["days"].items()
            if isinstance(v, dict)}


def save_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 2,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "days": {d: _aggregate_to_json(v) for d, v in cache.items()},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def refresh_cache(cache_path: Path, log_path: Path,
                  today: Optional[str] = None) -> dict[str, dict]:
    today = today or date.today().isoformat()
    cache = load_cache(cache_path)
    live = aggregate_per_day(parse_log(log_path))

    dirty = False
    for d, agg in live.items():
        if d < today:
            old = cache.get(d)
            new_json = _aggregate_to_json(agg)
            if old is None or _aggregate_to_json(old) != new_json:
                cache[d] = agg
                dirty = True
    if dirty:
        try:
            save_cache(cache_path, cache)
        except OSError as exc:
            log.warning("Cache konnte nicht geschrieben werden: %s", exc)

    combined = dict(cache)
    if today in live:
        combined[today] = live[today]
    return combined


# ---------------------------------------------------------------------------
# Aggregator-View ueber alle Tage
# ---------------------------------------------------------------------------


def overall_tools(days: dict[str, dict]) -> Counter:
    c: Counter = Counter()
    for d in days.values():
        c.update(d["tools"])
    return c


def overall_methods(days: dict[str, dict]) -> Counter:
    c: Counter = Counter()
    for d in days.values():
        c.update(d["methods"])
    return c


def overall_apps(days: dict[str, dict]) -> dict[str, dict]:
    """Pro KI-Client: Sessions, Tool-Calls, Setup-Calls, letzte Sitzung."""
    out: dict[str, dict] = defaultdict(lambda: {
        "sessions": 0, "tool_calls": 0, "setup_calls": 0, "last": None,
    })
    for day_data in days.values():
        for app, n in day_data["apps"].items():
            out[app]["sessions"] += n
        for app, n in day_data["apps_tool_calls"].items():
            out[app]["tool_calls"] += n
        for app, n in day_data["apps_setup_calls"].items():
            out[app]["setup_calls"] += n
        for app, ts_str in day_data["apps_last_ts"].items():
            try:
                ts = datetime.fromisoformat(ts_str)
            except ValueError:
                continue
            cur = out[app]["last"]
            if cur is None or ts > cur:
                out[app]["last"] = ts
    return dict(out)


def overall_hours(days: dict[str, dict]) -> tuple[list[int], list[int]]:
    tools = [0] * 24
    setup = [0] * 24
    for d in days.values():
        for h in range(24):
            tools[h] += d["hours_tools"][h]
            setup[h] += d["hours_setup"][h]
    return tools, setup


# ---------------------------------------------------------------------------
# HTML-Rendering
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #f5f4ee; --panel: #fdfcf6; --ink: #1f2125; --muted: #5a5c63;
  --accent: #34507e; --accent-2: #6c8cbf; --line: #d8d6c8; --soft: #ecead8;
  --accent-light: #b5c5dc;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: Georgia, "Times New Roman", serif;
  background: radial-gradient(circle at top left, rgba(108,140,191,.18), transparent 28rem),
              linear-gradient(180deg, #f8f7ee 0%, var(--bg) 100%);
  color: var(--ink); }
main { max-width: 92rem; margin: 0 auto; padding: 2rem 1rem 3rem; }
.eyebrow { letter-spacing: .08em; text-transform: uppercase;
  color: var(--accent); font-size: .82rem; font-weight: 700; }
h1 { font-size: clamp(2rem,5vw,3.4rem); line-height: 1; margin: .4rem 0 1.2rem; }
.subtitle { color: var(--muted); margin: 0 0 2rem; font-size: 1.05rem; }
.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
  gap: 1rem; margin: 0 0 2rem; }
.kpi { background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 1rem 1.2rem; }
.kpi .v { font-size: 2.1rem; font-weight: 700; color: var(--ink);
  font-variant-numeric: tabular-nums; }
.kpi .l { color: var(--muted); font-size: .92rem; margin-top: .1rem; }
.panel { background: var(--panel); border: 1px solid var(--line);
  border-radius: 12px; padding: 1.4rem 1.6rem; margin-bottom: 1.4rem;
  box-shadow: 0 2px 14px rgba(60,50,30,.05); }
.panel h2 { margin: 0 0 .3rem; font-size: 1.25rem; }
.panel .lead { color: var(--muted); margin: 0 0 1rem; font-size: .94rem; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: .55rem .55rem; border-bottom: 1px dotted var(--line);
  font-size: .94rem; text-align: left; vertical-align: middle; }
th { color: var(--accent); font-weight: 600; font-size: .78rem;
  text-transform: uppercase; letter-spacing: .04em; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.day { font-weight: 600; }
td.muted { color: var(--muted); }
td.spark { padding: 0 .4rem; }
.bar { height: .55rem; background: var(--accent); border-radius: 3px;
  display: inline-block; vertical-align: middle; }
.bar.alt { background: var(--accent-2); }
.bar-cell { width: 12rem; }
.bar-stack { display: inline-flex; vertical-align: middle; height: .55rem;
  border-radius: 3px; overflow: hidden; background: transparent; }
.bar-stack .seg { height: 100%; }
.bar-stack .seg.tool { background: var(--accent); }
.bar-stack .seg.setup { background: var(--accent-light); }
.bar-stack .seg.rest { background: var(--soft); }
.has-tooltip { border-bottom: 1px dotted var(--muted); cursor: help; }
.client-list { font-size: .92rem; line-height: 1.45; }
.client-list b { color: var(--accent); font-weight: 600;
  font-size: .74rem; text-transform: uppercase; letter-spacing: .03em;
  margin-right: .4rem; }

/* Tagesübersicht: pro Tag 4-zeiliges Layout via tbody-Gruppen. */
table.days { table-layout: fixed; width: 100%; }
table.days tbody.day-group { border-top: 1px solid var(--line); }
table.days tbody.day-group:first-of-type { border-top: none; }
table.days tr.day-main td { padding: .7rem .55rem .25rem;
  border-bottom: none; }
table.days tr.day-detail td { padding: .12rem .55rem; font-size: .94rem;
  border-bottom: none; color: var(--ink); white-space: normal; }
table.days tr.day-detail.last td { padding-bottom: .7rem; }
table.days tr.day-detail td .label { color: var(--accent);
  font-size: .72rem; text-transform: uppercase; letter-spacing: .04em;
  font-weight: 600; margin-right: .8rem; display: inline-block;
  min-width: 11rem; }
table.days th.col-spark { white-space: normal; line-height: 1.15; }
table.days td.spark { padding: .3rem .4rem; }
table.list-table { table-layout: auto; width: 100%; }
.tool-name { font-weight: 500; }
code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: .92rem; color: var(--ink); }
footer { margin-top: 2rem; font-size: .88rem; color: var(--muted); }
.empty { color: var(--muted); font-style: italic; padding: 1rem 0; }
svg.sparkline { display: block; }
.legend { font-size: .82rem; color: var(--muted); margin-top: .4rem; }
.legend .swatch { display: inline-block; width: .8rem; height: .55rem;
  border-radius: 2px; vertical-align: middle; margin-right: .25rem; }
.legend .swatch.tool { background: var(--accent); }
.legend .swatch.setup { background: var(--accent-light); }
"""

PAGE = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>entscheidsuche-MCP — Nutzungsstatistik</title>
<style>{css}</style></head><body><main>
  <div class="eyebrow">entscheidsuche.ch · MCP</div>
  <h1>Nutzungsstatistik</h1>
  <p class="subtitle">Stand: {generated}</p>
  <div class="kpis">
    <div class="kpi"><div class="v">{today_tool_calls}</div><div class="l">heute · Tool-Aufrufe</div></div>
    <div class="kpi"><div class="v">{today_sessions}</div><div class="l">heute · Sessions</div></div>
    <div class="kpi"><div class="v">{week_tool_calls}</div><div class="l">letzte 7&nbsp;Tage · Tool-Aufrufe</div></div>
    <div class="kpi"><div class="v">{total_tool_calls}</div><div class="l">gesamt · Tool-Aufrufe</div></div>
  </div>
  <div class="panel"><h2>Tagesübersicht</h2>
    <p class="lead">dunkel&nbsp;=&nbsp;Toolaufrufe,
       hell&nbsp;=&nbsp;Setup</p>{day_table}</div>
  <div class="panel"><h2>Tool-Nutzung</h2>
    <p class="lead">Tools sind die inhaltlichen Anfragen — im Gegensatz zu
       <code>initialize</code>, <code>tools/list</code>, <code>ping</code>,
       <code>notifications/initialized</code>, <code>resources/list</code>,
       <code>prompts/list</code> etc., die zum Protokoll-Setup gehören.</p>
       {tool_table}</div>
  <div class="panel"><h2>KI-Clients</h2>
    <p class="lead">Aus dem <code>clientInfo</code>-Feld der MCP-Handshakes.
       Balken zweifarbig: dunkel = Tool-Aufrufe, hell = Setup-Aufrufe.</p>{app_table}
    <div class="legend"><span class="swatch tool"></span>Tool-Aufrufe
      &nbsp;&nbsp;<span class="swatch setup"></span>Setup-Aufrufe</div></div>
  <div class="panel"><h2>Tagesübergreifende Aktivität nach Stunde</h2>
    <p class="lead">Tool-Aufrufe (dunkelblau) und Setup (heller) über alle Tage, Server-Zeit.</p>{hours_chart}</div>
  <div class="panel" style="opacity:.85"><h2>Methoden-Verteilung (technisch)</h2>
    <p class="lead">Setup-Methoden sind MCP-Protokoll-Overhead — pro neuem Chat einmalig.</p>{method_table}</div>
</main></body></html>
"""


# ---------------------------------------------------------------------------
# SVG-Helper
# ---------------------------------------------------------------------------


def _stacked_sparkline_svg(tools: list[int], setup: list[int],
                           width: int = 144, height: int = 28) -> str:
    """24-Spalten-Sparkline mit Tool (dunkel, unten) + Setup (hell, oben)."""
    n = len(tools) or 1
    maxv = max((t + s) for t, s in zip(tools, setup)) or 1
    bar_w = width / n
    parts = []
    for i in range(n):
        t = tools[i]
        s = setup[i]
        total = t + s
        if total == 0:
            continue
        h_total = max(1.0, (total / maxv) * (height - 2))
        h_tool = (t / maxv) * (height - 2)
        x = i * bar_w + 0.5
        if s > 0:
            parts.append(f'<rect x="{x:.2f}" y="{height-h_total:.2f}" '
                         f'width="{bar_w-1:.2f}" height="{h_total - h_tool:.2f}" '
                         f'fill="var(--accent-light)" rx="1"/>')
        if t > 0:
            parts.append(f'<rect x="{x:.2f}" y="{height-h_tool:.2f}" '
                         f'width="{bar_w-1:.2f}" height="{h_tool:.2f}" '
                         f'fill="var(--accent)" rx="1"/>')
    title = "Stunden — Tool/Setup: " + ", ".join(
        f"{h:02d}h:{t}+{s}" for h, (t, s) in enumerate(zip(tools, setup))
        if (t + s) > 0
    )
    return (f'<svg class="sparkline" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            f'<title>{html.escape(title)}</title>' + "".join(parts) + "</svg>")


def _stacked_hours_svg(tools: list[int], setup: list[int],
                       width: int = 760, height: int = 180) -> str:
    n = 24
    maxv = max((t + s) for t, s in zip(tools, setup)) or 1
    bar_w = (width - 40) / n
    inner = []
    for i in range(4):
        y = 20 + i * ((height - 50) / 3)
        v = int(maxv - i * maxv / 3)
        inner.append(f'<line x1="35" y1="{y:.1f}" x2="{width-5}" y2="{y:.1f}" '
                     f'stroke="var(--soft)" stroke-width="1"/>')
        inner.append(f'<text x="30" y="{y+3:.1f}" text-anchor="end" '
                     f'font-size="10" fill="var(--muted)">{v}</text>')
    for i in range(n):
        t, s = tools[i], setup[i]
        h_total = ((t + s) / maxv) * (height - 50)
        h_tools = (t / maxv) * (height - 50)
        x = 38 + i * bar_w + 1
        y0 = height - 30
        if s > 0:
            inner.append(f'<rect x="{x:.2f}" y="{y0 - h_total:.2f}" '
                         f'width="{bar_w-2:.2f}" height="{h_total - h_tools:.2f}" '
                         f'fill="var(--accent-light)" rx="1"/>')
        if t > 0:
            inner.append(f'<rect x="{x:.2f}" y="{y0 - h_tools:.2f}" '
                         f'width="{bar_w-2:.2f}" height="{h_tools:.2f}" '
                         f'fill="var(--accent)" rx="1"/>')
        if i % 3 == 0:
            inner.append(f'<text x="{x + bar_w/2:.2f}" y="{height-12}" '
                         f'text-anchor="middle" font-size="10" '
                         f'fill="var(--muted)">{i:02d}h</text>')
    return (f'<svg width="100%" height="{height}" viewBox="0 0 {width} {height}" '
            f'preserveAspectRatio="xMidYMid meet" role="img">'
            + "".join(inner) + "</svg>")


def _stacked_inline_bar(tool: int, setup: int, max_total: int,
                        width_rem: float = 12.0) -> str:
    """Inline-Bar mit zwei Segmenten: dunkel = Tool, hell = Setup.
    Gesamtbreite proportional zu (tool+setup)/max_total — staerkste
    Clients fuellen den Container, schwaechere bleiben kuerzer."""
    if max_total <= 0:
        return ""
    tool_pct = 100 * tool / max_total
    setup_pct = 100 * setup / max_total
    parts = []
    if tool_pct > 0:
        parts.append(f'<span class="seg tool" style="width:{tool_pct:.2f}%"></span>')
    if setup_pct > 0:
        parts.append(f'<span class="seg setup" style="width:{setup_pct:.2f}%"></span>')
    return (f'<span class="bar-stack" style="width:{width_rem}rem">'
            + "".join(parts) + '</span>')


# ---------------------------------------------------------------------------
# Tabellen-Render-Helper
# ---------------------------------------------------------------------------


def _tooltip_text(parts: list[tuple[str, int]], max_items: Optional[int] = None) -> str:
    """Mehrzeiliger Tooltip-Text (durch Newlines getrennt) — wird im title-
    Attribut sauber angezeigt."""
    if max_items:
        parts = parts[:max_items]
    return "\n".join(f"{name}: {n}" for name, n in parts)


def _top_n(counter: Counter, n: int = 3) -> list[tuple[str, int]]:
    return counter.most_common(n)


def _format_client_list(items: list[tuple[str, int]]) -> str:
    return ", ".join(f"{html.escape(name)} ({n})" for name, n in items)


def _render_day_table(days: dict[str, dict]) -> str:
    if not days:
        return ('<div class="empty">Noch keine Daten — der Server hat seit '
                'Aktivierung des Access-Logs keine Anfragen erhalten.</div>')
    max_tc = max((d["tool_calls"] for d in days.values()), default=0) or 1
    out = ["<table class='days'>"
           "<colgroup>"
           "<col style='width:7.5rem'>"          # Tag
           "<col style='width:7rem'>"            # Tool-Calls
           "<col>"                                # Stundenverteilung (auto)
           "<col style='width:7rem'>"            # Sessions
           "<col style='width:4rem'>"            # Ø ms
           "<col style='width:4rem'>"            # Fehler
           "</colgroup>"
           "<thead><tr>"
           "<th>Tag</th>"
           "<th class='num'>Tool-<br>Calls</th>"
           "<th class='col-spark'>Stunden-<br>verteilung</th>"
           "<th class='num'>Sessions<br>"
           "<span style='font-weight:normal;text-transform:none'>"
           "(gesamt/Tool)</span></th>"
           "<th class='num'>Ø&nbsp;ms</th>"
           "<th class='num'>Fehler</th>"
           "</tr></thead>"]

    for day in sorted(days.keys(), reverse=True):
        d = days[day]
        avg_ms = round(d["ms_total"] / d["ms_n"]) if d["ms_n"] else 0
        bar_w = 100 * d["tool_calls"] / max_tc
        spark = _stacked_sparkline_svg(d["hours_tools"], d["hours_setup"])

        # Tools (Tool-Requests) mit Tooltip
        top_tools = d["tools"].most_common(3)
        all_tools = list(d["tools"].most_common())
        if top_tools:
            top_tools_str = ", ".join(
                f"<span class='tool-name'>{html.escape(name)}</span> "
                f"<span class='muted'>({n})</span>"
                for name, n in top_tools
            )
            if len(all_tools) > 3:
                tools_tt = _tooltip_text(all_tools)
                top_tools_html = (
                    f'<span class="has-tooltip" '
                    f'title="{html.escape(tools_tt)}">{top_tools_str}</span>'
                )
            else:
                top_tools_html = top_tools_str
        else:
            top_tools_html = '<span class="muted">—</span>'

        # KI-Clients: Top 3 nach Tool-Calls + Top 3 nach Total (zwei Zeilen)
        top_tool_clients = _top_n(d["apps_tool_calls"], 3)
        top_total_clients = _top_n(d["apps_all_calls"], 3)

        def _client_tooltip(counter: Counter) -> str:
            return _tooltip_text(list(counter.most_common()))

        if top_tool_clients:
            tool_clients_str = _format_client_list(top_tool_clients)
            if len(d["apps_tool_calls"]) > 3:
                tt = _client_tooltip(d["apps_tool_calls"])
                tool_clients_html = (f'<span class="has-tooltip" '
                                     f'title="{html.escape(tt)}">'
                                     f'{tool_clients_str}</span>')
            else:
                tool_clients_html = tool_clients_str
        else:
            tool_clients_html = '<span class="muted">—</span>'

        if top_total_clients:
            total_clients_str = _format_client_list(top_total_clients)
            if len(d["apps_all_calls"]) > 3:
                tt = _client_tooltip(d["apps_all_calls"])
                total_clients_html = (f'<span class="has-tooltip" '
                                      f'title="{html.escape(tt)}">'
                                      f'{total_clients_str}</span>')
            else:
                total_clients_html = total_clients_str
        else:
            total_clients_html = '<span class="muted">—</span>'

        # Fehler mit Tooltip
        if d["errors"] > 0:
            err_tt = _tooltip_text(list(d["error_breakdown"].most_common()))
            errors_html = (
                f'<span class="has-tooltip" title="{html.escape(err_tt)}">'
                f'{d["errors"]}</span>'
            )
        else:
            errors_html = "0"

        out.append(
            f"<tbody class='day-group'>"
            f"<tr class='day-main'>"
            f"<td class='day' rowspan='4'>{html.escape(day)}</td>"
            f"<td class='num'><span class='bar' "
            f"style='width:{bar_w*0.7:.1f}%;margin-right:.4rem'></span>"
            f"{d['tool_calls']}</td>"
            f"<td class='spark'>{spark}</td>"
            f"<td class='num'>{d['sessions']}&nbsp;/&nbsp;"
            f"<strong>{d['sessions_with_tools']}</strong></td>"
            f"<td class='num muted'>{avg_ms}</td>"
            f"<td class='num muted'>{errors_html}</td>"
            f"</tr>"
            f"<tr class='day-detail'>"
            f"<td colspan='5'>"
            f"<span class='label'>Tool-Requests</span>{top_tools_html}"
            f"</td></tr>"
            f"<tr class='day-detail'>"
            f"<td colspan='5'>"
            f"<span class='label'>Clients (nur tools)</span>{tool_clients_html}"
            f"</td></tr>"
            f"<tr class='day-detail last'>"
            f"<td colspan='5'>"
            f"<span class='label'>Clients (alle)</span>{total_clients_html}"
            f"</td></tr>"
            f"</tbody>"
        )
    out.append("</table>")
    return "".join(out)


def _render_tool_table(tools: Counter) -> str:
    if not tools:
        return '<div class="empty">Noch keine Tool-Aufrufe registriert.</div>'
    max_v = tools.most_common(1)[0][1]
    rows = ["<table class='list-table'>"
            "<colgroup><col><col style='width:5rem'><col style='width:14rem'></colgroup>"
            "<thead><tr><th>Tool</th><th class='num'>Calls</th>"
            "<th class='bar-cell'>Anteil</th></tr></thead><tbody>"]
    for name, n in tools.most_common(20):
        w = 100 * n / max_v
        rows.append(f"<tr><td><span class='tool-name'>"
                    f"{html.escape(name)}</span></td>"
                    f"<td class='num'>{n}</td>"
                    f"<td class='bar-cell'><span class='bar alt' "
                    f"style='width:{w:.1f}%'></span></td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_app_table(apps: dict[str, dict]) -> str:
    if not apps:
        return '<div class="empty">Noch keine identifizierten KI-Clients.</div>'
    max_total = max((a["tool_calls"] + a["setup_calls"]) for a in apps.values()) or 1
    items = sorted(apps.items(),
                   key=lambda x: -(x[1]["tool_calls"] + x[1]["setup_calls"]))
    rows = ["<table class='list-table'>"
            "<colgroup>"
            "<col><col style='width:6rem'><col style='width:5rem'>"
            "<col style='width:6rem'><col style='width:14rem'>"
            "<col style='width:11rem'>"
            "</colgroup>"
            "<thead><tr>"
            "<th>KI-Client</th>"
            "<th class='num'>Tool-Calls</th>"
            "<th class='num'>Setup</th>"
            "<th class='num'>Sessions</th>"
            "<th class='bar-cell'>Anteil (Tool/Setup)</th>"
            "<th>Letzte Sitzung</th>"
            "</tr></thead><tbody>"]
    for name, info in items:
        bar = _stacked_inline_bar(info["tool_calls"], info["setup_calls"],
                                  max_total)
        last = info["last"].strftime("%Y-%m-%d %H:%M") if info["last"] else "—"
        rows.append(
            f"<tr><td><strong>{html.escape(name)}</strong></td>"
            f"<td class='num'>{info['tool_calls']}</td>"
            f"<td class='num muted'>{info['setup_calls']}</td>"
            f"<td class='num muted'>{info['sessions']}</td>"
            f"<td class='bar-cell'>{bar}</td>"
            f"<td class='muted'>{html.escape(last)}</td></tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_method_table(methods: Counter) -> str:
    if not methods:
        return '<div class="empty">Noch keine Methoden registriert.</div>'
    max_v = methods.most_common(1)[0][1]
    rows = ["<table class='list-table'>"
            "<colgroup><col><col style='width:5rem'><col style='width:14rem'></colgroup>"
            "<thead><tr><th>Methode</th><th class='num'>Calls</th>"
            "<th class='bar-cell'>Anteil</th></tr></thead><tbody>"]
    for name, n in methods.most_common():
        w = 100 * n / max_v
        cls = "" if name == "tools/call" else " class='muted'"
        rows.append(f"<tr{cls}><td><span class='tool-name'>"
                    f"{html.escape(name)}</span></td>"
                    f"<td class='num'>{n}</td>"
                    f"<td class='bar-cell'><span class='bar' "
                    f"style='width:{w:.1f}%'></span></td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def render_html(days: dict[str, dict], log_path: Path,
                cache_path: Path) -> str:
    today = date.today().isoformat()
    week_cutoff = (date.today() - timedelta(days=6)).isoformat()
    today_d = days.get(today, {})
    week_tc = sum(d["tool_calls"] for k, d in days.items() if k >= week_cutoff)
    tools_h, setup_h = overall_hours(days)
    return PAGE.format(
        css=CSS,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        today_tool_calls=today_d.get("tool_calls", 0),
        today_sessions=today_d.get("sessions", 0),
        week_tool_calls=week_tc,
        total_tool_calls=sum(d["tool_calls"] for d in days.values()),
        day_table=_render_day_table(days),
        tool_table=_render_tool_table(overall_tools(days)),
        app_table=_render_app_table(overall_apps(days)),
        hours_chart=_stacked_hours_svg(tools_h, setup_h),
        method_table=_render_method_table(overall_methods(days)),
    )


# ---------------------------------------------------------------------------
# Basic Auth + Starlette-Endpoint
# ---------------------------------------------------------------------------


def _unauthorized() -> Response:
    return Response(
        "Authentication required.\n", status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="entscheidsuche-MCP Statistik"'},
    )


def check_basic_auth(request: Request) -> Optional[Response]:
    user = os.environ.get("ESC_STATS_USER", "").strip()
    password = os.environ.get("ESC_STATS_PASS", "")
    if not user or not password:
        return None
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return _unauthorized()
    try:
        creds = base64.b64decode(header.split(None, 1)[1], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return _unauthorized()
    given_user, sep, given_pass = creds.partition(":")
    if not sep:
        return _unauthorized()
    if not (hmac.compare_digest(given_user, user) and
            hmac.compare_digest(given_pass, password)):
        return _unauthorized()
    return None


def _env_path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or str(default)).expanduser()


async def statistik_endpoint(request: Request) -> Response:
    auth = check_basic_auth(request)
    if auth is not None:
        return auth
    log_path = _env_path("ESC_ACCESS_LOG_FILE", DEFAULT_LOG)
    cache_path = _env_path("ESC_STATS_CACHE", DEFAULT_CACHE)
    days = refresh_cache(cache_path, log_path)
    body = render_html(days, log_path, cache_path)
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})
