#!/usr/bin/env python3
"""Smoke-Test: führt einen echten Suchlauf gegen die Live-API aus.

Auf dem Zielserver ausführen:

    python scripts/smoke_test.py

Optional kann ein anderer ES-Endpoint per `ENTSCHEIDSUCHE_ES_URL` gesetzt werden.
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


ES_URL = os.environ.get(
    "ENTSCHEIDSUCHE_ES_URL",
    "https://entscheidsuche.pansoft.de/entscheidsuche.v2-*/_search",
)
FACETS_URL = os.environ.get(
    "ENTSCHEIDSUCHE_FACETS_URL",
    "https://www.recherche.histoirerurale.ch/Facetten.json",
)


async def main() -> int:
    client = EntscheidsucheClient(es_url=ES_URL, facets_url=FACETS_URL)
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
        print("== Facetten-Baum (Top-Ebene):")
        nodes = await client.list_facets()
        for node in nodes:
            print(f"  - {node.id}: {node.label.de}")

        # Phrasen-Suche / Geschäftsnummer
        print()
        print('== Phrasen-Suche: "BGE 142 III 1"')
        resp = await client.search(
            SearchParams(query='"BGE 142 III 1"', language=Language.de, size=3)
        )
        print(f"  Total: {resp.total}")
        for hit in resp.hits[:3]:
            print(f"  - {hit.id}: {hit.title[:120]}")

        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
