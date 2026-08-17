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
from typing import Annotated, Any, AsyncIterator, List, Literal, Optional

from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import __version__
from .access_log import wrap_if_enabled
from .statistik import statistik_endpoint
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
DEFAULT_PUBLIC_BASE_URL = "https://mcp.entscheidsuche.ch"


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


def _public_base_url() -> str:
    return _env("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).rstrip("/")


def _split_csv_env(name: str) -> List[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _allowed_hosts() -> List[str]:
    """Hostnamen, die der DNS-Rebinding-Schutz akzeptiert.

    Default: localhost, 127.0.0.1 und der Host aus `PUBLIC_BASE_URL`. Über
    `MCP_ALLOWED_HOSTS` (kommagetrennt) lassen sich weitere Werte ergänzen.
    """
    hosts: List[str] = ["127.0.0.1", "localhost"]
    base_host = urlparse(_public_base_url()).hostname
    if base_host and base_host not in hosts:
        hosts.append(base_host)
    for extra in _split_csv_env("MCP_ALLOWED_HOSTS"):
        if extra not in hosts:
            hosts.append(extra)
    return hosts


def _allowed_origins() -> List[str]:
    """Origins, die der DNS-Rebinding-Schutz akzeptiert.

    Default: `PUBLIC_BASE_URL` selbst. Über `MCP_ALLOWED_ORIGINS` lässt sich
    die Liste ergänzen (kommagetrennt, vollqualifizierte URLs).
    """
    origins: List[str] = []
    base_url = _public_base_url()
    if base_url:
        origins.append(base_url)
    for extra in _split_csv_env("MCP_ALLOWED_ORIGINS"):
        if extra not in origins:
            origins.append(extra)
    return origins


def _transport_security() -> TransportSecuritySettings:
    """Konfiguriert den DNS-Rebinding-Schutz von FastMCP.

    Über `MCP_DNS_REBINDING_PROTECTION=false` lässt sich der Schutz komplett
    abschalten — z.B. wenn der Server hinter einem CDN/Proxy steht, der den
    Host-Header verändert.
    """
    enabled = _env_bool("MCP_DNS_REBINDING_PROTECTION", True)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=enabled,
        allowed_hosts=_allowed_hosts(),
        allowed_origins=_allowed_origins(),
    )


def _server_card_payload() -> dict[str, Any]:
    base_url = _public_base_url()
    mcp_url = f"{base_url}/mcp"
    return {
        "name": "entscheidsuche",
        "title": "entscheidsuche-mcp",
        "description": "MCP server for Swiss court decisions and case law provided by entscheidsuche.ch.",
        "version": __version__,
        "beta": True,
        "website_url": "https://entscheidsuche.ch",
        "documentation_url": base_url,
        "transports": {
            "streamable_http": {
                "url": mcp_url,
                "stateless": _env_bool("MCP_STATELESS_HTTP", True),
            }
        },
        "capabilities": {
            "tools": True,
            "resources": True,
            "prompts": False,
        },
        "tools": [
            {
                "name": "search",
                "title": "Search Swiss case law",
                "description": "Full-text search across Swiss court decisions and case law.",
            },
            {
                "name": "search_by_case_number",
                "title": "Search by case number",
                "description": "Exact lookup of Swiss case numbers, docket references and BGE citations.",
            },
            {
                "name": "fetch_document",
                "title": "Fetch document",
                "description": "Retrieve a single decision together with its full text.",
            },
            {
                "name": "list_hierarchy",
                "title": "List court hierarchy",
                "description": "List cantons, courts and chambers with counts.",
            },
            {
                "name": "list_facets",
                "title": "List facets",
                "description": "Localized facet tree for courts and jurisdictions.",
            },
            {
                "name": "server_info",
                "title": "Server information",
                "description": "Version and endpoint information for this server.",
            },
        ],
        "resources": [
            {
                "uri": "mcp://server-card.json",
                "name": "server-card",
                "description": "Structured metadata for this MCP server.",
                "mimeType": "application/json",
            }
        ],
    }


def _normalise_mcp_path(path: str) -> str:
    path = (path or "/mcp").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/mcp"


def _mcp_probe_payload(path: str) -> dict[str, Any]:
    base_url = _public_base_url()
    return {
        "name": "entscheidsuche",
        "title": "entscheidsuche-mcp",
        "status": "ok",
        "transport": "streamable-http",
        "endpoint": f"{base_url}{path}",
        "message": (
            "Use POST for MCP JSON-RPC requests. Machine-readable metadata is "
            "available under /.well-known/mcp and /.well-known/mcp/server-card.json."
        ),
    }


def _is_probe_request(scope: dict[str, Any], json_response_mode: bool = False) -> bool:
    """True, wenn die GET-/HEAD-Anfrage offensichtlich eine Browser-/Discovery-Probe ist.

    Echte JSON-RPC-Aufrufe gehen per POST ein und werden hier gar nicht ausgewertet.
    GET mit `text/event-stream` ist der MCP-SSE-Stream — darf nicht als Probe gelten.
    Im `json_response_mode` (= MCP_JSON_RESPONSE=true) sendet der Server keine
    Server-initiierten Notifications; GET /mcp mit `application/json` ist dann
    fast immer ein Discovery-Probe und wird mit JSON beantwortet — sonst würde
    FastMCP 406 zurückliefern ("Not Acceptable: Client must accept text/event-stream"),
    was MCP-Verzeichnisse und SDK-CLI-Heartbeats unnötig brechen lässt.
    """
    if scope.get("method") == "HEAD":
        return True
    if scope.get("method") != "GET":
        return False

    headers = {
        key.decode("latin1").lower(): value.decode("latin1").lower()
        for key, value in scope.get("headers", [])
    }
    accept = headers.get("accept", "")

    # SSE-Streams immer durchreichen — der Client will explizit den
    # Server-initiierten Event-Stream und FastMCP weiss damit umzugehen.
    if "text/event-stream" in accept:
        return False

    # JSON-Response-Modus: keine Server-initiierten Streams. Daher jedes GET
    # mit JSON-Accept (oder ohne) als Discovery-Probe behandeln.
    if json_response_mode:
        return True

    # Klassischer Streamable-HTTP-Modus: JSON-Accept durchreichen, FastMCP
    # entscheidet selbst (typischerweise 406, da kein SSE).
    if "application/json" in accept:
        return False

    # Browser/Connector-Discovery: leerer Accept, */*, oder text/html → Probe.
    return accept in {"", "*/*"} or "text/html" in accept


def _compat_streamable_http_app(mcp_app, mcp_path: str, json_response_mode: bool = False):
    """Hülle um die FastMCP-ASGI-App, die HEAD/GET-Discovery-Probes mit JSON beantwortet."""

    async def app(scope, receive, send):
        if (
            scope.get("type") == "http"
            and scope.get("path") == mcp_path
            and _is_probe_request(scope, json_response_mode=json_response_mode)
        ):
            response = JSONResponse(_mcp_probe_payload(mcp_path))
            await response(scope, receive, send)
            return

        await mcp_app(scope, receive, send)

    return app


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
        transport_security=_transport_security(),
    )

    def _client(ctx: Context) -> EntscheidsucheClient:
        return ctx.request_context.lifespan_context["client"]

    @mcp.resource(
        "mcp://server-card.json",
        name="server-card",
        title="MCP Server Card",
        description="Structured metadata for the entscheidsuche MCP server.",
        mime_type="application/json",
    )
    def server_card_resource() -> dict[str, Any]:
        return _server_card_payload()

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Search decisions",
        description=(
            "Full-text search across Swiss court judgments, court decisions and "
            "case law from entscheidsuche.ch. Covers decisions of the Federal "
            "Supreme Court (BGer, BGE) and cantonal courts such as Kantonsgericht, "
            "Obergericht or Verwaltungsgericht.\n\n"
            "Typical use cases:\n"
            "  - Search Federal Supreme Court judgments, BGE or rulings by topic\n"
            "  - Find Swiss case law on tenancy law, ZGB, OR, StGB or BV\n"
            "  - Verify case numbers, judgment numbers or citations\n"
            "  - Related tools: fetch_document, search_by_case_number, "
            "list_hierarchy, list_facets\n\n"
            "Search syntax (Lucene query string):\n"
            "  - Phrase / case-number search: \"BGE 142 III 1\" (in quotes)\n"
            "  - Boolean operators: AND, OR, NOT (or +, -)\n"
            "  - Wildcards: * and ?\n"
            "  - The default operator between terms is AND.\n\n"
            "Filters are combined with AND. Use hierarchy IDs from `list_hierarchy` "
            "or `list_facets`.\n\n"
            "Pagination: after the first call, pass the `next_cursor` value from the "
            "response as `search_after` in the next call."
        ),
        )
    async def search(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                description=(
                    "Full-text query. \"...\" for phrases, AND/OR/NOT for Boolean "
                    "operators, * and ? for wildcards. \"*\" matches everything."
                ),
            ),
        ] = "*",
        language: Annotated[
            Optional[Language],
            Field(
                description=(
                    "Optional preferred language for highlighting/display (de, fr, "
                    "it). If omitted, the first available language field is returned "
                    "— this does NOT filter; use `language_filter` for that."
                ),
            ),
        ] = None,
        sort: Annotated[
            Literal["relevance", "date", "scrapedate"],
            Field(
                description=(
                    "Optional sort order: 'relevance' | 'date' (decision date) "
                    "| 'scrapedate'. Default is 'relevance'."
                ),
            ),
        ] = "relevance",
        size: Annotated[
            int, Field(ge=1, le=100, description="Results per page (1–100).")
        ] = 20,
        search_after: Annotated[
            Optional[List[Any]],
            Field(description="Cursor from `next_cursor` of the previous response."),
        ] = None,
        decision_date_from: Annotated[
            Optional[date_type],
            Field(description="Lower bound for the decision date (YYYY-MM-DD, inclusive)."),
        ] = None,
        decision_date_to: Annotated[
            Optional[date_type],
            Field(description="Upper bound for the decision date (YYYY-MM-DD, inclusive)."),
        ] = None,
        scrape_date_from: Annotated[
            Optional[date_type],
            Field(description="Lower bound for the scrape date (YYYY-MM-DD, inclusive)."),
        ] = None,
        scrape_date_to: Annotated[
            Optional[date_type],
            Field(description="Upper bound for the scrape date (YYYY-MM-DD, inclusive)."),
        ] = None,
        hierarchy: Annotated[
            Optional[List[str]],
            Field(
                description=(
                    "List of hierarchy IDs (canton/court/chamber). Multiple IDs are "
                    "combined with OR; with other filters, AND."
                ),
            ),
        ] = None,
        language_filter: Annotated[
            Optional[List[Language]],
            Field(description="Filter by document language(s)."),
        ] = None,
        include_aggregations: Annotated[
            bool,
            Field(description="Include aggregations (distributions over the results)."),
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
        title="Search by case number",
        description=(
            "Specialized search for case numbers, judgment numbers and BGE "
            "citations in Swiss court judgments. This tool automatically wraps the "
            "given case number in quotes and runs an exact phrase search, for "
            "example for 'BGE 142 III 1', '5A_396/2015' or similar references from "
            "the Federal Supreme Court (BGer, BGE) and cantonal case law."
        ),
    )
    async def search_by_case_number(
        ctx: Context,
        case_number: Annotated[
            str,
            Field(
                description=(
                    "Case number (docket reference), BGE citation or judgment "
                    "number, e.g. 'BGE 142 III 1' or '5A_396/2015'."
                ),
            ),
        ],
        language: Annotated[
            Optional[Language],
            Field(
                description=(
                    "Optional preferred language for highlighting/display (de, fr, "
                    "it). If omitted, the first available language field is returned "
                    "— this does NOT filter; use `language_filter` for that."
                ),
            ),
        ] = None,
        sort: Annotated[
            Literal["relevance", "date", "scrapedate"],
            Field(
                description=(
                    "Optional sort order: 'relevance' | 'date' (decision date) "
                    "| 'scrapedate'. Default is 'relevance'."
                ),
            ),
        ] = "relevance",
        size: Annotated[
            int, Field(ge=1, le=100, description="Results per page (1–100).")
        ] = 20,
        search_after: Annotated[
            Optional[List[Any]],
            Field(description="Cursor from `next_cursor` of the previous response."),
        ] = None,
        decision_date_from: Annotated[
            Optional[date_type],
            Field(description="Optional lower bound for the decision date (YYYY-MM-DD)."),
        ] = None,
        decision_date_to: Annotated[
            Optional[date_type],
            Field(description="Optional upper bound for the decision date (YYYY-MM-DD)."),
        ] = None,
        scrape_date_from: Annotated[
            Optional[date_type],
            Field(description="Optional lower bound for the scrape date (YYYY-MM-DD)."),
        ] = None,
        scrape_date_to: Annotated[
            Optional[date_type],
            Field(description="Optional upper bound for the scrape date (YYYY-MM-DD)."),
        ] = None,
        hierarchy: Annotated[
            Optional[List[str]],
            Field(description="Optional hierarchy IDs for canton/court/chamber."),
        ] = None,
        language_filter: Annotated[
            Optional[List[Language]],
            Field(description="Optional filter by document language(s)."),
        ] = None,
        include_aggregations: Annotated[
            bool,
            Field(description="Optionally include aggregations."),
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
        title="Fetch decision",
        description=(
            "Fetch a single Swiss court decision, judgment or Federal Supreme "
            "Court decision by its document ID (`_id`). Returns the indexed full "
            "text of the decision together with metadata, title, abstract and "
            "links. Use this when the full decision should be read or processed "
            "after a search. Note: for very long decisions the indexed text is "
            "capped at 100,000 characters; if `text_truncated` is true, the "
            "complete document is available at `document_url`."
        ),
    )
    async def fetch_document(
        ctx: Context,
        id: Annotated[
            str,
            Field(description="Document ID (e.g. 'CH_BGer_001_5A_123_2024_2024-06-15')."),
        ],
        language: Annotated[
            Optional[Language],
            Field(
                description=(
                    "Optional preferred language for title/abstract. If omitted, "
                    "the first available language field is returned."
                ),
            ),
        ] = None,
    ) -> Optional[SearchHit]:
        return await _client(ctx).get_document(id, language)

    # ------------------------------------------------------------------
    # list_hierarchy
    # ------------------------------------------------------------------
    @mcp.tool(
        title="List hierarchy buckets",
        description=(
            "Returns the available hierarchy IDs for Swiss cantons, courts and "
            "chambers together with document counts. Useful for narrowing "
            "subsequent searches to the Federal Supreme Court, cantonal courts, "
            "Obergerichte or Verwaltungsgerichte."
        ),
    )
    async def list_hierarchy(
        ctx: Context,
        query: Annotated[str, Field(description="Optional full-text query.")] = "*",
        size: Annotated[
            int, Field(ge=1, le=10000, description="Maximum number of entries.")
        ] = 1000,
    ) -> HierarchyResponse:
        return await _client(ctx).list_hierarchy(query, size)

    # ------------------------------------------------------------------
    # list_facets
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Facet tree (localized)",
        description=(
            "Hierarchical facet tree with localized labels for cantons, courts "
            "and chambers. The `id` fields can be used in the `hierarchy` filter "
            "of the search."
        ),
    )
    async def list_facets(ctx: Context) -> List[dict[str, Any]]:
        nodes = await _client(ctx).list_facets()
        return [n.model_dump() for n in nodes]

    # ------------------------------------------------------------------
    # server_info
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Server info",
        description=(
            "Version info and configured endpoint URLs of the MCP server for "
            "Swiss case law and court decisions."
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
    """Lazy-erzeugte FastMCP-Instanz (für ASGI-Mount via Module-Level-`app`)."""
    global _mcp_singleton
    if _mcp_singleton is None:
        _mcp_singleton = build_server()
    return _mcp_singleton


def create_app(mcp: Optional[FastMCP] = None):
    """Factory für ASGI-Server.

    Wenn `mcp` übergeben wird, nutzt die App genau diese Instanz — wichtig, damit
    Settings (Pfad, stateless_http etc.), die der Aufrufer vor dem Bauen der App
    konfiguriert hat, auch tatsächlich wirksam werden. Ohne Argument wird die
    Lazy-Singleton-Instanz aus `get_mcp()` verwendet (für `uvicorn …:app`-Aufrufe).

    Wichtig: FastMCP startet ihren `StreamableHTTPSessionManager` im Lifespan der
    Sub-App, die `streamable_http_app()` zurückliefert. Starlette propagiert
    Lifespans von gemounteten Sub-Apps NICHT, also müssen wir den Lifespan
    der inneren App explizit in die äußere App heben — sonst wirft jeder
    Request `RuntimeError: Task group is not initialized`.
    """
    if mcp is None:
        mcp = get_mcp()

    base_url = _public_base_url()
    mcp_path = _normalise_mcp_path(
        getattr(mcp.settings, "streamable_http_path", None) or "/mcp"
    )

    async def well_known_manifest(_request):
        return JSONResponse(
            {
                "name": "entscheidsuche",
                "title": "entscheidsuche-mcp",
                "description": "MCP server for Swiss court decisions and case law.",
                "server_card_url": f"{base_url}/.well-known/mcp/server-card.json",
                "transports": {
                    "streamable_http": {
                        "url": f"{base_url}{mcp_path}",
                    }
                },
            }
        )

    async def well_known_server_card(_request):
        return JSONResponse(_server_card_payload())

    inner_app = mcp.streamable_http_app()
    json_response_mode = bool(getattr(mcp.settings, "json_response", False))
    mcp_app = _compat_streamable_http_app(
        inner_app, mcp_path, json_response_mode=json_response_mode
    )
    mcp_app = wrap_if_enabled(mcp_app, mcp_path=mcp_path)

    @asynccontextmanager
    async def lifespan(_app):
        # Innere Starlette hat `lifespan=lambda app: session_manager.run()` —
        # diesen Kontext hier explizit öffnen, damit der Session-Manager läuft.
        async with inner_app.router.lifespan_context(inner_app):
            yield

    return Starlette(
        routes=[
            Route("/.well-known/mcp", endpoint=well_known_manifest),
            Route("/.well-known/mcp/server-card.json", endpoint=well_known_server_card),
            Route("/statistik", endpoint=statistik_endpoint),
            Route("/statistik/", endpoint=statistik_endpoint),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )


# Module-Level-Convenience für `uvicorn entscheidsuche_mcp.server:app`.
# Nutzt die Lazy-Singleton-Instanz; für direkte CLI-Aufrufe baut `__main__.py`
# die Instanz selbst und übergibt sie an `create_app`.
def _build_module_app():
    return create_app()


app = _build_module_app()
