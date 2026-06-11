"""Statistik-Endpoint + JSON-Tagescache fuer den entscheidsuche-MCP-Server.

Verarbeitet das Access-Log und liefert eine selbst-enthaltene HTML-Seite
mit Tageszahlen, KI-Client-Klassifizierung, Top-Tools, Methoden-Verteilung
und Stunden-Sparklines. Wird **bei jedem Aufruf live** generiert; Vortage
werden in einer JSON-Cache-Datei festgehalten, so dass beim Aufruf nur
der laufende Tag aus dem Log neu aggregiert werden muss.

Konfiguration via Env-Variablen:

* ``ESC_ACCESS_LOG_FILE`` — Pfad zum Access-Log (Default
  ``/var/log/entscheidsuche-mcp/access.log``)
* ``ESC_STATS_CACHE``     — Pfad zum JSON-Cache (Default
  ``/var/lib/entscheidsuche-mcp/stats-cache.json``)
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
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _empty_day() -> dict[str, Any]:
    return {
        "total": 0, "setup": 0, "tool_calls": 0, "other": 0, "sessions": 0,
        "tools": Counter(), "methods": Counter(), "apps": Counter(),
        "apps_last_ts": {},
        "hours_tools": [0] * 24, "hours_all": [0] * 24,
        "ms_total": 0, "ms_n": 0, "errors": 0,
    }


def aggregate_per_day(rows: list[dict]) -> dict[str, dict]:
    days: dict[str, dict] = defaultdict(_empty_day)
    for r in rows:
        d = days[r["date"]]
        d["total"] += 1
        d["hours_all"][r["hour"]] += 1
        d["methods"][r["method"]] += 1
        if r["method"] == "initialize":
            d["sessions"] += 1
            d["setup"] += 1
            if r["app"] != "-":
                d["apps"][r["app"]] += 1
                d["apps_last_ts"][r["app"]] = r["ts"].isoformat()
        elif r["method"] in SETUP_METHODS:
            d["setup"] += 1
        elif r["method"] == "tools/call":
            d["tool_calls"] += 1
            d["hours_tools"][r["hour"]] += 1
            if r["tool"] != "-":
                d["tools"][r["tool"]] += 1
        else:
            d["other"] += 1
        d["ms_total"] += r["ms"]
        d["ms_n"] += 1
        try:
            if int(r["status"]) >= 400:
                d["errors"] += 1
        except ValueError:
            pass
    return dict(days)


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


def _aggregate_to_json(agg: dict) -> dict:
    return {
        **{k: agg[k] for k in ("total", "setup", "tool_calls", "other",
                                "sessions", "ms_total", "ms_n", "errors")},
        "tools": dict(agg["tools"]),
        "methods": dict(agg["methods"]),
        "apps": dict(agg["apps"]),
        "apps_last_ts": dict(agg["apps_last_ts"]),
        "hours_tools": list(agg["hours_tools"]),
        "hours_all": list(agg["hours_all"]),
    }


def _aggregate_from_json(d: dict) -> dict:
    agg = _empty_day()
    for k in ("total", "setup", "tool_calls", "other", "sessions",
              "ms_total", "ms_n", "errors"):
        agg[k] = int(d.get(k, 0))
    agg["tools"] = Counter(d.get("tools") or {})
    agg["methods"] = Counter(d.get("methods") or {})
    agg["apps"] = Counter(d.get("apps") or {})
    agg["apps_last_ts"] = dict(d.get("apps_last_ts") or {})
    hours_tools = d.get("hours_tools") or [0] * 24
    hours_all = d.get("hours_all") or [0] * 24
    agg["hours_tools"] = list(hours_tools)[:24] + [0] * max(0, 24 - len(hours_tools))
    agg["hours_all"] = list(hours_all)[:24] + [0] * max(0, 24 - len(hours_all))
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
        "schema_version": 1,
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
    out: dict[str, dict] = defaultdict(lambda: {"sessions": 0, "last": None})
    for day_data in days.values():
        for app, n in day_data["apps"].items():
            out[app]["sessions"] += n
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
            setup[h] += d["hours_all"][h] - d["hours_tools"][h]
    return tools, setup


# ---------------------------------------------------------------------------
# HTML-Rendering
# ---------------------------------------------------------------------------

CSS = """
:root {
  --bg: #f5f4ee; --panel: #fdfcf6; --ink: #1f2125; --muted: #5a5c63;
  --accent: #34507e; --accent-2: #6c8cbf; --line: #d8d6c8; --soft: #ecead8;
}
* { box-sizing: border-box; }
body { margin: 0; font-family: Georgia, "Times New Roman", serif;
  background: radial-gradient(circle at top left, rgba(108,140,191,.18), transparent 28rem),
              linear-gradient(180deg, #f8f7ee 0%, var(--bg) 100%);
  color: var(--ink); }
main { max-width: 62rem; margin: 0 auto; padding: 3rem 1.5rem 4rem; }
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
  box-shadow: 0 2px 14px rgba(40,50,80,.05); }
.panel h2 { margin: 0 0 .3rem; font-size: 1.25rem; }
.panel .lead { color: var(--muted); margin: 0 0 1rem; font-size: .94rem; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: .55rem .55rem; border-bottom: 1px dotted var(--line);
  font-size: .96rem; text-align: left; vertical-align: middle; }
th { color: var(--accent); font-weight: 600; font-size: .8rem;
  text-transform: uppercase; letter-spacing: .04em; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.day { font-weight: 600; }
td.muted { color: var(--muted); }
td.spark { padding: 0 .5rem; }
.bar { height: .55rem; background: var(--accent); border-radius: 3px;
  display: inline-block; vertical-align: middle; }
.bar.alt { background: var(--accent-2); }
.bar-cell { width: 14rem; }
code { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: .92rem; color: var(--ink); }
footer { margin-top: 2rem; font-size: .88rem; color: var(--muted); }
.empty { color: var(--muted); font-style: italic; padding: 1rem 0; }
svg.sparkline { display: block; }
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
  <p class="subtitle">Stand: {generated} · live aus <code>{logfile}</code>
     (Vortage aus <code>{cachefile}</code>)</p>
  <div class="kpis">
    <div class="kpi"><div class="v">{today_tool_calls}</div><div class="l">heute · Tool-Aufrufe</div></div>
    <div class="kpi"><div class="v">{today_sessions}</div><div class="l">heute · Sessions</div></div>
    <div class="kpi"><div class="v">{week_tool_calls}</div><div class="l">letzte 7&nbsp;Tage · Tool-Aufrufe</div></div>
    <div class="kpi"><div class="v">{total_tool_calls}</div><div class="l">gesamt · Tool-Aufrufe</div></div>
  </div>
  <div class="panel"><h2>Tagesübersicht</h2>
    <p class="lead">Tool-Aufrufe pro Tag mit Stunden-Sparkline. Eine
       <em>Session</em> = ein <code>initialize</code>-Call, also ein
       neu geöffneter Chat im jeweiligen KI-Client.</p>{day_table}</div>
  <div class="panel"><h2>Top-Tools</h2>
    <p class="lead">Welche entscheidsuche-Werkzeuge wurden aufgerufen?</p>{tool_table}</div>
  <div class="panel"><h2>KI-Clients</h2>
    <p class="lead">Aus dem <code>clientInfo</code>-Feld der MCP-Handshakes.</p>{app_table}</div>
  <div class="panel"><h2>Tagesübergreifende Aktivität nach Stunde</h2>
    <p class="lead">Tool-Aufrufe (dunkelblau) und Setup (heller) über alle Tage, Server-Zeit.</p>{hours_chart}</div>
  <div class="panel" style="opacity:.85"><h2>Methoden-Verteilung (technisch)</h2>
    <p class="lead">Setup-Methoden sind MCP-Protokoll-Overhead — pro neuem Chat einmalig.</p>{method_table}</div>
  <footer>Live-Generierung bei jedem Aufruf. Vortage in JSON-Cache fixiert.
    Reine Aggregate — keine Nutzerinhalte gespeichert
    (<code>ESC_ACCESS_LOG_ARGS_MAX=0</code>).</footer>
</main></body></html>
"""


def _sparkline_svg(values: list[int], width: int = 144, height: int = 28) -> str:
    n = len(values) or 1
    maxv = max(values) or 1
    bar_w = width / n
    parts = []
    for i, v in enumerate(values):
        h = max(1.0, (v / maxv) * (height - 2))
        x = i * bar_w + 0.5
        parts.append(f'<rect x="{x:.2f}" y="{height-h:.2f}" '
                     f'width="{bar_w-1:.2f}" height="{h:.2f}" '
                     f'fill="var(--accent)" rx="1"/>')
    tooltip = "Stunden: " + ", ".join(f"{h:02d}h:{v}" for h, v in enumerate(values))
    return (f'<svg class="sparkline" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}">'
            f'<title>{html.escape(tooltip)}</title>' + "".join(parts) + "</svg>")


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
                         f'fill="var(--accent-2)" opacity="0.5" rx="1"/>')
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


def _render_day_table(days: dict[str, dict]) -> str:
    if not days:
        return ('<div class="empty">Noch keine Daten — der Server hat seit '
                'Aktivierung des Access-Logs keine Anfragen erhalten.</div>')
    max_tc = max((d["tool_calls"] for d in days.values()), default=0) or 1
    rows = ["<table><thead><tr>"
            "<th>Tag</th><th class='num'>Tool-Calls</th>"
            "<th>Stundenverteilung</th>"
            "<th class='num'>Sessions</th><th>Top-Tool</th>"
            "<th>KI-Clients</th><th class='num'>Ø&nbsp;ms</th>"
            "<th class='num muted'>Setup</th>"
            "<th class='num muted'>Fehler</th></tr></thead><tbody>"]
    for day in sorted(days.keys(), reverse=True):
        d = days[day]
        avg_ms = round(d["ms_total"] / d["ms_n"]) if d["ms_n"] else 0
        top = d["tools"].most_common(1)
        top_str = (f"<code>{html.escape(top[0][0])}</code> "
                   f"<span class='muted'>({top[0][1]})</span>") if top else "—"
        apps_str = ", ".join(f"{html.escape(a)} ({n})"
                             for a, n in d["apps"].most_common()) or "—"
        bar_w = 100 * d["tool_calls"] / max_tc
        spark = _sparkline_svg(d["hours_tools"])
        rows.append(
            f"<tr><td class='day'>{html.escape(day)}</td>"
            f"<td class='num'><span class='bar' "
            f"style='width:{bar_w*0.7:.1f}%;margin-right:.4rem'></span>"
            f"{d['tool_calls']}</td>"
            f"<td class='spark'>{spark}</td>"
            f"<td class='num'>{d['sessions']}</td>"
            f"<td>{top_str}</td>"
            f"<td class='muted'>{apps_str}</td>"
            f"<td class='num muted'>{avg_ms}</td>"
            f"<td class='num muted'>{d['setup']}</td>"
            f"<td class='num muted'>{d['errors']}</td></tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_tool_table(tools: Counter) -> str:
    if not tools:
        return '<div class="empty">Noch keine Tool-Aufrufe registriert.</div>'
    max_v = tools.most_common(1)[0][1]
    rows = ["<table><thead><tr><th>Tool</th><th class='num'>Calls</th>"
            "<th class='bar-cell'>Anteil</th></tr></thead><tbody>"]
    for name, n in tools.most_common(20):
        w = 100 * n / max_v
        rows.append(f"<tr><td><code>{html.escape(name)}</code></td>"
                    f"<td class='num'>{n}</td>"
                    f"<td class='bar-cell'><span class='bar alt' "
                    f"style='width:{w:.1f}%'></span></td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_app_table(apps: dict[str, dict]) -> str:
    if not apps:
        return ('<div class="empty">Noch keine identifizierten KI-Clients.</div>')
    max_v = max(a["sessions"] for a in apps.values()) or 1
    items = sorted(apps.items(), key=lambda x: -x[1]["sessions"])
    rows = ["<table><thead><tr><th>KI-Client</th>"
            "<th class='num'>Sessions</th><th class='bar-cell'>Anteil</th>"
            "<th>Letzte Sitzung</th></tr></thead><tbody>"]
    for name, info in items:
        w = 100 * info["sessions"] / max_v
        last = info["last"].strftime("%Y-%m-%d %H:%M") if info["last"] else "—"
        rows.append(f"<tr><td><strong>{html.escape(name)}</strong></td>"
                    f"<td class='num'>{info['sessions']}</td>"
                    f"<td class='bar-cell'><span class='bar' "
                    f"style='width:{w:.1f}%'></span></td>"
                    f"<td class='muted'>{html.escape(last)}</td></tr>")
    rows.append("</tbody></table>")
    return "".join(rows)


def _render_method_table(methods: Counter) -> str:
    if not methods:
        return '<div class="empty">Noch keine Methoden registriert.</div>'
    max_v = methods.most_common(1)[0][1]
    rows = ["<table><thead><tr><th>Methode</th><th class='num'>Calls</th>"
            "<th class='bar-cell'>Anteil</th></tr></thead><tbody>"]
    for name, n in methods.most_common():
        w = 100 * n / max_v
        cls = "" if name == "tools/call" else " class='muted'"
        rows.append(f"<tr{cls}><td><code>{html.escape(name)}</code></td>"
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
        logfile=html.escape(str(log_path)),
        cachefile=html.escape(str(cache_path)),
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
