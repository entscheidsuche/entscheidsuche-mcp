"""Elasticsearch/OpenSearch-Client für entscheidsuche.ch.

Diese Modul portiert die Suchlogik aus dem TypeScript-Frontend von
entscheidsuche.ch (`SearchUtil.ts`) nach Python.

Der Client baut Elasticsearch-Queries für:

* Stichwortsuche mit Lucene-`query_string`-Syntax (Anführungszeichen → Phrasen)
* Semantische Suche (KI-Suche): Die Anfrage wird in einen Embedding-Vektor
  umgerechnet, per kNN werden die inhaltlich nächsten Textabschnitte (Chunks)
  gesucht und den Entscheiden zugeordnet
* Filter nach Entscheiddatum, Scraping-Datum, Hierarchie (Kanton/Gericht/Kammer),
  Sprache
* Aggregationen (Buckets) für Facetten-Auswertung
* Highlight-Snippets mit Auszügen rund um die Treffer

Die rohe Elasticsearch-Antwort wird in saubere, dokumentierte Pydantic-Modelle
übersetzt.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .models import (
    AggregationBucket,
    DateRange,
    FacetLabel,
    FacetNode,
    HierarchyEntry,
    HierarchyResponse,
    Language,
    ResultRef,
    SearchHit,
    SearchParams,
    SearchResponse,
    SortOrder,
)

logger = logging.getLogger(__name__)

# Default-Limit des Elasticsearch-`ingest-attachment`-Prozessors (Tika):
# `indexed_chars` = 100'000. Längere Entscheide werden im Index abgeschnitten;
# der vollständige Text steht nur im Originaldokument (content_url).
INDEXED_CHARS_LIMIT = 100_000

# Der Such-Endpunkt von entscheidsuche.ch durchsucht ohne Einschränkung ALLE
# Indizes des Clusters — auch die Chunk-/Embedding-Indizes der KI-Suche. Die
# Entscheide selbst liegen in den Indizes mit diesem Präfix; jede Abfrage auf
# die Entscheide wird darauf eingeschränkt.
DECISION_INDEX_PREFIX = "entscheidsuche.v2-"

# Öffentliche Ansichts-URL eines Entscheids (für Zitate/Links).
DEFAULT_VIEW_URL = "https://entscheidsuche.ch/view/"

# --- Semantische Suche (KI-Suche) -------------------------------------------
# Qwen3-Embedding erwartet nur auf der Anfrage-Seite eine Instruktion; die
# Dokumente wurden ohne Instruktion eingebettet. Identisch mit dem Frontend.
QUERY_INSTRUCTION = (
    "Given a legal search query, retrieve relevant passages of court decisions "
    "that answer the query"
)
# Anzahl der Textabschnitte (grosse Chunks), die per kNN ermittelt werden. Wie
# im Frontend: die 100 am besten passenden Abschnitte; mehrere Abschnitte
# desselben Entscheids ergeben einen Treffer.
SEMANTIC_CHUNKS = 100
# Sprach- und Scrape-Datum-Filter sind im Chunk-Index nicht zuverlässig gefüllt
# und werden erst auf dem Hauptindex angewendet. Damit danach genug Entscheide
# übrig bleiben, werden in diesem Fall mehr Abschnitte geholt.
SEMANTIC_CHUNKS_POSTFILTER = 300
# Fehlen für einen Treffer die Micro-Chunks, werden sie (wie im Frontend) über
# `indexMicroChunk` nachindexiert. Der MCP-Aufruf wartet darauf insgesamt
# höchstens so viele Sekunden; danach kommt der Treffer ohne Auszug.
DEFAULT_MICRO_CHUNK_WAIT = 20.0
# HTTP-Timeout des Indexierungs-Requests selbst (Frontend: 120 s).
INDEX_MICRO_CHUNK_TIMEOUT = 120.0
# Höchstzahl gleichzeitig laufender semantischer Suchen (jede schickt
# Vektoren mit mehreren tausend Werten an den Upstream).
SEMANTIC_CONCURRENCY = 8


# Die Begrenzung gilt prozessweit: Im zustandslosen HTTP-Betrieb legt FastMCP
# pro Request einen eigenen Client an, ein Semaphor am Client wäre wirkungslos.
_SEMANTIC_SLOTS: Dict[int, asyncio.Semaphore] = {}


def _semantic_slots() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    slots = _SEMANTIC_SLOTS.get(loop_id)
    if slots is None:
        slots = _SEMANTIC_SLOTS[loop_id] = asyncio.Semaphore(SEMANTIC_CONCURRENCY)
    return slots


class SearchError(Exception):
    """Fehler mit einer Meldung, die direkt an den MCP-Client gehen kann."""


class QuerySyntaxError(SearchError):
    """Die Anfrage ist keine gültige Lucene-Syntax (HTTP 400 vom Upstream)."""


# Felder, in denen die Volltext-Suche stattfindet (mit Boost-Faktoren).
# Identisch mit dem Frontend.
QUERY_FIELDS = [
    "title.*^5",
    "abstract.*^3",
    "meta.*^10",
    "attachment.content",
    "reference^3",
]


def _serialise_date(value: date | int | str) -> str | int:
    """Datum so serialisieren, wie es Elasticsearch erwartet.

    Im TS-Code werden negative Millisekunden-Timestamps zu YYYY-MM-DD-Strings,
    sonst bleiben sie ms-Timestamps. Wir akzeptieren date/int/str und geben
    je nachdem ms oder ISO-Datum zurück.
    """
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        if value >= 0:
            return value
        # `datetime.utcfromtimestamp` ist seit Python 3.12 deprecated.
        d = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        return d.strftime("%Y-%m-%d")
    raise TypeError(f"Unsupported date type: {type(value)}")


def _date_range_clause(field: str, range_: DateRange) -> dict | None:
    """Baut eine Elasticsearch-`range`-Klausel für ein Datumsfeld."""
    payload: dict[str, Any] = {}
    if range_.from_ is not None:
        payload["gte"] = _serialise_date(range_.from_)
    if range_.to is not None:
        payload["lte"] = _serialise_date(range_.to)
    if not payload:
        return None
    return {"range": {field: payload}}


def _build_filters(params: SearchParams) -> List[dict]:
    """Konvertiert die Filter aus den Such-Parametern in ES-Filter-Klauseln."""
    clauses: List[dict] = []

    if params.decision_date is not None:
        clause = _date_range_clause("date", params.decision_date)
        if clause is not None:
            clauses.append(clause)

    if params.scrape_date is not None:
        clause = _date_range_clause("scrapedate", params.scrape_date)
        if clause is not None:
            clauses.append(clause)

    if params.hierarchy:
        clauses.append({"terms": {"hierarchy": params.hierarchy}})

    if params.language_filter:
        clauses.append(
            {
                "terms": {
                    "attachment.language": [
                        lang.value for lang in params.language_filter
                    ]
                }
            }
        )

    return clauses


def _calendar_interval(range_: DateRange | None) -> str:
    """Wählt das Aggregations-Intervall für `date_histogram`."""
    if range_ is None or range_.from_ is None or range_.to is None:
        return "quarter"
    span_days = (range_.to - range_.from_).days
    if span_days < 40:
        return "day"
    if span_days < 280:
        return "week"
    if span_days < 1200:
        return "month"
    return "quarter"


def _highlight_fields(language: Language | None) -> Dict[str, Any]:
    """Highlight-Felder für `_build_query`.

    Ohne expliziten Sprach-Wunsch werden alle Sprach-Sub-Felder von `title` und
    `abstract` gehighlighted; sonst nur das angefragte Sub-Feld.
    """
    if language is None:
        return {
            "title.*": {"number_of_fragments": 0},
            "abstract.*": {"number_of_fragments": 0},
            "attachment.content": {},
        }
    return {
        f"title.{language.value}": {"number_of_fragments": 0},
        f"abstract.{language.value}": {"number_of_fragments": 0},
        "attachment.content": {},
    }


_LUCENE_SPECIAL_RE = re.compile(r'([+\-=&|!(){}\[\]^"~*?:\\/])')
_LUCENE_OPERATOR_RE = re.compile(r"\b(AND|OR|NOT|TO)\b")


def plain_terms_query(query: str) -> str:
    """Macht aus einer beliebigen Eingabe eine garantiert gültige Lucene-Anfrage.

    Alle Sonderzeichen werden maskiert und die Operatoren AND/OR/NOT klein
    geschrieben, sodass sie als gewöhnliche Suchwörter gelten. Damit wird
    z.B. ``Art. 271 OR`` (OR = Obligationenrecht) oder ``5A_396/2015`` zu einer
    normalen Suche nach diesen Begriffen statt zu einem Syntaxfehler.
    """
    q = query.replace("<", " ").replace(">", " ")
    q = _LUCENE_SPECIAL_RE.sub(r"\\\1", q)
    q = _LUCENE_OPERATOR_RE.sub(lambda m: m.group(1).lower(), q)
    return q.strip() or "*"


# «Art. 271 OR», «Art. 41 Abs. 1 OR»: Hier ist OR das Obligationenrecht, nicht
# der Boolesche Operator. Ohne Schutz würde Lucene «271 OR <nächstes Wort>»
# lesen und praktisch alle Entscheide liefern.
_STATUTE_OR_RE = re.compile(
    r"(\b[Aa]rt(?:ikel|\.)?\s*\d+[a-z]{0,5}"
    r"(?:\s+(?:Abs|Ziff|lit|Bst|al|cpv|let)\.?\s*[\w.]+)*\s+)OR\b"
)


def protect_statute_or(query: str) -> Tuple[str, bool]:
    """Setzt das Gesetzeskürzel OR nach einer Artikelangabe in Anführungszeichen.

    Rückgabe: (ggf. angepasste Anfrage, wurde angepasst?).
    """
    new_query, count = _STATUTE_OR_RE.subn(r'\1"OR"', query or "")
    return new_query, count > 0


def _query_string_clause(query: str) -> dict:
    return {
        "query_string": {
            "query": query or "*",
            "default_operator": "AND",
            "type": "cross_fields",
            "fields": QUERY_FIELDS,
        }
    }


def _index_filter(index_prefix: Optional[str]) -> List[dict]:
    """Filter, der die Abfrage auf die Entscheid-Indizes einschränkt."""
    if not index_prefix:
        return []
    return [{"prefix": {"_index": index_prefix}}]


def _build_query(
    params: SearchParams,
    index_prefix: Optional[str] = DECISION_INDEX_PREFIX,
    query_override: Optional[str] = None,
) -> dict:
    """Erzeugt den vollständigen Elasticsearch-Query-Body."""
    sort_field = {
        SortOrder.relevance: "_score",
        SortOrder.date: "date",
        SortOrder.scrapedate: "scrapedate",
    }[params.sort]

    body: Dict[str, Any] = {
        "size": params.size,
        "_source": {"excludes": ["attachment.content"]},
        "track_total_hits": True,
        "query": {
            "bool": {
                "must": _query_string_clause(
                    query_override if query_override is not None else params.query
                )
            }
        },
        "sort": [{sort_field: "desc"}, {"id": "desc"}],
        "highlight": {"fields": _highlight_fields(params.language)},
    }

    filters = _index_filter(index_prefix) + _build_filters(params)
    if filters:
        body["query"]["bool"]["filter"] = filters

    if params.search_after is not None:
        body["search_after"] = params.search_after

    if params.include_aggregations and params.search_after is None:
        aggs: Dict[str, Any] = {}
        if not params.hierarchy:
            aggs["hierarchy"] = {
                "terms": {"size": 1000, "field": "hierarchy"}
            }
        if not params.language_filter:
            aggs["language"] = {
                "terms": {"size": 4, "field": "attachment.language"}
            }
        if params.decision_date is None:
            aggs["decision_date"] = {
                "date_histogram": {
                    "calendar_interval": _calendar_interval(params.decision_date),
                    "field": "date",
                }
            }
        if params.scrape_date is None:
            aggs["scrape_date"] = {
                "date_histogram": {
                    "calendar_interval": _calendar_interval(params.scrape_date),
                    "field": "scrapedate",
                }
            }
        if aggs:
            body["aggs"] = aggs

    return body


def _format_iso_date(raw: str) -> Optional[str]:
    """ES liefert das Datum als 'YYYY-MM-DD' (10 Zeichen). Nur dann übernehmen."""
    if not raw or len(raw) != 10:
        return None
    return raw


def _join_highlight(parts: Optional[List[str]]) -> str:
    if not parts:
        return ""
    return " ".join(parts)


_COURT_PREFIX_RE = re.compile(r"^([^_]*_[^_]*)")

# Sprach-Reihenfolge für Fallback, wenn keine Sprache angefragt oder das angefragte
# Sub-Feld leer ist. Schweizer Amtssprachen, dann Englisch.
_LANG_FALLBACK_ORDER: Tuple[str, ...] = ("de", "fr", "it", "en")


def _localized_value(field: dict | None, lang: Language | None) -> str:
    """Holt den Wert eines lokalisierten Felds mit Fallback auf andere Sprachen."""
    if not field:
        return ""
    if lang is not None:
        val = field.get(lang.value)
        if val:
            return val
    for code in _LANG_FALLBACK_ORDER:
        val = field.get(code)
        if val:
            return val
    return ""


# Trunkierungs-Hinweis in der Sprache des Entscheids (Metadatum `language`,
# de/fr/it); Rückfallsprache Englisch, wenn keine Sprache bekannt ist.
_TRUNCATION_NOTE: Dict[str, str] = {
    "de": "Text abgeschnitten, vollständiger Text unter {url}",
    "fr": "texte tronqué, voir le texte intégral sous {url}",
    "it": "testo troncato, vedi il testo completo su {url}",
    "en": "text truncated, see full text under {url}",
}


def _localized_highlight(
    highlight: dict, prefix: str, lang: Language | None
) -> tuple[str, str | None]:
    """Liefert (highlight_text, used_language_code) für ein lokalisiertes Feld.

    Sucht zuerst die angefragte Sprache, dann jede Sprache aus dem Fallback;
    Rückgabe ist leer-string + None, wenn nichts gehighlighted wurde.
    """
    if lang is not None:
        key = f"{prefix}.{lang.value}"
        if highlight.get(key):
            return _join_highlight(highlight[key]), lang.value
    for code in _LANG_FALLBACK_ORDER:
        key = f"{prefix}.{code}"
        if highlight.get(key):
            return _join_highlight(highlight[key]), code
    return "", None


def _parse_hits(
    raw: dict,
    lang: Language | None,
    include_content: bool = False,
    view_url: Optional[str] = DEFAULT_VIEW_URL,
) -> Tuple[List[SearchHit], int]:
    """Konvertiert die ES-Treffer in unsere `SearchHit`-Modelle."""
    hits_node = raw.get("hits", {})
    total = hits_node.get("total", {}).get("value", 0)
    hits: List[SearchHit] = []

    for hit in hits_node.get("hits", []):
        src = hit.get("_source", {}) or {}
        highlight = hit.get("highlight", {}) or {}

        title = _localized_value(src.get("title"), lang)
        abstract = _localized_value(src.get("abstract"), lang)
        meta = _localized_value(src.get("meta"), lang)
        original_url = _localized_value(src.get("url"), lang)

        text = _join_highlight(highlight.get("attachment.content"))
        title_hl, _ = _localized_highlight(highlight, "title", lang)
        if title_hl:
            title = title_hl
        abstract_hl, _ = _localized_highlight(highlight, "abstract", lang)
        if abstract_hl:
            abstract = abstract_hl
        meta_hl, _ = _localized_highlight(highlight, "meta", lang)
        if meta_hl:
            meta = meta_hl

        attachment = src.get("attachment") or {}
        is_pdf = (attachment.get("content_type") == "application/pdf")
        document_url = attachment.get("content_url")

        # "Gericht" wird (wie im Frontend) aus den ersten zwei Segmenten der ID
        # abgeleitet, z.B. "CH_BGer_001_..." → "CH_BGer".
        match = _COURT_PREFIX_RE.match(hit.get("_id", ""))
        court = match.group(1) if match else hit.get("_id", "")

        hit_obj = SearchHit(
            id=hit["_id"],
            title=title,
            abstract=abstract,
            text=text,
            meta=meta,
            canton=(src.get("canton") or "").upper(),
            court=court,
            decision_date=_format_iso_date(src.get("date", "")),
            scrape_date=_format_iso_date(src.get("scrapedate", "")),
            is_pdf=is_pdf,
            url=f"{view_url}{hit['_id']}" if view_url else document_url,
            document_url=document_url,
            original_url=original_url or None,
            sort=hit.get("sort"),
        )

        if include_content:
            content = attachment.get("content")
            if content:
                # Wir hängen den Volltext an `text` an, wenn explizit gewünscht.
                hit_obj.text = content

                # content_length aus dem Index; Fallback auf die tatsächliche Länge.
                length = attachment.get("content_length")
                if not isinstance(length, int):
                    length = len(content)
                hit_obj.content_length = length

                # Elasticsearch schneidet den indexierten Text bei 100'000 Zeichen
                # ab (ingest-attachment default). In diesem Fall liefert `text` nur
                # den Anfang — wir signalisieren das und verweisen auf das
                # vollständige Originaldokument.
                if length >= INDEXED_CHARS_LIMIT:
                    hit_obj.text_truncated = True
                    if document_url:
                        # Hinweis in der Entscheidsprache (Metadatum `language`),
                        # Rückfallsprache Englisch.
                        note = _TRUNCATION_NOTE.get(
                            src.get("language"), _TRUNCATION_NOTE["en"]
                        )
                        hit_obj.text = f"{content}\n…\n{note.format(url=document_url)}"

        hits.append(hit_obj)

    return hits, total


def _parse_aggregations(raw: dict) -> Optional[Dict[str, List[AggregationBucket]]]:
    """ES-Aggregationen → flaches Dict { name: [{key, count}] }."""
    aggs = raw.get("aggregations")
    if not aggs:
        return None
    result: Dict[str, List[AggregationBucket]] = {}
    for name, body in aggs.items():
        if "buckets" in body:
            result[name] = [
                AggregationBucket(key=b.get("key_as_string") or b["key"], count=b["doc_count"])
                for b in body["buckets"]
            ]
        elif "value" in body:
            # min/max-Aggregation
            result[name] = [AggregationBucket(key=body["value"], count=1)]
    return result or None


def _next_cursor(
    hits: List[SearchHit], total: int, params: SearchParams
) -> Optional[List[Any]]:
    """Bestimmt den `search_after`-Cursor für die nächste Seite.

    Ohne Cursor-State des Clients lässt sich auf Folge-Seiten nicht zuverlässig
    vorab feststellen, ob noch weitere Treffer kommen — wir setzen den Cursor
    optimistisch, sobald die Seite voll ist. Auf der ersten Seite (kein
    `search_after` im Request) sparen wir uns den Cursor, wenn `total` bereits
    durch diese Seite abgedeckt ist; das vermeidet den häufigsten Edge-Case
    (genau `size` Treffer insgesamt).
    """
    if not hits or len(hits) < params.size:
        return None
    if params.search_after is None and total <= params.size:
        return None
    return hits[-1].sort


def _previous_chunk_id(chunk_id: str) -> Optional[str]:
    """ID des vorhergehenden Abschnitts (``…_C0008`` → ``…_C0007``), wie im Frontend."""
    match = re.match(r"^(.*)_C(\d+)$", chunk_id or "")
    if not match:
        return None
    number = int(match.group(2)) - 1
    if number < 0:
        return None
    return f"{match.group(1)}_C{number:04d}"


def _first(fields: dict, name: str, default: Any = None) -> Any:
    """Erster Wert eines ES-`fields`-Eintrags (ES liefert dort immer Listen)."""
    value = fields.get(name)
    if isinstance(value, list):
        return value[0] if value else default
    return value if value is not None else default


class EntscheidsucheClient:
    """Asynchroner HTTP-Client für die Such-API von entscheidsuche.ch."""

    def __init__(
        self,
        es_url: str,
        facets_url: str,
        timeout: float = 30.0,
        verify_ssl: bool = True,
        client: Optional[httpx.AsyncClient] = None,
        *,
        chunk_search_url: Optional[str] = None,
        micro_chunk_search_url: Optional[str] = None,
        embed_big_url: Optional[str] = None,
        embed_big_model: str = "qwen3-embedding-8b",
        embed_micro_url: Optional[str] = None,
        embed_micro_model: str = "qwen3-embedding-0.6b",
        index_micro_chunk_url: Optional[str] = None,
        micro_chunk_wait: float = DEFAULT_MICRO_CHUNK_WAIT,
        index_prefix: Optional[str] = DECISION_INDEX_PREFIX,
        view_url: Optional[str] = DEFAULT_VIEW_URL,
    ) -> None:
        self.es_url = es_url
        self.facets_url = facets_url
        self.chunk_search_url = chunk_search_url
        self.micro_chunk_search_url = micro_chunk_search_url
        self.embed_big_url = embed_big_url
        self.embed_big_model = embed_big_model
        self.embed_micro_url = embed_micro_url
        self.embed_micro_model = embed_micro_model
        self.index_micro_chunk_url = index_micro_chunk_url
        self.micro_chunk_wait = micro_chunk_wait
        self.index_prefix = index_prefix
        self.view_url = view_url
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout, verify=verify_ssl)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "EntscheidsucheClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    @property
    def semantic_available(self) -> bool:
        """True, wenn die Endpunkte der semantischen Suche konfiguriert sind."""
        return bool(self.chunk_search_url and self.embed_big_url)

    # ------------------------------------------------------------------
    # Stichwortsuche
    # ------------------------------------------------------------------

    async def search(self, params: SearchParams) -> SearchResponse:
        """Stichwortsuche mit Volltext, Filtern, Sortierung, Paginierung.

        Ist die Anfrage keine gültige Lucene-Syntax (z.B. ``Art. 271 OR`` oder
        ``5A_396/2015`` ohne Anführungszeichen), wird sie ein zweites Mal als
        Folge gewöhnlicher Suchbegriffe ausgeführt; die Antwort enthält dann
        einen entsprechenden Hinweis in ``note``.
        """
        note: Optional[str] = None
        query, statute_or = protect_statute_or(params.query)
        if statute_or:
            note = (
                'Read "OR" after the article number as the statute (Obligationenrecht), '
                "not as the Boolean operator."
            )
        body = _build_query(params, self.index_prefix, query_override=query)
        logger.debug("ES query: %s", body)
        try:
            resp = await self._post(body)
        except QuerySyntaxError:
            plain = plain_terms_query(query)
            body = _build_query(params, self.index_prefix, query_override=plain)
            try:
                resp = await self._post(body)
            except QuerySyntaxError as exc:
                raise QuerySyntaxError(
                    "The query could not be parsed. Use plain search terms, put "
                    'phrases and case numbers in double quotes (e.g. "5A_396/2015"), '
                    "and write the Boolean operators AND, OR, NOT between two terms."
                ) from exc
            note = (
                "The query was not valid search syntax (e.g. special characters such "
                "as / : ( ) or a dangling AND/OR/NOT) and was therefore searched as "
                "plain terms. Put phrases and case numbers in double quotes for an "
                "exact match."
            )
        hits, total = _parse_hits(resp, params.language, view_url=self.view_url)
        next_cursor = _next_cursor(hits, total, params)
        aggregations = _parse_aggregations(resp) if params.include_aggregations else None
        return SearchResponse(
            search_mode="keyword",
            note=note,
            total=total,
            hits=hits,
            next_cursor=next_cursor,
            aggregations=aggregations,
        )

    async def get_document(
        self, doc_id: str, lang: Optional[Language] = None
    ) -> Optional[SearchHit]:
        """Einzelnes Dokument mit vollständigem Volltext anhand seiner ID abrufen."""
        doc_id = (doc_id or "").strip()
        if not doc_id:
            raise SearchError(
                "The document id must not be empty. Pass the `id` of a hit returned "
                "by one of the search tools."
            )
        body: Dict[str, Any] = {
            "size": 1,
            "query": {
                "bool": {
                    "must": {"ids": {"values": [doc_id]}},
                    "filter": _index_filter(self.index_prefix),
                }
            },
        }
        resp = await self._post(body)
        hits, _total = _parse_hits(
            resp, lang, include_content=True, view_url=self.view_url
        )
        return hits[0] if hits else None

    async def list_hierarchy(
        self, query: str = "*", size: int = 1000
    ) -> HierarchyResponse:
        """Aggregierte Hierarchie-Buckets über die Treffermenge."""

        query, _ = protect_statute_or(query)

        def body_for(q: str) -> dict:
            return {
                "size": 0,
                "query": {
                    "bool": {
                        "must": _query_string_clause(q),
                        "filter": _index_filter(self.index_prefix),
                    }
                },
                "aggs": {
                    "hierarchy": {"terms": {"size": size, "field": "hierarchy"}}
                },
            }

        try:
            resp = await self._post(body_for(query or "*"))
        except QuerySyntaxError:
            resp = await self._post(body_for(plain_terms_query(query)))
        buckets = (
            resp.get("aggregations", {}).get("hierarchy", {}).get("buckets", [])
        )
        return HierarchyResponse(
            entries=[
                HierarchyEntry(id=b["key"], count=b["doc_count"]) for b in buckets
            ]
        )

    async def list_facets(self) -> List[FacetNode]:
        """Hierarchische Kanton/Gericht/Kammer-Struktur (Facetten-Baum)."""
        try:
            resp = await self._client.get(self.facets_url, timeout=self._timeout)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SearchError(
                "The court hierarchy (facet tree) could not be loaded from "
                "entscheidsuche.ch. Use `list_hierarchy` instead or try again later."
            ) from exc
        return _transform_facets(data)

    # ------------------------------------------------------------------
    # Semantische Suche (KI-Suche)
    # ------------------------------------------------------------------

    async def semantic_search(
        self,
        query: str,
        *,
        size: int = 10,
        language: Optional[Language] = None,
        decision_date: Optional[DateRange] = None,
        scrape_date: Optional[DateRange] = None,
        hierarchy: Optional[List[str]] = None,
        language_filter: Optional[List[Language]] = None,
        include_context: bool = False,
    ) -> SearchResponse:
        """Semantische Suche über Embeddings — Ablauf wie in der Web-Oberfläche.

        1. Anfrage einbetten (grosses Modell für die Abschnitte, kleines Modell
           für die Micro-Chunks — parallel).
        2. kNN auf dem Chunk-Index → die am besten passenden Abschnitte.
           Hierarchie- und Entscheiddatum-Filter wirken direkt im kNN.
        3. Pro Entscheid nur den besten Abschnitt behalten.
        4. Metadaten der Entscheide aus dem Hauptindex holen; Sprach- und
           Scrape-Datum-Filter wirken erst hier.
        5. Pro Treffer den am besten passenden Micro-Chunk als Auszug holen
           (siehe `_add_passages`).
        """
        query = (query or "").strip()
        if not query or query == "*":
            raise SearchError(
                "Semantic search needs a question or a description of the legal "
                "issue in natural language. Use `keyword_search` to list or filter "
                "decisions without a query."
            )
        if not self.semantic_available:
            raise SearchError(
                "Semantic search is not configured on this server. Use "
                "`keyword_search` instead."
            )

        async with _semantic_slots():
            big_vector, micro_vector = await asyncio.gather(
                self._embed(self.embed_big_url, self.embed_big_model, query),
                self._embed_optional(query),
            )

            # --- 2./3. beste Abschnitte, ein Abschnitt pro Entscheid ---------
            post_filtered = bool(language_filter) or scrape_date is not None
            chunk_count = SEMANTIC_CHUNKS_POSTFILTER if post_filtered else SEMANTIC_CHUNKS
            knn: Dict[str, Any] = {
                "field": "embedding",
                "query_vector": big_vector,
                "k": int(chunk_count * 1.5),
                "num_candidates": chunk_count * 5,
            }
            knn_filters: List[dict] = []
            if hierarchy:
                knn_filters.append({"terms": {"hierarchy": hierarchy}})
            if decision_date is not None:
                clause = _date_range_clause("date", decision_date)
                if clause is not None:
                    knn_filters.append(clause)
            if knn_filters:
                knn["filter"] = knn_filters if len(knn_filters) > 1 else knn_filters[0]

            chunk_resp = await self._post(
                {
                    "size": chunk_count,
                    "knn": knn,
                    "_source": False,
                    "fields": ["documentId"],
                },
                url=self.chunk_search_url,
            )
            best_chunk: Dict[str, Tuple[str, float]] = {}
            for hit in chunk_resp.get("hits", {}).get("hits", []):
                doc_id = _first(hit.get("fields") or {}, "documentId")
                if doc_id and doc_id not in best_chunk:
                    best_chunk[doc_id] = (hit["_id"], float(hit.get("_score") or 0.0))

            note = (
                f"Semantic search: ranked by similarity of meaning, based on the "
                f"{chunk_count} passages that best match the query. There is no "
                f"pagination and no other sort order; use `keyword_search` for "
                f"exact terms, case numbers or complete result lists."
            )
            if not best_chunk:
                return SearchResponse(search_mode="semantic", note=note, total=0, hits=[])

            # --- 4. Metadaten aus dem Hauptindex ----------------------------
            doc_filters = _index_filter(self.index_prefix) + _build_filters(
                SearchParams(
                    decision_date=decision_date,
                    scrape_date=scrape_date,
                    hierarchy=hierarchy,
                    language_filter=language_filter,
                )
            )
            doc_resp = await self._post(
                {
                    "size": len(best_chunk),
                    "_source": {"excludes": ["attachment.content"]},
                    "query": {
                        "bool": {
                            "must": {"ids": {"values": list(best_chunk)}},
                            "filter": doc_filters,
                        }
                    },
                }
            )
            hits, _total = _parse_hits(doc_resp, language, view_url=self.view_url)
            chunk_of: Dict[str, str] = {}
            for hit in hits:
                chunk_id, score = best_chunk[hit.id]
                hit.score = round(score, 4)
                hit.sort = None
                chunk_of[hit.id] = chunk_id
            hits.sort(key=lambda h: h.score or 0.0, reverse=True)
            total = len(hits)
            hits = hits[:size]

            # --- 5. Auszüge -------------------------------------------------
            await self._add_passages(hits, chunk_of, micro_vector, include_context)

        return SearchResponse(search_mode="semantic", note=note, total=total, hits=hits)

    async def _add_passages(
        self,
        hits: List[SearchHit],
        chunk_of: Dict[str, str],
        micro_vector: Optional[List[float]],
        include_context: bool,
    ) -> None:
        """Setzt `text` (bester Micro-Chunk) und optional `context` der Treffer.

        Vorgehen wie in der Web-Oberfläche (`getBestMicroChunks`):

        1. Besten Micro-Chunk des Abschnitts suchen.
        2. Gibt es keinen, die Micro-Chunks des Entscheids über
           `indexMicroChunk` indexieren lassen und erneut suchen.
        3. Gibt es dann immer noch keinen, dasselbe mit dem vorhergehenden
           Abschnitt versuchen.

        Anders als im Browser wartet der MCP-Aufruf auf die Schritte 2 und 3
        insgesamt höchstens `micro_chunk_wait` Sekunden. Treffer, für die bis
        dahin kein Auszug vorliegt, werden ohne Auszug geliefert (`text` leer);
        der Vorfall wird als Fehler geloggt, der Client bekommt keine Meldung.
        """
        if not hits:
            return
        chunk_ids = [chunk_of[h.id] for h in hits]

        micro: Dict[str, str] = {}
        if micro_vector is not None and self.micro_chunk_search_url:
            try:
                micro = await self._best_micro_chunks(chunk_ids, micro_vector)
            except SearchError as exc:
                logger.error("KI-Suche: Micro-Chunk-Suche fehlgeschlagen: %s", exc)

            missing = [h for h in hits if chunk_of[h.id] not in micro]
            if missing and self.index_micro_chunk_url:
                tasks = [
                    asyncio.create_task(
                        self._index_and_find_micro_chunk(h.id, chunk_of[h.id], micro_vector)
                    )
                    for h in missing
                ]
                done, pending = await asyncio.wait(tasks, timeout=self.micro_chunk_wait)
                for task in pending:
                    task.cancel()
                for hit, task in zip(missing, tasks):
                    text: Optional[str] = None
                    if task in done and task.exception() is None:
                        text = task.result()
                    if text:
                        micro[chunk_of[hit.id]] = text
                    elif task in pending:
                        logger.error(
                            "KI-Suche: kein Auszug für %s (Abschnitt %s): Indexierung "
                            "der Micro-Chunks nicht innerhalb von %.0f s abgeschlossen",
                            hit.id, chunk_of[hit.id], self.micro_chunk_wait,
                        )
                    else:
                        logger.error(
                            "KI-Suche: kein Auszug für %s (Abschnitt %s): auch nach "
                            "Indexierung keine Micro-Chunks gefunden (%s)",
                            hit.id, chunk_of[hit.id], task.exception() or "leer",
                        )
            elif missing:
                for hit in missing:
                    logger.error(
                        "KI-Suche: kein Auszug für %s (Abschnitt %s): keine "
                        "Micro-Chunks vorhanden und keine Indexierungs-URL konfiguriert",
                        hit.id, chunk_of[hit.id],
                    )
        else:
            logger.error(
                "KI-Suche: keine Auszüge für %d Treffer: Micro-Embedding oder "
                "Micro-Chunk-Suche nicht verfügbar", len(hits),
            )

        big: Dict[str, str] = {}
        if include_context:
            try:
                big = await self._chunk_texts(chunk_ids)
            except SearchError as exc:
                logger.error("KI-Suche: Abschnittstexte konnten nicht geladen werden: %s", exc)

        for hit in hits:
            chunk_id = chunk_of[hit.id]
            hit.text = micro.get(chunk_id, "")
            if include_context and big.get(chunk_id):
                hit.context = big[chunk_id].strip()

    async def _best_micro_chunks(
        self, chunk_ids: List[str], micro_vector: List[float]
    ) -> Dict[str, str]:
        """Bester Micro-Chunk je grossem Abschnitt: {chunkId: Text}.

        Ein einziger kNN über die Micro-Chunks aller Abschnitte; `collapse`
        liefert pro Abschnitt den am besten passenden Micro-Chunk.
        """
        k = min(max(len(chunk_ids) * 20, 50), 1000)
        resp = await self._post(
            {
                "size": len(chunk_ids),
                "knn": {
                    "field": "embedding",
                    "query_vector": micro_vector,
                    "k": k,
                    "num_candidates": min(k * 3, 5000),
                    "filter": {"terms": {"chunkId": chunk_ids}},
                },
                "collapse": {"field": "chunkId"},
                "_source": False,
                "fields": ["chunkId", "chunkText"],
            },
            url=self.micro_chunk_search_url,
        )
        result: Dict[str, str] = {}
        for hit in resp.get("hits", {}).get("hits", []):
            fields = hit.get("fields") or {}
            chunk_id = _first(fields, "chunkId")
            text = _first(fields, "chunkText", "")
            if chunk_id and text and chunk_id not in result:
                result[chunk_id] = text.strip()
        return result

    async def _index_and_find_micro_chunk(
        self, document_id: str, chunk_id: str, micro_vector: List[float]
    ) -> Optional[str]:
        """Micro-Chunks indexieren lassen und den besten suchen (wie das Frontend).

        Die Request-Bodies entsprechen exakt denen des Frontends
        (`{id, chunkId}` bzw. beim vorhergehenden Abschnitt `{id, newChunkId}`).
        """
        await self._post(
            {"id": document_id, "chunkId": chunk_id},
            url=self.index_micro_chunk_url,
            timeout=INDEX_MICRO_CHUNK_TIMEOUT,
            expect_json=False,
        )
        found = await self._best_micro_chunks([chunk_id], micro_vector)
        if found.get(chunk_id):
            return found[chunk_id]

        previous = _previous_chunk_id(chunk_id)
        if previous is None:
            return None
        await self._post(
            {"id": document_id, "newChunkId": previous},
            url=self.index_micro_chunk_url,
            timeout=INDEX_MICRO_CHUNK_TIMEOUT,
            expect_json=False,
        )
        found = await self._best_micro_chunks([previous], micro_vector)
        return found.get(previous)

    async def _chunk_texts(self, chunk_ids: List[str]) -> Dict[str, str]:
        """Texte grosser Abschnitte: {chunkId: Text}."""
        if not chunk_ids:
            return {}
        resp = await self._post(
            {
                "size": len(chunk_ids),
                "_source": False,
                "fields": ["chunkText"],
                "query": {"ids": {"values": chunk_ids}},
            },
            url=self.chunk_search_url,
        )
        return {
            hit["_id"]: _first(hit.get("fields") or {}, "chunkText", "")
            for hit in resp.get("hits", {}).get("hits", [])
        }

    async def _embed(self, url: Optional[str], model: str, query: str) -> List[float]:
        """Embedding-Vektor der Anfrage (OpenAI-kompatible /v1/embeddings-API)."""
        if not url:
            raise SearchError("Semantic search is not configured on this server.")
        data = await self._post(
            {
                "model": model,
                "input": f"Instruct: {QUERY_INSTRUCTION}\nQuery: {query}",
                "encoding_format": "float",
            },
            url=url.rstrip("/") + "/v1/embeddings",
        )
        try:
            vector = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise SearchError(
                "The embedding service of entscheidsuche.ch returned an unexpected "
                "response. Try again later or use `keyword_search`."
            ) from exc
        return vector

    async def _embed_optional(self, query: str) -> Optional[List[float]]:
        """Embedding für die Micro-Chunks; ein Fehler kostet nur die Auszüge."""
        if not self.embed_micro_url or not self.micro_chunk_search_url:
            return None
        try:
            return await self._embed(self.embed_micro_url, self.embed_micro_model, query)
        except SearchError as exc:
            logger.warning("Micro-Embedding fehlgeschlagen: %s", exc)
            return None

    # ------------------------------------------------------------------
    # interne Helfer
    # ------------------------------------------------------------------

    async def _post(
        self,
        body: dict,
        url: Optional[str] = None,
        timeout: Optional[float] = None,
        expect_json: bool = True,
    ) -> dict:
        """POST an den Upstream; übersetzt Fehler in verständliche Meldungen.

        Die Meldungen gehen an MCP-Clients (Sprachmodelle) und enthalten
        deshalb weder interne URLs noch rohe HTTP-Fehlertexte.
        """
        try:
            resp = await self._client.post(
                url or self.es_url,
                json=body,
                timeout=timeout or self._timeout,
                headers={"Content-Type": "application/json"},
            )
        except httpx.TimeoutException as exc:
            raise SearchError(
                "The search backend of entscheidsuche.ch did not answer in time. "
                "Try again, or narrow the query."
            ) from exc
        except httpx.HTTPError as exc:
            raise SearchError(
                "The search backend of entscheidsuche.ch could not be reached. "
                "Try again later."
            ) from exc
        if resp.status_code == 400 and url is None:
            # Auf dem Entscheid-Index bedeutet 400 praktisch immer: ungültige
            # Lucene-Syntax in der Anfrage.
            raise QuerySyntaxError("The search backend rejected the query (HTTP 400).")
        if resp.status_code >= 400:
            raise SearchError(
                f"The search backend of entscheidsuche.ch returned an error "
                f"(HTTP {resp.status_code}). Try again later."
            )
        if not expect_json:
            return {}
        try:
            data = resp.json()
        except ValueError as exc:
            raise SearchError(
                "The search backend of entscheidsuche.ch returned an unreadable "
                "response. Try again later."
            ) from exc
        if url is None:
            _raise_on_shard_failures(data, self.index_prefix)
        return data


def _raise_on_shard_failures(data: Any, index_prefix: Optional[str] = None) -> None:
    """Teil-Fehlschläge einzelner Shards erkennen.

    Scheitert eine Anfrage nur auf einem Teil der Shards, antwortet
    Elasticsearch mit HTTP 200 und einer (scheinbar gültigen) leeren oder
    unvollständigen Trefferliste. Ein Syntaxfehler in der Anfrage äussert sich
    genau so — ohne diese Prüfung sähe er wie „0 Treffer" aus.

    Gezählt werden nur Fehlschläge auf den Entscheid-Indizes (`index_prefix`).
    Der Such-Endpunkt läuft über alle Indizes des Clusters; auf den übrigen
    (Chunk-Indizes, interne Indizes) scheitert z.B. die Sortierung nach `id`
    regelmässig — das ist belanglos, weil der Index-Filter sie ohnehin
    ausschliesst.
    """
    if not isinstance(data, dict):
        return
    shards = data.get("_shards")
    if not isinstance(shards, dict) or not shards.get("failed"):
        return
    failures = [
        failure
        for failure in shards.get("failures") or []
        if isinstance(failure, dict)
        and (not index_prefix or str(failure.get("index") or "").startswith(index_prefix))
    ]
    if not failures:
        return
    reasons = {(failure.get("reason") or {}).get("type") for failure in failures}
    if reasons & {"query_shard_exception", "parse_exception", "parsing_exception"}:
        raise QuerySyntaxError("The search backend could not parse the query.")
    logger.warning(
        "Upstream meldet fehlgeschlagene Shards auf Entscheid-Indizes (%s)",
        ", ".join(sorted(r for r in reasons if r)),
    )


def result_refs(hits: List[SearchHit]) -> List[ResultRef]:
    """Kompakte (id, title, url)-Liste der Treffer; Hervorhebungen entfernt."""
    return [
        ResultRef(
            id=h.id,
            title=re.sub(r"</?em>", "", h.title or "") or h.id,
            url=h.url or h.document_url,
        )
        for h in hits
    ]


def _facet_label(node: dict) -> FacetLabel:
    return FacetLabel(
        de=node.get("de"), fr=node.get("fr"), it=node.get("it"), en=node.get("en")
    )


def _children(node: dict, *keys: str) -> dict:
    """Unterknoten unter dem ersten vorhandenen Schlüssel.

    `Facetten.json` von entscheidsuche.ch verwendet ``gerichte`` → ``kammern``;
    das ältere Format ``Quellen`` → ``Sammlungen`` wird weiterhin gelesen. Leere
    Ebenen stehen in der Datei teils als leere Liste statt als Objekt.
    """
    for key in keys:
        value = node.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _transform_facets(data: dict) -> List[FacetNode]:
    """`Facetten.json` → unsere Pydantic-Struktur (Kanton → Gericht → Kammer)."""
    if not isinstance(data, dict):
        return []
    facets: List[FacetNode] = []
    for canton_id, canton in data.items():
        if not isinstance(canton, dict):
            continue
        courts: List[FacetNode] = []
        for court_id, court in _children(canton, "gerichte", "Quellen").items():
            if not isinstance(court, dict):
                continue
            chambers = [
                FacetNode(id=chamber_id, label=_facet_label(chamber))
                for chamber_id, chamber in _children(
                    court, "kammern", "Sammlungen"
                ).items()
                if isinstance(chamber, dict)
            ]
            courts.append(
                FacetNode(
                    id=court_id,
                    label=_facet_label(court),
                    children=chambers or None,
                )
            )
        facets.append(
            FacetNode(id=canton_id, label=_facet_label(canton), children=courts)
        )
    return facets
