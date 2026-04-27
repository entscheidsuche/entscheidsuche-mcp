"""FastMCP-Server für entscheidsuche.ch.

Stellt die Entscheidsuche als MCP-Tools über Streamable HTTP bereit.

Tools:
    * search           — Volltext-Suche mit Filtern, Sortierung, Paginierung
    * get_document     — Einzelnes Dokument anhand der ID
    * list_hierarchy   — Hierarchie-Buckets mit Trefferzahlen
    * list_facets      — Hierarchischer Facetten-Baum (Kanton/Gericht/Kammer)
    * server_info      — Endpunkt-Konfiguration und Versionsinfo

Aufruf via:

    python -m entscheidsuche_mcp                    # Streamable HTTP auf 127.0.0.1:8765/mcp
    python -m entscheidsuche_mcp --transport stdio  # für lokale CLI-Clients
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import date as date_type
from typing import Annotated, Any, AsyncIterator, List, Optional

from mcp.server.fastmcp import Context, FastMCP
from pydantic import Field

from . import __version__
from .models import (
    DateRange,
    HierarchyResponse,
    Language,
    SearchHit,
    SearchParams,
    SearchResponse,
    SortOrder,
)
from .search import EntscheidsucheClient

logger = logging.getLogger(__name__)


DEFAULT_ES_URL = "https://entscheidsuche.pansoft.de:9200/entscheidsuche.v2-*/_search"
DEFAULT_FACETS_URL = "https://www.recherche.histoirerurale.ch/Facetten.json"


def _env(name: str, default: str) -> str:
    val = os.environ.get(name)
    return val if val else default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() not in {"0", "false", "no", "off"}


@asynccontextmanager
async def _lifespan(app: FastMCP) -> AsyncIterator[dict[str, Any]]:
    """Verwaltet den HTTP-Client über die Lebensdauer des Servers."""
    es_url = _env("ENTSCHEIDSUCHE_ES_URL", DEFAULT_ES_URL)
    facets_url = _env("ENTSCHEIDSUCHE_FACETS_URL", DEFAULT_FACETS_URL)
    timeout = float(_env("HTTP_TIMEOUT", "30"))
    verify_ssl = _env_bool("ENTSCHEIDSUCHE_VERIFY_SSL", True)
    client = EntscheidsucheClient(
        es_url=es_url,
        facets_url=facets_url,
        timeout=timeout,
        verify_ssl=verify_ssl,
    )
    logger.info("entscheidsuche-mcp %s — ES=%s", __version__, es_url)
    try:
        yield {"client": client}
    finally:
        await client.aclose()


def _coerce_date_range(
    from_: Optional[date_type | str], to: Optional[date_type | str]
) -> Optional[DateRange]:
    if from_ is None and to is None:
        return None
    return DateRange(**{"from": from_, "to": to})


def _quote_as_phrase(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').strip()
    return f'"{escaped}"'


def build_server() -> FastMCP:
    """Erzeugt die FastMCP-Instanz mit allen registrierten Tools."""
    mcp = FastMCP(
        name="entscheidsuche",
        instructions=(
            "MCP-Server für entscheidsuche.ch — eine Suchmaschine für Schweizer "
            "Gerichtsentscheide, Gerichtsurteile, Rechtsprechung und Swiss case law. "
            "Die Tools helfen bei Entscheiden des Bundesgerichts (BGer, BGE) sowie "
            "kantonalen Gerichten, Obergerichten, Verwaltungsgerichten und weiteren "
            "Schweizer Gerichten. Unterstützt Volltextsuche, Geschäftsnummern- bzw. "
            "Urteilsnummern-Suche, Volltext-Abruf einzelner Entscheide, Hierarchien "
            "für Kantone und Gerichte sowie Facetten für Filter und Recherche."
        ),
        lifespan=_lifespan,
    )

    def _client(ctx: Context) -> EntscheidsucheClient:
        return ctx.request_context.lifespan_context["client"]

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Entscheide durchsuchen",
        description=(
            "Volltext-Suche in Schweizer Gerichtsurteilen, Gerichtsentscheiden, "
            "Rechtsprechung und Swiss case law aus entscheidsuche.ch. Geeignet für "
            "Entscheide des Bundesgerichts (BGer, BGE) und kantonaler Gerichte wie "
            "Kantonsgericht, Obergericht oder Verwaltungsgericht.\n\n"
            "Typische Anwendungsfälle:\n"
            "  - Bundesgerichtsurteil, BGE oder Gerichtsurteil thematisch suchen\n"
            "  - Schweizer Rechtsprechung zu Mietrecht, ZGB, OR, StGB oder BV finden\n"
            "  - Geschäftsnummern, Urteilsnummern oder Zitate verifizieren\n"
            "  - Verwandte Tools: get_document, search_by_case_number, "
            "list_hierarchy, list_facets\n\n"
            "Suchsyntax (Lucene-Query-String):\n"
            "  - Phrasen-/Geschäftsnummer-Suche: \"BGE 142 III 1\" (mit Anführungszeichen)\n"
            "  - Boolesche Operatoren: AND, OR, NOT (bzw. +, -)\n"
            "  - Wildcards: * und ?\n"
            "  - Default-Operator zwischen Begriffen ist AND.\n\n"
            "Filter werden mit AND verknüpft. Hierarchie-IDs aus `list_hierarchy` "
            "oder `list_facets` übernehmen.\n\n"
            "Paginierung: nach dem ersten Aufruf den `next_cursor`-Wert aus der "
            "Antwort als `search_after` im nächsten Aufruf übergeben."
        ),
        )
    async def search(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                description=(
                    "Volltext-Anfrage. \"...\" für Phrasen, AND/OR/NOT für Boolesche "
                    "Verknüpfungen, * und ? für Wildcards. \"*\" matcht alles."
                ),
            ),
        ] = "*",
        language: Annotated[
            Language,
            Field(description="Optionale Sprache für Highlight/Anzeige (de, fr, it)."),
        ] = Language.de,
        sort: Annotated[
            SortOrder,
            Field(
                description=(
                    "Optionale Sortierung: 'relevance' | 'date' (Entscheiddatum) "
                    "| 'scrapedate'. Default ist 'relevance'."
                ),
            ),
        ] = SortOrder.relevance,
        size: Annotated[
            int, Field(ge=1, le=100, description="Treffer pro Seite (1–100).")
        ] = 20,
        search_after: Annotated[
            Optional[List[Any]],
            Field(description="Cursor aus `next_cursor` der vorigen Antwort."),
        ] = None,
        decision_date_from: Annotated[
            Optional[date_type],
            Field(description="Untergrenze Entscheiddatum (YYYY-MM-DD, inklusive)."),
        ] = None,
        decision_date_to: Annotated[
            Optional[date_type],
            Field(description="Obergrenze Entscheiddatum (YYYY-MM-DD, inklusive)."),
        ] = None,
        scrape_date_from: Annotated[
            Optional[date_type],
            Field(description="Untergrenze Scrape-Datum (YYYY-MM-DD, inklusive)."),
        ] = None,
        scrape_date_to: Annotated[
            Optional[date_type],
            Field(description="Obergrenze Scrape-Datum (YYYY-MM-DD, inklusive)."),
        ] = None,
        hierarchy: Annotated[
            Optional[List[str]],
            Field(
                description=(
                    "Liste von Hierarchie-IDs (Kanton/Gericht/Kammer). Mehrere IDs "
                    "werden mit OR verknüpft; mit anderen Filtern AND."
                ),
            ),
        ] = None,
        language_filter: Annotated[
            Optional[List[Language]],
            Field(description="Filter nach Dokumentsprache(n)."),
        ] = None,
        include_aggregations: Annotated[
            bool,
            Field(description="Aggregationen mitliefern (Verteilungen über die Treffer)."),
        ] = False,
    ) -> SearchResponse:
        params = SearchParams(
            query=query,
            language=language,
            sort=sort,
            size=size,
            search_after=search_after,
            decision_date=_coerce_date_range(decision_date_from, decision_date_to),
            scrape_date=_coerce_date_range(scrape_date_from, scrape_date_to),
            hierarchy=hierarchy,
            language_filter=language_filter,
            include_aggregations=include_aggregations,
        )
        return await _client(ctx).search(params)

    # ------------------------------------------------------------------
    # search_by_case_number
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Nach Geschäftsnummer suchen",
        description=(
            "Spezialsuche für Geschäftsnummern, Urteilsnummern und BGE-Zitate in "
            "Schweizer Gerichtsurteilen. Dieses Tool setzt die angegebene "
            "Geschäftsnummer automatisch in Anführungszeichen und startet damit "
            "eine exakte Phrasensuche, zum Beispiel für 'BGE 142 III 1', "
            "'5A_396/2015' oder ähnliche Referenzen aus Bundesgericht, BGer, BGE "
            "und kantonaler Rechtsprechung."
        ),
    )
    async def search_by_case_number(
        ctx: Context,
        case_number: Annotated[
            str,
            Field(
                description=(
                    "Geschäftsnummer (Aktenzeichen), BGE-Zitat oder Urteilsnummer, z.B. "
                    "'BGE 142 III 1' oder '5A_396/2015'."
                ),
            ),
        ],
        language: Annotated[
            Language,
            Field(description="Optionale Sprache für Highlight/Anzeige (de, fr, it)."),
        ] = Language.de,
        sort: Annotated[
            SortOrder,
            Field(
                description=(
                    "Optionale Sortierung: 'relevance' | 'date' (Entscheiddatum) "
                    "| 'scrapedate'. Default ist 'relevance'."
                ),
            ),
        ] = SortOrder.relevance,
        size: Annotated[
            int, Field(ge=1, le=100, description="Treffer pro Seite (1–100).")
        ] = 20,
        search_after: Annotated[
            Optional[List[Any]],
            Field(description="Cursor aus `next_cursor` der vorigen Antwort."),
        ] = None,
        decision_date_from: Annotated[
            Optional[date_type],
            Field(description="Optionale Untergrenze Entscheiddatum (YYYY-MM-DD)."),
        ] = None,
        decision_date_to: Annotated[
            Optional[date_type],
            Field(description="Optionale Obergrenze Entscheiddatum (YYYY-MM-DD)."),
        ] = None,
        scrape_date_from: Annotated[
            Optional[date_type],
            Field(description="Optionale Untergrenze Scrape-Datum (YYYY-MM-DD)."),
        ] = None,
        scrape_date_to: Annotated[
            Optional[date_type],
            Field(description="Optionale Obergrenze Scrape-Datum (YYYY-MM-DD)."),
        ] = None,
        hierarchy: Annotated[
            Optional[List[str]],
            Field(description="Optionale Hierarchie-IDs für Kanton/Gericht/Kammer."),
        ] = None,
        language_filter: Annotated[
            Optional[List[Language]],
            Field(description="Optionale Filter nach Dokumentsprache(n)."),
        ] = None,
        include_aggregations: Annotated[
            bool,
            Field(description="Optional Aggregationen mitliefern."),
        ] = False,
    ) -> SearchResponse:
        params = SearchParams(
            query=_quote_as_phrase(case_number),
            language=language,
            sort=sort,
            size=size,
            search_after=search_after,
            decision_date=_coerce_date_range(decision_date_from, decision_date_to),
            scrape_date=_coerce_date_range(scrape_date_from, scrape_date_to),
            hierarchy=hierarchy,
            language_filter=language_filter,
            include_aggregations=include_aggregations,
        )
        return await _client(ctx).search(params)

    # ------------------------------------------------------------------
    # fetch_document
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Entscheid abrufen",
        description=(
            "Fetch / retrieve eines einzelnen Schweizer Gerichtsentscheids, "
            "Gerichtsurteils oder Bundesgerichtsentscheids anhand seiner "
            "Dokument-ID (`_id`). Liefert immer den vollständigen Volltext des "
            "Entscheids sowie Metadaten, Titel, Abstract und Links zurück. "
            "Geeignet, wenn nach einer Suche der ganze Entscheid gelesen oder "
            "weiterverarbeitet werden soll."
        ),
    )
    async def fetch_document(
        ctx: Context,
        id: Annotated[
            str,
            Field(description="Dokument-ID (z.B. 'CH_BGer_001_5A_123_2024_2024-06-15')."),
        ],
        language: Annotated[
            Language, Field(description="Sprache für Anzeige.")
        ] = Language.de,
    ) -> Optional[SearchHit]:
        return await _client(ctx).get_document(id, language)

    @mcp.tool(
        title="Einzeldokument abrufen (Alias)",
        description=(
            "Alias für `fetch_document`. Ruft einen einzelnen Entscheid anhand "
            "seiner Dokument-ID ab und liefert immer den vollständigen Volltext "
            "zurück."
        ),
    )
    async def get_document(
        ctx: Context,
        id: Annotated[
            str,
            Field(description="Dokument-ID (z.B. 'CH_BGer_001_5A_123_2024_2024-06-15')."),
        ],
        language: Annotated[
            Language, Field(description="Sprache für Anzeige.")
        ] = Language.de,
    ) -> Optional[SearchHit]:
        return await _client(ctx).get_document(id, language)

    # ------------------------------------------------------------------
    # list_hierarchy
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Hierarchie-Buckets",
        description=(
            "Liefert die verfügbaren Hierarchie-IDs für Schweizer Kantone, "
            "Gerichte und Kammern mit Trefferzahlen. Hilfreich, um Bundesgericht, "
            "kantonale Gerichte, Obergerichte oder Verwaltungsgerichte für die "
            "weitere Suche einzugrenzen."
        ),
    )
    async def list_hierarchy(
        ctx: Context,
        query: Annotated[str, Field(description="Optionale Volltext-Anfrage.")] = "*",
        size: Annotated[
            int, Field(ge=1, le=10000, description="Maximale Anzahl Einträge.")
        ] = 1000,
    ) -> HierarchyResponse:
        return await _client(ctx).list_hierarchy(query, size)

    # ------------------------------------------------------------------
    # list_facets
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Facetten-Baum (lokalisiert)",
        description=(
            "Hierarchischer Facetten-Baum mit lokalisierten Bezeichnungen für "
            "Kantone, Gerichte und Kammern. Die `id`-Felder können im "
            "`hierarchy`-Filter der Suche verwendet werden."
        ),
    )
    async def list_facets(ctx: Context) -> List[dict[str, Any]]:
        nodes = await _client(ctx).list_facets()
        return [n.model_dump() for n in nodes]

    # ------------------------------------------------------------------
    # server_info
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Server-Info",
        description=(
            "Versionsinfo und konfigurierte Endpunkt-URLs des MCP-Servers für "
            "Schweizer Rechtsprechung und Gerichtsentscheide."
        ),
    )
    async def server_info(ctx: Context) -> dict[str, Any]:
        return {
            "name": "entscheidsuche-mcp",
            "version": __version__,
            "elasticsearch_url": _env("ENTSCHEIDSUCHE_ES_URL", DEFAULT_ES_URL),
            "facets_url": _env("ENTSCHEIDSUCHE_FACETS_URL", DEFAULT_FACETS_URL),
            "languages": [l.value for l in Language],
            "sort_orders": [s.value for s in SortOrder],
        }

    return mcp


# ---------------------------------------------------------------------------
# ASGI-Einstieg für `uvicorn entscheidsuche_mcp.server:app`
# ---------------------------------------------------------------------------

_mcp_singleton: Optional[FastMCP] = None


def get_mcp() -> FastMCP:
    """Lazy-erzeugte FastMCP-Instanz (für ASGI-Mount)."""
    global _mcp_singleton
    if _mcp_singleton is None:
        _mcp_singleton = build_server()
    return _mcp_singleton


def create_app():
    """Factory für ASGI-Server (z.B. `uvicorn ...:create_app --factory`)."""
    return get_mcp().streamable_http_app()


# Für direkten ASGI-Mount: `uvicorn entscheidsuche_mcp.server:app`
app = create_app()
