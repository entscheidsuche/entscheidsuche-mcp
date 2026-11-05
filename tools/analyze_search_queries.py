#!/usr/bin/env python3
"""Analysiert Suchmuster im entscheidsuche-mcp-Access-Log.

Aufruf:

    python3 tools/analyze_search_queries.py                # Default-Logpfad
    python3 tools/analyze_search_queries.py /var/log/entscheidsuche-mcp/access.log
    python3 tools/analyze_search_queries.py --json         # maschinenlesbar
    python3 tools/analyze_search_queries.py --days 7       # nur letzte 7 Tage

Ausgabe:

* Gesamtzahl search-/fetch-/search_by_case_number-Calls
* Top 30 Suchbegriffe (nach Haufigkeit)
* Verteilung der Query-Laenge (in Zeichen)
* Null-Treffer-Heuristik: Anteil der Responses mit size < 150 Bytes
  (mangels gelogtem total-Feld nur approximativ)
* Verteilung der Suchparameter (language, sort, size, hierarchy-Filter)
* Anteil "trivialer" Queries (nur ein Wort, keine Operatoren)
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

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
    r"(?:\s+args=(?P<args>.*))?$"
)

DEFAULT_LOG = Path("/var/log/entscheidsuche-mcp/access.log")
ZERO_HIT_SIZE_THRESHOLD = 150  # Bytes — JSON mit total:0 ist meist ~60-120 B

TRIVIAL_OPERATORS = {'"', '*', '?', '~', '^', '(', ')', ':', 'AND', 'OR', 'NOT'}


def iter_lines(path: Path):
    files = sorted(path.parent.glob(path.name + "*"))
    files.sort(key=lambda p: (p.suffix != "", p.name), reverse=True)
    if path in files:
        files.remove(path)
    files = [path] + files
    for p in files:
        if not p.exists():
            continue
        opener = gzip.open if p.suffix == ".gz" else open
        try:
            with opener(p, "rt", encoding="utf-8", errors="replace") as f:
                yield from f
        except OSError:
            continue


def parse_args_field(raw: str) -> dict | None:
    if not raw or raw.endswith("…"):
        # Trunkierte args sind nicht sauber JSON-parsbar — versuch's trotzdem,
        # viele search-Aufrufe sind kurz genug um unter 200 Byte zu bleiben.
        raw = raw.rstrip("…")
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        # Versuch, als trunkiertes JSON zu reparieren (nicht zuverlaessig).
        for n in range(len(raw), 0, -1):
            try:
                return json.loads(raw[:n] + "}")
            except ValueError:
                continue
        return None


def classify_query(q: str) -> str:
    if not q or q == "*":
        return "wildcard"
    if any(op in q for op in TRIVIAL_OPERATORS):
        return "advanced"
    if " " in q.strip():
        return "multi-word"
    return "single-word"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logfile", nargs="?", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--days", type=int, default=0,
                    help="Nur die letzten N Tage (Default: alle)")
    ap.add_argument("--json", action="store_true", help="JSON statt Text")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()

    if not args.logfile.exists():
        print(f"Logfile nicht gefunden: {args.logfile}", file=sys.stderr)
        return 1

    cutoff: str | None = None
    if args.days > 0:
        cutoff = (date.today() - timedelta(days=args.days)).isoformat()

    total_rpcs = 0
    tool_counts: Counter = Counter()
    search_queries: Counter = Counter()
    search_lengths: list[int] = []
    search_zero_hits = 0
    search_with_results = 0
    search_languages: Counter = Counter()
    search_sorts: Counter = Counter()
    search_sizes: Counter = Counter()
    search_with_hierarchy = 0
    search_with_facets = 0
    search_query_classes: Counter = Counter()
    search_by_date: dict[str, int] = defaultdict(int)
    unparsable_args = 0

    fetch_ids: Counter = Counter()
    case_numbers: Counter = Counter()

    for line in iter_lines(args.logfile):
        m = LOG_LINE.match(line)
        if not m:
            continue
        d = m.groupdict()
        if cutoff and d["date"] < cutoff:
            continue
        total_rpcs += 1
        tool = d["tool"]
        if tool == "-":
            continue
        tool_counts[tool] += 1

        raw_args = d.get("args") or ""
        parsed = parse_args_field(raw_args)
        size = int(d["size"])

        if tool == "search":
            if parsed is None:
                unparsable_args += 1
                continue
            q = parsed.get("query", "") or ""
            search_queries[q.strip().lower()] += 1
            search_lengths.append(len(q))
            search_query_classes[classify_query(q)] += 1
            search_by_date[d["date"]] += 1
            if parsed.get("language"):
                search_languages[parsed["language"]] += 1
            if parsed.get("sort"):
                search_sorts[parsed["sort"]] += 1
            if parsed.get("size"):
                search_sizes[str(parsed["size"])] += 1
            if parsed.get("hierarchy"):
                search_with_hierarchy += 1
            if parsed.get("include_aggregations") or parsed.get("include_facets"):
                search_with_facets += 1
            # Null-Treffer-Heuristik: sehr kleine Response.
            # Achtung: SSE-Response hat Overhead. Threshold ist konservativ.
            if size < ZERO_HIT_SIZE_THRESHOLD:
                search_zero_hits += 1
            else:
                search_with_results += 1

        elif tool == "fetch_document":
            if parsed:
                doc_id = parsed.get("id") or parsed.get("document_id")
                if doc_id:
                    fetch_ids[str(doc_id)] += 1

        elif tool == "search_by_case_number":
            if parsed:
                cn = parsed.get("case_number") or parsed.get("query")
                if cn:
                    case_numbers[str(cn).strip().lower()] += 1

    total_search = tool_counts.get("search", 0)
    report = {
        "scope": {
            "logfile": str(args.logfile),
            "days": args.days or "all",
            "cutoff_date": cutoff,
            "parsed_rpcs": total_rpcs,
            "unparsable_search_args": unparsable_args,
        },
        "tools": dict(tool_counts.most_common()),
        "search": {
            "total_calls": total_search,
            "unique_queries": len(search_queries),
            "zero_hit_approx": search_zero_hits,
            "zero_hit_ratio_pct": round(
                100 * search_zero_hits / total_search, 1) if total_search else 0,
            "query_classes": dict(search_query_classes.most_common()),
            "query_length_stats": {
                "min": min(search_lengths) if search_lengths else 0,
                "max": max(search_lengths) if search_lengths else 0,
                "median": (sorted(search_lengths)[len(search_lengths) // 2]
                           if search_lengths else 0),
                "avg": round(sum(search_lengths) / len(search_lengths), 1)
                       if search_lengths else 0,
            },
            "languages": dict(search_languages.most_common()),
            "sorts": dict(search_sorts.most_common()),
            "sizes": dict(search_sizes.most_common(10)),
            "with_hierarchy_filter": search_with_hierarchy,
            "with_facets": search_with_facets,
            "top_queries": search_queries.most_common(args.top),
            "per_day": dict(sorted(search_by_date.items())),
        },
        "fetch_document": {
            "total_calls": tool_counts.get("fetch_document", 0),
            "unique_ids": len(fetch_ids),
            "top_ids": fetch_ids.most_common(10),
        },
        "search_by_case_number": {
            "total_calls": tool_counts.get("search_by_case_number", 0),
            "unique_case_numbers": len(case_numbers),
            "top": case_numbers.most_common(20),
        },
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    s = report["search"]
    print(f"Logfile: {args.logfile}")
    print(f"Zeitraum: {args.days or 'alle'} Tage"
          + (f" (ab {cutoff})" if cutoff else ""))
    print(f"Geparste RPC-Zeilen: {total_rpcs:,}")
    print(f"Nicht parsbare search-args (truncated): {unparsable_args:,}")
    print()
    print("Tool-Nutzung:")
    for name, n in report["tools"].items():
        print(f"  {name:30s} {n:>8,}")
    print()
    print(f"=== search (gesamt: {s['total_calls']:,}) ===")
    print(f"  Unique Queries        : {s['unique_queries']:,}")
    print(f"  Null-Treffer (approx) : {s['zero_hit_approx']:,}"
          f"  ({s['zero_hit_ratio_pct']} %)")
    print(f"  Query-Klassen         : {s['query_classes']}")
    print(f"  Query-Länge           : min={s['query_length_stats']['min']},"
          f" median={s['query_length_stats']['median']},"
          f" avg={s['query_length_stats']['avg']},"
          f" max={s['query_length_stats']['max']}")
    print(f"  Sprachen              : {s['languages']}")
    print(f"  Sortierungen          : {s['sorts']}")
    print(f"  size-Parameter        : {s['sizes']}")
    print(f"  mit hierarchy-Filter  : {s['with_hierarchy_filter']:,}")
    print(f"  mit Facetten          : {s['with_facets']:,}")
    print()
    print(f"Top {args.top} Suchbegriffe:")
    for q, n in s["top_queries"]:
        q_disp = q if len(q) <= 80 else q[:77] + "..."
        print(f"  {n:>6,}  {q_disp!r}")
    print()
    f = report["fetch_document"]
    print(f"=== fetch_document (gesamt: {f['total_calls']:,}) ===")
    print(f"  Unique IDs: {f['unique_ids']:,}")
    print("  Top IDs:")
    for i, n in f["top_ids"]:
        print(f"    {n:>5,}  {i}")
    print()
    c = report["search_by_case_number"]
    print(f"=== search_by_case_number (gesamt: {c['total_calls']:,}) ===")
    print(f"  Unique: {c['unique_case_numbers']:,}")
    for cn, n in c["top"]:
        print(f"    {n:>4,}  {cn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
