#!/usr/bin/env python3
"""Smoke-Test: führt einen echten Suchlauf gegen die Live-API aus.

Auf dem Zielserver ausführen:

    python scripts/smoke_test.py

Optional kann eine andere Such-API per `ENTSCHEIDSUCHE_API_BASE` gesetzt werden.
"""

from __future__ import annotations

import asyncio
import os
import sys

# Modulpfad bereitstellen, falls direkt aus dem Repo aufgerufen
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from entscheidsuche_mcp.models import Language, SearchParams, SortOrder  # noqa: E402
from entscheidsuche_mcp.search import EntscheidsucheClient  # noqa: E402


API_BASE = os.environ.get(
    "ENTSCHEIDSUCHE_API_BASE", "https://entscheidsuche.ch/_search_KI.php"
).rstrip("/")
ES_URL = os.environ.get("ENTSCHEIDSUCHE_ES_URL", f"{API_BASE}/elasticsearch/_search")
FACETS_URL = os.environ.get(
    "ENTSCHEIDSUCHE_FACETS_URL",
    "https://entscheidsuche.ch/docs/Facetten.json",
)


async def main() -> int:
    client = EntscheidsucheClient(
        es_url=ES_URL,
        facets_url=FACETS_URL,
        chunk_search_url=f"{API_BASE}/elasticsearch/embeddings_qwen3-embedding-8b/_search",
        micro_chunk_search_url=f"{API_BASE}/elasticsearch/embeddings_qwen3-embedding-0.6b_micro/_search",
        embed_big_url=f"{API_BASE}/embed/big",
        embed_micro_url=f"{API_BASE}/embed/micro",
        index_micro_chunk_url=f"{API_BASE}/indexMicroChunk",
    )
    try:
        print(f"== Suche: 'Mietzins', sort=date, size=3 (Endpoint: {ES_URL})")
        resp = await client.search(
            SearchParams(query="Mietzins", language=Language.de, sort=SortOrder.date, size=3)
        )
        print(f"  Total: {resp.total}")
        for h in resp.hits:
            print(f"  - {h.id}  [{h.canton}/{h.court}]  {h.decision_date}")
            print(f"      {h.title[:120]}")

        print()
        print("== Hierarchie-Buckets (top 5):")
        h = await client.list_hierarchy(query="*", size=5)
        for entry in h.entries:
            print(f"  - {entry.id}: {entry.count}")

        print()
        print("== Facetten-Baum (Kantone mit Anzahl Gerichte / Kammern):")
        nodes = await client.list_facets()
        for node in nodes:
            courts = node.children or []
            chambers = sum(len(c.children or []) for c in courts)
            print(f"  - {node.id}: {node.label.de} — {len(courts)} Gerichte, {chambers} Kammern")
        if not any(node.children for node in nodes):
            print("  FEHLER: Facetten-Baum enthält keine Gerichte")
            return 1

        # Phrasen-Suche / Geschäftsnummer
        print()
        print('== Phrasen-Suche: "BGE 142 III 1"')
        resp = await client.search(
            SearchParams(query='"BGE 142 III 1"', language=Language.de, size=3)
        )
        print(f"  Total: {resp.total}")
        for hit in resp.hits[:3]:
            print(f"  - {hit.id}: {hit.title[:120]}")

        # Anfragen, die früher mit HTTP 400 scheiterten
        print()
        print("== Robuste Anfragen:")
        for query in ("Art. 271 OR", "5A_396/2015", "(Eigenbedarf AND"):
            resp = await client.search(SearchParams(query=query, size=1))
            print(f"  - {query!r}: {resp.total} Treffer" + (" (als Suchbegriffe gelesen)" if resp.note else ""))

        # KI-Suche
        print()
        question = "Darf der Vermieter wegen Eigenbedarf kündigen, wenn seine Tochter einziehen will?"
        print(f"== KI-Suche: {question}")
        resp = await client.semantic_search(question, size=3)
        print(f"  Entscheide: {resp.total}")
        for hit in resp.hits:
            excerpt = " ".join(hit.text.split())[:110]
            print(f"  - {hit.score}  {hit.id}")
            print(f"      {excerpt or '(kein Auszug)'}")

        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
