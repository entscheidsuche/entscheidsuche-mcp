"""FastMCP-Server für entscheidsuche.ch.

Stellt die Entscheidsuche als MCP-Tools über Streamable HTTP bereit.

Tools:
    * search                — Standardsuche: KI-Suche, wechselt bei Geschäftsnummern,
                              Phrasen, Operatoren u.ä. automatisch zur Stichwortsuche
    * semantic_search       — KI-Suche nach Bedeutung (Embeddings)
    * keyword_search        — Stichwortsuche mit Filtern, Sortierung, Paginierung
    * search_by_case_number — Geschäftsnummern und BGE-Zitate
    * fetch_document        — Einzelner Entscheid mit Volltext (Alias: fetch)
    * list_hierarchy        — Hierarchie-IDs mit Trefferzahlen
    * list_facets           — Baum Kanton/Gericht/Kammer mit Bezeichnungen
    * server_info           — Endpunkt-Konfiguration und Versionsinfo

Aufruf via:

    python -m entscheidsuche_mcp                    # Streamable HTTP auf 127.0.0.1:8765/mcp
    python -m entscheidsuche_mcp --transport stdio  # für lokale CLI-Clients
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_type
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, List, Literal, Optional

from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Mount, Route

from . import __version__
from .access_log import wrap_if_enabled
from .statistik import statistik_endpoint
from .models import (
    DateRange,
    FacetsResponse,
    HierarchyResponse,
    Language,
    SearchHit,
    SearchParams,
    SearchResponse,
    SortOrder,
)
from .search import (
    DEFAULT_MICRO_CHUNK_WAIT,
    EntscheidsucheClient,
    SearchError,
    protect_statute_or,
    result_refs,
)

logger = logging.getLogger(__name__)

# Statische Testoberfläche (wird unter /test ausgeliefert).
_TEST_PAGE = Path(__file__).parent / "static" / "test.html"


# Such-API von entscheidsuche.ch (derselbe Proxy, den die Web-Oberfläche
# verwendet). Alle Endpunkte der Stichwort- und der KI-Suche liegen darunter;
# jeder lässt sich einzeln per Umgebungsvariable überschreiben.
DEFAULT_API_BASE = "https://entscheidsuche.ch/_search_KI.php"
DEFAULT_EMBED_BIG_MODEL = "qwen3-embedding-8b"
DEFAULT_EMBED_MICRO_MODEL = "qwen3-embedding-0.6b"
DEFAULT_FACETS_URL = "https://entscheidsuche.ch/docs/Facetten.json"
DEFAULT_PUBLIC_BASE_URL = "https://mcp.entscheidsuche.ch"

# Höchstzahl Entscheide pro Aufruf der semantischen Suche.
SEMANTIC_MAX_SIZE = 50


def _api_base() -> str:
    return _env("ENTSCHEIDSUCHE_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _endpoints() -> dict[str, str]:
    """Upstream-Endpunkte aus der Umgebung (mit Defaults unter `_api_base()`)."""
    base = _api_base()
    big_model = _env("ENTSCHEIDSUCHE_EMBED_BIG_MODEL", DEFAULT_EMBED_BIG_MODEL)
    micro_model = _env("ENTSCHEIDSUCHE_EMBED_MICRO_MODEL", DEFAULT_EMBED_MICRO_MODEL)
    return {
        "es_url": _env("ENTSCHEIDSUCHE_ES_URL", f"{base}/elasticsearch/_search"),
        "chunk_search_url": _env(
            "ENTSCHEIDSUCHE_CHUNK_SEARCH_URL",
            f"{base}/elasticsearch/embeddings_{big_model}/_search",
        ),
        "micro_chunk_search_url": _env(
            "ENTSCHEIDSUCHE_MICRO_CHUNK_SEARCH_URL",
            f"{base}/elasticsearch/embeddings_{micro_model}_micro/_search",
        ),
        "embed_big_url": _env("ENTSCHEIDSUCHE_EMBED_BIG_URL", f"{base}/embed/big"),
        "embed_big_model": big_model,
        "embed_micro_url": _env("ENTSCHEIDSUCHE_EMBED_MICRO_URL", f"{base}/embed/micro"),
        "embed_micro_model": micro_model,
        "index_micro_chunk_url": _env(
            "ENTSCHEIDSUCHE_INDEX_MICRO_CHUNK_URL", f"{base}/indexMicroChunk"
        ),
        "facets_url": _env("ENTSCHEIDSUCHE_FACETS_URL", DEFAULT_FACETS_URL),
    }


# (name, title, description) — eine Quelle für Server-Card und `server_info`.
TOOL_SUMMARY: List[tuple[str, str, str]] = [
    (
        "search",
        "Search Swiss case law (default)",
        "Default search: AI search by meaning for questions in natural language; "
        "switches to keyword search for case numbers, phrases and Boolean queries.",
    ),
    (
        "semantic_search",
        "AI search by meaning (semantic search)",
        "Embedding-based search for legal questions and descriptions of facts, "
        "across German, French and Italian decisions.",
    ),
    (
        "keyword_search",
        "Keyword search (exact terms)",
        "Full-text search for exact words, phrases and Boolean expressions with "
        "filters, sorting and pagination.",
    ),
    (
        "search_by_case_number",
        "Search by case number",
        "Exact lookup of Swiss case numbers, docket references and BGE citations.",
    ),
    (
        "fetch_document",
        "Fetch decision (full text)",
        "Retrieve a single decision together with its full text.",
    ),
    (
        "fetch",
        "Fetch decision (alias of fetch_document)",
        "Alias of fetch_document.",
    ),
    (
        "list_hierarchy",
        "List courts with decision counts",
        "Hierarchy IDs of cantons, courts and chambers with decision counts.",
    ),
    (
        "list_facets",
        "Court tree with names",
        "Tree of cantons, courts and chambers with names in German, French and Italian.",
    ),
    (
        "server_info",
        "Server info",
        "Version and endpoint information for this server.",
    ),
]

# --- Parameter-Typen, die mehrere Tools gemeinsam haben ----------------------
LanguageArg = Annotated[
    Optional[Language],
    Field(
        description=(
            "Preferred language (de, fr, it) for the displayed title and abstract. "
            "Does NOT filter — use `language_filter` for that."
        ),
    ),
]
TitleLanguageArg = Annotated[
    Optional[Language],
    Field(description="Preferred language (de, fr, it) for title and abstract."),
]
SortArg = Annotated[
    Literal["relevance", "date", "scrapedate"],
    Field(
        description=(
            "Sort order: 'relevance' (default) | 'date' (decision date, newest "
            "first) | 'scrapedate' (date added to the index, newest first)."
        ),
    ),
]
KeywordSizeArg = Annotated[
    int, Field(ge=1, le=100, description="Results per page (1–100).")
]
SearchAfterArg = Annotated[
    Optional[List[Any]],
    Field(description="Cursor from `next_cursor` of the previous response."),
]
DecisionFromArg = Annotated[
    Optional[date_type],
    Field(description="Earliest decision date (YYYY-MM-DD, inclusive)."),
]
DecisionToArg = Annotated[
    Optional[date_type],
    Field(description="Latest decision date (YYYY-MM-DD, inclusive)."),
]
ScrapeFromArg = Annotated[
    Optional[date_type],
    Field(description="Earliest date the decision was added to the index (YYYY-MM-DD)."),
]
ScrapeToArg = Annotated[
    Optional[date_type],
    Field(description="Latest date the decision was added to the index (YYYY-MM-DD)."),
]
HierarchyArg = Annotated[
    Optional[List[str]],
    Field(
        description=(
            "Restrict to cantons, courts or chambers by hierarchy ID, e.g. "
            "['CH_BGer'] or ['ZH_OG', 'BE']. Several IDs are combined with OR."
        ),
    ),
]
LanguageFilterArg = Annotated[
    Optional[List[Language]],
    Field(description="Only decisions written in these languages, e.g. ['fr']."),
]
AggregationsArg = Annotated[
    bool,
    Field(
        description=(
            "Also return distributions of the results by court, language and date."
        )
    ),
]
IncludeContextArg = Annotated[
    bool,
    Field(
        description=(
            "AI search only: also return, per hit, the larger section of the "
            "decision around the matching passage (up to about 6,000 characters "
            "each, field `context`)."
        ),
    ),
]
DocumentIdArg = Annotated[
    str,
    Field(description="Document ID of a search hit, e.g. 'CH_BGer_005_5A-396-2015_2017-06-22'."),
]


async def _guard(awaitable):
    """Lässt verständliche `SearchError`-Meldungen durch, verbirgt alles andere.

    Unerwartete Ausnahmen (Programmierfehler, unerwartete Upstream-Antworten)
    würden sonst mit internen Details beim Client landen.
    """
    try:
        return await awaitable
    except SearchError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unerwarteter Fehler in einem Tool-Aufruf")
        raise SearchError(
            "The request failed because of an internal error of the "
            "entscheidsuche MCP server. Try again later."
        ) from exc


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
    endpoints = _endpoints()
    timeout = float(_env("HTTP_TIMEOUT", "30"))
    verify_ssl = _env_bool("ENTSCHEIDSUCHE_VERIFY_SSL", True)
    semantic = _env_bool("ENTSCHEIDSUCHE_SEMANTIC_SEARCH", True)
    client = EntscheidsucheClient(
        es_url=endpoints["es_url"],
        facets_url=endpoints["facets_url"],
        timeout=timeout,
        verify_ssl=verify_ssl,
        chunk_search_url=endpoints["chunk_search_url"] if semantic else None,
        micro_chunk_search_url=endpoints["micro_chunk_search_url"] if semantic else None,
        embed_big_url=endpoints["embed_big_url"] if semantic else None,
        embed_big_model=endpoints["embed_big_model"],
        embed_micro_url=endpoints["embed_micro_url"] if semantic else None,
        embed_micro_model=endpoints["embed_micro_model"],
        index_micro_chunk_url=endpoints["index_micro_chunk_url"] if semantic else None,
        micro_chunk_wait=float(
            _env("ENTSCHEIDSUCHE_MICRO_CHUNK_WAIT", str(DEFAULT_MICRO_CHUNK_WAIT))
        ),
        # Der Präfix-Filter greift nur beim Such-Endpunkt über alle Indizes;
        # leer setzen, falls ES_URL bereits auf einen Index-Pattern zeigt und
        # die Indizes anders heissen.
        index_prefix=os.environ.get("ENTSCHEIDSUCHE_INDEX_PREFIX", "entscheidsuche.v2-"),
        view_url=_env("ENTSCHEIDSUCHE_VIEW_URL", "https://entscheidsuche.ch/view/"),
    )
    logger.info(
        "entscheidsuche-mcp %s — Suche=%s, KI-Suche=%s",
        __version__, endpoints["es_url"], "an" if semantic else "aus",
    )
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

    Default: AUS. Dieser Server ist öffentlich, ohne Authentifizierung und
    nur lesend; er wird von Browser-Clients (Claude, ChatGPT u.a.) über eine
    fremde Origin (z.B. https://claude.ai) angesprochen. Der DNS-Rebinding-
    Schutz prüft Host/Origin und ist dafür gedacht, LOKALE bzw.
    authentifizierte Server vor bösartigen Webseiten zu schützen — hier gibt
    es nichts zu schützen (öffentliche Daten, keine Credentials), er würde nur
    legitime Clients mit „403 Invalid Origin header" aussperren. Die
    Host-Prüfung übernimmt ohnehin nginx (server_name). Bei Bedarf lässt er
    sich mit `MCP_DNS_REBINDING_PROTECTION=true` wieder einschalten.
    """
    enabled = _env_bool("MCP_DNS_REBINDING_PROTECTION", False)
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
        "description": (
            "MCP server for Swiss court decisions and case law provided by "
            "entscheidsuche.ch: AI search by meaning and keyword search."
        ),
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
            {"name": name, "title": title, "description": description}
            for name, title, description in TOOL_SUMMARY
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


# ---------------------------------------------------------------------------
# Tool-Beschreibungen (englisch — sie werden von Sprachmodellen gelesen)
# ---------------------------------------------------------------------------

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_FILTER_NOTE = (
    "Filters are combined with AND. Hierarchy IDs (canton / court / chamber, e.g. "
    "'CH_BGer', 'ZH_OG') are case-sensitive; take them from `list_hierarchy` or "
    "`list_facets`."
)

_SEARCH_DESCRIPTION = (
    "Default search for Swiss court decisions and case law on entscheidsuche.ch "
    "(Federal Supreme Court BGer/BGE and cantonal courts; German, French, "
    "Italian). Use this tool unless you need something specific.\n\n"
    "Describe the legal question or the facts in natural language, in any "
    "language — the search works by meaning (AI embedding search, same as "
    "`semantic_search`) and also finds decisions that use different words or "
    "are written in another language. Each hit comes with the passage of the "
    "decision that best matches the query.\n\n"
    "The tool switches to exact keyword search (same as `keyword_search`) "
    "automatically when the query is a case number or citation (e.g. "
    "'5A_396/2015', 'BGE 142 III 1'), contains a quoted phrase, Boolean "
    "operators or wildcards, is empty or '*', or when `sort`, `search_after` "
    "or `include_aggregations` is used. The response field `search_mode` says "
    "which search ran.\n\n"
    "Read the full text of a hit with `fetch` (or `fetch_document`), passing "
    "the hit's `id`. " + _FILTER_NOTE
)

_SEMANTIC_DESCRIPTION = (
    "AI search by meaning (semantic / embedding search) across Swiss court "
    "decisions on entscheidsuche.ch. Best for legal questions, descriptions of "
    "facts and concepts: write one or more full sentences instead of keywords, "
    "e.g. 'Can a landlord terminate the lease because his daughter wants to "
    "move in?'. Works across languages: a German query also finds French and "
    "Italian decisions.\n\n"
    "How it works: the query is compared with the passages of all decisions; "
    "the 100 best-matching passages are determined and grouped by decision. "
    "Results are ranked by similarity (`score`); `text` contains the best "
    "matching passage of each decision. There is no pagination, no other sort "
    "order and no Boolean syntax — use `keyword_search` for exact terms, "
    "`search_by_case_number` for case numbers.\n\n" + _FILTER_NOTE
)

_KEYWORD_DESCRIPTION = (
    "Exact keyword (full-text) search across Swiss court decisions on "
    "entscheidsuche.ch. Finds decisions that contain the given words. Use it "
    "for specific terms, statute articles, names, quoted phrases, complete "
    "result lists, sorting by date and pagination. For legal questions in "
    "natural language prefer `semantic_search`.\n\n"
    "Search syntax (Lucene query string):\n"
    "  - All terms must occur (default operator AND).\n"
    "  - Phrase: \"dringender Eigenbedarf\" (double quotes).\n"
    "  - Boolean operators in capitals between terms: AND, OR, NOT.\n"
    "  - Wildcards: * and ?  —  '*' alone matches every decision.\n"
    "  - Statute citations work as written: Art. 271 OR, Art. 8 ZGB.\n"
    "  - Put case numbers in quotes (\"5A_396/2015\") or use "
    "`search_by_case_number`.\n"
    "If a query is not valid syntax it is searched as plain terms and the "
    "response says so in `note`.\n\n"
    "`text` contains a snippet with the matched terms in <em>…</em>. "
    + _FILTER_NOTE
    + "\n\nPagination: pass `next_cursor` of the response as `search_after` in "
    "the next call (same query, same sort)."
)

_FETCH_DESCRIPTION = (
    "Fetch one Swiss court decision by its document ID and return its full "
    "text together with title, court, date and links. Use the `id` of a hit "
    "returned by one of the search tools. For very long decisions the text is "
    "capped at 100,000 characters; if `text_truncated` is true, the complete "
    "document is available at `document_url`."
)

# Geschäftsnummern / Zitate, die als ganze Anfrage auf die Stichwortsuche gehen.
_CASE_NUMBER_RES = (
    # BGE 142 III 1, ATF 142 III 1, DTF 142 III 1
    re.compile(r"^(?:BGE|ATF|DTF)\s+\d{1,3}\s+[IVX]{1,3}[a-z]?\s+\d{1,4}$", re.I),
    # 5A_396/2015, 4A.123/2007, 1C_1/2024
    re.compile(r"^\d{1,2}[A-Z]{1,2}[_.]\d{1,4}/\d{4}$", re.I),
    # allgemein: ein einzelnes «Wort» mit Ziffern und typischen Trennzeichen,
    # z.B. ZB.2021.48, NG240011, VB.2015.00490, B-6350/2015, LE260019
    re.compile(r"^(?=\S*\d)(?=\S*[A-Za-z])[A-Za-z0-9]+(?:[._/\-][A-Za-z0-9]+)+$"),
    # Geschäftsnummern ohne Trennzeichen: Buchstaben + mindestens vier Ziffern
    re.compile(r"^[A-Za-z]{1,4}\d{4,}$"),
)
_BOOLEAN_BETWEEN_TERMS_RE = re.compile(r"\S\s+(?:AND|OR|NOT)\s+\S")
# `*` direkt an einem Wort, `?` nur IM Wort — ein Fragezeichen am Satzende ist
# Interpunktion, kein Platzhalter.
_WILDCARD_RE = re.compile(r"\w\*|\*\w|\w\?\w")


def keyword_reason(
    query: str,
    sort: str = "relevance",
    search_after: Optional[List[Any]] = None,
    include_aggregations: bool = False,
) -> Optional[str]:
    """Entscheidet für das `search`-Tool, ob die Stichwortsuche zu nehmen ist.

    Rückgabe: Begründung (für `note`), wenn ja; sonst None (→ semantische Suche).
    Reihenfolge und Regeln sind in docs/API.md dokumentiert.
    """
    q = (query or "").strip()
    if sort != "relevance" or search_after is not None or include_aggregations:
        return "`sort`, `search_after` or `include_aggregations` was used"
    if not q or q == "*":
        return "the query is empty or '*'"
    if any(rx.match(q) for rx in _CASE_NUMBER_RES):
        return "the query looks like a case number or citation"
    if '"' in q:
        return "the query contains a quoted phrase"
    statute_safe, _ = protect_statute_or(q)
    if _BOOLEAN_BETWEEN_TERMS_RE.search(statute_safe.replace('"OR"', "")):
        return "the query contains Boolean operators"
    if _WILDCARD_RE.search(q):
        return "the query contains wildcards"
    return None


def build_server() -> FastMCP:
    """Erzeugt die FastMCP-Instanz mit allen registrierten Tools."""
    mcp = FastMCP(
        name="entscheidsuche",
        instructions=(
            "MCP server for entscheidsuche.ch — the open search engine for Swiss "
            "court decisions and case law (Schweizer Gerichtsentscheide, "
            "Rechtsprechung, jurisprudence suisse): Federal Supreme Court "
            "(BGer, BGE) and cantonal courts, in German, French and Italian.\n"
            "Tools: `search` is the default — describe the legal question in "
            "natural language (AI search by meaning). `semantic_search` is the "
            "same AI search without automatic switching. `keyword_search` finds "
            "exact words, phrases and Boolean expressions and supports sorting "
            "and pagination. `search_by_case_number` looks up case numbers and "
            "BGE citations. `fetch` / `fetch_document` return the full text of "
            "a decision. `list_hierarchy` and `list_facets` list cantons, courts "
            "and chambers for the `hierarchy` filter."
        ),
        lifespan=_lifespan,
        transport_security=_transport_security(),
    )
    # FastMCP meldet in `initialize` sonst die Version des MCP-SDK als
    # Server-Version.
    try:
        mcp._mcp_server.version = __version__
    except AttributeError:  # pragma: no cover — SDK-Interna geändert
        logger.warning("Server-Version konnte nicht gesetzt werden")

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
    # gemeinsame Implementierungen
    # ------------------------------------------------------------------
    async def _keyword(
        ctx: Context,
        *,
        query: str,
        language: Optional[Language],
        sort: str,
        size: int,
        search_after: Optional[List[Any]],
        decision_date_from: Optional[date_type],
        decision_date_to: Optional[date_type],
        scrape_date_from: Optional[date_type],
        scrape_date_to: Optional[date_type],
        hierarchy: Optional[List[str]],
        language_filter: Optional[List[Language]],
        include_aggregations: bool,
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
        return await _guard(_client(ctx).search(params))

    async def _semantic(
        ctx: Context,
        *,
        query: str,
        language: Optional[Language],
        size: int,
        decision_date_from: Optional[date_type],
        decision_date_to: Optional[date_type],
        scrape_date_from: Optional[date_type],
        scrape_date_to: Optional[date_type],
        hierarchy: Optional[List[str]],
        language_filter: Optional[List[Language]],
        include_context: bool,
    ) -> SearchResponse:
        return await _guard(
            _client(ctx).semantic_search(
                query,
                size=min(size, SEMANTIC_MAX_SIZE),
                language=language,
                decision_date=_coerce_date_range(decision_date_from, decision_date_to),
                scrape_date=_coerce_date_range(scrape_date_from, scrape_date_to),
                hierarchy=hierarchy,
                language_filter=language_filter,
                include_context=include_context,
            )
        )

    async def _fetch(ctx: Context, id: str, language: Optional[Language]) -> SearchHit:
        hit = await _guard(_client(ctx).get_document(id, language))
        if hit is None:
            raise ValueError(
                f"No decision found with id '{id}'. Document IDs are case-sensitive; "
                "use the `id` of a hit returned by one of the search tools."
            )
        return hit

    # ------------------------------------------------------------------
    # search — Standardsuche (KI-Suche, mit automatischem Wechsel)
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Search Swiss case law (default)",
        description=_SEARCH_DESCRIPTION,
        annotations=_READ_ONLY,
    )
    async def search(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                description=(
                    "The legal question or facts in natural language (any "
                    "language), or a case number / citation."
                ),
            ),
        ] = "*",
        language: LanguageArg = None,
        size: Annotated[
            int,
            Field(
                ge=1,
                le=100,
                description="Number of decisions to return (AI search: at most 50).",
            ),
        ] = 10,
        decision_date_from: DecisionFromArg = None,
        decision_date_to: DecisionToArg = None,
        scrape_date_from: ScrapeFromArg = None,
        scrape_date_to: ScrapeToArg = None,
        hierarchy: HierarchyArg = None,
        language_filter: LanguageFilterArg = None,
        include_context: IncludeContextArg = False,
        sort: Annotated[
            Literal["relevance", "date", "scrapedate"],
            Field(
                description=(
                    "Keyword search only: 'relevance' | 'date' (decision date) | "
                    "'scrapedate'. Any value other than 'relevance' switches to "
                    "keyword search."
                ),
            ),
        ] = "relevance",
        search_after: Annotated[
            Optional[List[Any]],
            Field(
                description=(
                    "Keyword search only: cursor from `next_cursor` of the "
                    "previous response."
                )
            ),
        ] = None,
        include_aggregations: Annotated[
            bool,
            Field(
                description=(
                    "Keyword search only: include distributions over the results."
                )
            ),
        ] = False,
    ) -> SearchResponse:
        reason = keyword_reason(query, sort, search_after, include_aggregations)
        if reason is None and not _client(ctx).semantic_available:
            reason = "semantic search is not configured on this server"
        if reason is not None:
            q = (query or "").strip() or "*"
            if any(rx.match(q) for rx in _CASE_NUMBER_RES):
                q = _quote_as_phrase(q)
            response = await _keyword(
                ctx,
                query=q,
                language=language,
                sort=sort,
                size=size,
                search_after=search_after,
                decision_date_from=decision_date_from,
                decision_date_to=decision_date_to,
                scrape_date_from=scrape_date_from,
                scrape_date_to=scrape_date_to,
                hierarchy=hierarchy,
                language_filter=language_filter,
                include_aggregations=include_aggregations,
            )
            switch_note = f"Keyword search was used because {reason}."
            response.note = f"{switch_note} {response.note}" if response.note else switch_note
        else:
            response = await _semantic(
                ctx,
                query=query,
                language=language,
                size=size,
                decision_date_from=decision_date_from,
                decision_date_to=decision_date_to,
                scrape_date_from=scrape_date_from,
                scrape_date_to=scrape_date_to,
                hierarchy=hierarchy,
                language_filter=language_filter,
                include_context=include_context,
            )
        response.results = result_refs(response.hits)
        return response

    # ------------------------------------------------------------------
    # semantic_search — KI-Suche
    # ------------------------------------------------------------------
    @mcp.tool(
        title="AI search by meaning (semantic search)",
        description=_SEMANTIC_DESCRIPTION,
        annotations=_READ_ONLY,
    )
    async def semantic_search(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                description=(
                    "Legal question or description of the facts in natural "
                    "language, ideally one or more full sentences. Any language."
                ),
            ),
        ],
        language: LanguageArg = None,
        size: Annotated[
            int,
            Field(ge=1, le=SEMANTIC_MAX_SIZE, description="Number of decisions to return (1–50)."),
        ] = 10,
        decision_date_from: DecisionFromArg = None,
        decision_date_to: DecisionToArg = None,
        scrape_date_from: ScrapeFromArg = None,
        scrape_date_to: ScrapeToArg = None,
        hierarchy: HierarchyArg = None,
        language_filter: LanguageFilterArg = None,
        include_context: IncludeContextArg = False,
    ) -> SearchResponse:
        return await _semantic(
            ctx,
            query=query,
            language=language,
            size=size,
            decision_date_from=decision_date_from,
            decision_date_to=decision_date_to,
            scrape_date_from=scrape_date_from,
            scrape_date_to=scrape_date_to,
            hierarchy=hierarchy,
            language_filter=language_filter,
            include_context=include_context,
        )

    # ------------------------------------------------------------------
    # keyword_search — Stichwortsuche
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Keyword search (exact terms)",
        description=_KEYWORD_DESCRIPTION,
        annotations=_READ_ONLY,
    )
    async def keyword_search(
        ctx: Context,
        query: Annotated[
            str,
            Field(
                description=(
                    "Search terms. \"...\" for phrases, AND/OR/NOT for Boolean "
                    "operators, * and ? for wildcards. \"*\" matches everything."
                ),
            ),
        ] = "*",
        language: LanguageArg = None,
        sort: SortArg = "relevance",
        size: KeywordSizeArg = 20,
        search_after: SearchAfterArg = None,
        decision_date_from: DecisionFromArg = None,
        decision_date_to: DecisionToArg = None,
        scrape_date_from: ScrapeFromArg = None,
        scrape_date_to: ScrapeToArg = None,
        hierarchy: HierarchyArg = None,
        language_filter: LanguageFilterArg = None,
        include_aggregations: AggregationsArg = False,
    ) -> SearchResponse:
        return await _keyword(
            ctx,
            query=query,
            language=language,
            sort=sort,
            size=size,
            search_after=search_after,
            decision_date_from=decision_date_from,
            decision_date_to=decision_date_to,
            scrape_date_from=scrape_date_from,
            scrape_date_to=scrape_date_to,
            hierarchy=hierarchy,
            language_filter=language_filter,
            include_aggregations=include_aggregations,
        )

    # ------------------------------------------------------------------
    # search_by_case_number
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Search by case number",
        description=(
            "Look up Swiss court decisions by case number, docket reference or "
            "BGE citation, e.g. 'BGE 142 III 1', '5A_396/2015' or 'ZB.2021.48'. "
            "The reference is searched as an exact phrase; the decision itself "
            "normally comes first, followed by decisions that cite it."
        ),
        annotations=_READ_ONLY,
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
        language: LanguageArg = None,
        sort: SortArg = "relevance",
        size: KeywordSizeArg = 20,
        search_after: SearchAfterArg = None,
        decision_date_from: DecisionFromArg = None,
        decision_date_to: DecisionToArg = None,
        scrape_date_from: ScrapeFromArg = None,
        scrape_date_to: ScrapeToArg = None,
        hierarchy: HierarchyArg = None,
        language_filter: LanguageFilterArg = None,
        include_aggregations: AggregationsArg = False,
    ) -> SearchResponse:
        number = case_number.strip()
        if len(number) >= 2 and number[0] == number[-1] == '"':
            number = number[1:-1]
        if not number.strip():
            raise ValueError("`case_number` must not be empty.")
        return await _keyword(
            ctx,
            query=_quote_as_phrase(number),
            language=language,
            sort=sort,
            size=size,
            search_after=search_after,
            decision_date_from=decision_date_from,
            decision_date_to=decision_date_to,
            scrape_date_from=scrape_date_from,
            scrape_date_to=scrape_date_to,
            hierarchy=hierarchy,
            language_filter=language_filter,
            include_aggregations=include_aggregations,
        )

    # ------------------------------------------------------------------
    # fetch_document / fetch
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Fetch decision (full text)",
        description=_FETCH_DESCRIPTION,
        annotations=_READ_ONLY,
    )
    async def fetch_document(
        ctx: Context,
        id: DocumentIdArg,
        language: TitleLanguageArg = None,
    ) -> SearchHit:
        return await _fetch(ctx, id, language)

    @mcp.tool(
        title="Fetch decision (alias of fetch_document)",
        description=_FETCH_DESCRIPTION + " Identical to `fetch_document`.",
        annotations=_READ_ONLY,
    )
    async def fetch(
        ctx: Context,
        id: DocumentIdArg,
        language: TitleLanguageArg = None,
    ) -> SearchHit:
        return await _fetch(ctx, id, language)

    # ------------------------------------------------------------------
    # list_hierarchy
    # ------------------------------------------------------------------
    @mcp.tool(
        title="List courts with decision counts",
        description=(
            "Returns the hierarchy IDs of Swiss cantons, courts and chambers "
            "together with the number of decisions, optionally restricted to "
            "the decisions matching a keyword query. Use the IDs in the "
            "`hierarchy` filter of the search tools."
        ),
        annotations=_READ_ONLY,
    )
    async def list_hierarchy(
        ctx: Context,
        query: Annotated[
            str, Field(description="Optional keyword query (same syntax as `keyword_search`).")
        ] = "*",
        size: Annotated[
            int, Field(ge=1, le=10000, description="Maximum number of entries.")
        ] = 1000,
    ) -> HierarchyResponse:
        return await _guard(_client(ctx).list_hierarchy(query, size))

    # ------------------------------------------------------------------
    # list_facets
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Court tree with names (canton / court / chamber)",
        description=(
            "Tree of all cantons, courts and chambers with their names in "
            "German, French and Italian. The `id` fields can be used in the "
            "`hierarchy` filter of the search tools."
        ),
        annotations=_READ_ONLY,
    )
    async def list_facets(ctx: Context) -> FacetsResponse:
        nodes = await _guard(_client(ctx).list_facets())
        return FacetsResponse(facets=nodes)

    # ------------------------------------------------------------------
    # server_info
    # ------------------------------------------------------------------
    @mcp.tool(
        title="Server info",
        description=(
            "Version and configured endpoints of this MCP server for Swiss "
            "case law."
        ),
        annotations=_READ_ONLY,
    )
    async def server_info(ctx: Context) -> dict[str, Any]:
        client = _client(ctx)
        return {
            "name": "entscheidsuche-mcp",
            "version": __version__,
            "search_url": client.es_url,
            "semantic_search": client.semantic_available,
            "embedding_model": client.embed_big_model if client.semantic_available else None,
            "facets_url": client.facets_url,
            "languages": [l.value for l in Language],
            "sort_orders": [s.value for s in SortOrder],
            "tools": [name for name, _title, _desc in TOOL_SUMMARY],
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

    async def test_page(_request):
        """Testoberfläche für die MCP-API (statische Seite, ruft POST /mcp auf)."""
        try:
            html = _TEST_PAGE.read_text(encoding="utf-8")
        except OSError:
            return JSONResponse({"error": "test page not installed"}, status_code=404)
        html = html.replace("__MCP_PATH__", mcp_path).replace("__VERSION__", __version__)
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

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
            Route("/test", endpoint=test_page),
            Route("/test/", endpoint=test_page),
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
