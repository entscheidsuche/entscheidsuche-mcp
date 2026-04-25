"""Elasticsearch/OpenSearch-Client für entscheidsuche.ch.

Diese Modul portiert die Suchlogik aus dem TypeScript-Frontend von
entscheidsuche.ch (`SearchUtil.ts`) nach Python.

Der Client baut Elasticsearch-Queries für:

* Volltext-Suche mit Lucene-`query_string`-Syntax (Anführungszeichen → Phrasen)
* Filter nach Entscheiddatum, Scraping-Datum, Hierarchie (Kanton/Gericht/Kammer),
  Sprache
* Aggregationen (Buckets) für Facetten-Auswertung
* Highlight-Snippets mit Auszügen rund um die Treffer

Die rohe Elasticsearch-Antwort wird in saubere, dokumentierte Pydantic-Modelle
übersetzt.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
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
    SearchHit,
    SearchParams,
    SearchResponse,
    SortOrder,
)

logger = logging.getLogger(__name__)

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
        d = datetime.utcfromtimestamp(value / 1000)
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


def _build_query(params: SearchParams) -> dict:
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
                "must": {
                    "query_string": {
                        "query": params.query or "*",
                        "default_operator": "AND",
                        "type": "cross_fields",
                        "fields": QUERY_FIELDS,
                    }
                }
            }
        },
        "sort": [{sort_field: "desc"}, {"id": "desc"}],
        "highlight": {
            "fields": {
                f"title.{params.language.value}": {"number_of_fragments": 0},
                f"abstract.{params.language.value}": {"number_of_fragments": 0},
                "attachment.content": {},
            }
        },
    }

    filters = _build_filters(params)
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


def _parse_hits(
    raw: dict, lang: Language, include_content: bool = False
) -> Tuple[List[SearchHit], int]:
    """Konvertiert die ES-Treffer in unsere `SearchHit`-Modelle."""
    hits_node = raw.get("hits", {})
    total = hits_node.get("total", {}).get("value", 0)
    hits: List[SearchHit] = []

    for hit in hits_node.get("hits", []):
        src = hit.get("_source", {}) or {}
        highlight = hit.get("highlight", {}) or {}

        title = (src.get("title") or {}).get(lang.value, "") or ""
        abstract = (src.get("abstract") or {}).get(lang.value, "") or ""
        meta = (src.get("meta") or {}).get(lang.value, "") or ""
        original_url = (src.get("url") or {}).get(lang.value, "") or ""

        text = _join_highlight(highlight.get("attachment.content"))
        if highlight.get(f"title.{lang.value}"):
            title = _join_highlight(highlight[f"title.{lang.value}"])
        if highlight.get(f"abstract.{lang.value}"):
            abstract = _join_highlight(highlight[f"abstract.{lang.value}"])
        if highlight.get(f"meta.{lang.value}"):
            meta = _join_highlight(highlight[f"meta.{lang.value}"])

        attachment = src.get("attachment") or {}
        is_pdf = (attachment.get("content_type") == "application/pdf")
        document_url = attachment.get("content_url")

        # "Gericht" wird (wie im Frontend) aus den ersten zwei Segmenten der ID
        # abgeleitet, z.B. "CH_BGer_001_..." → "CH_BGer".
        match = _COURT_PREFIX_RE.match(hit.get("_id", ""))
        court = (match.group(1) if match else hit.get("_id", "")).upper()

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
            document_url=document_url,
            original_url=original_url or None,
            sort=hit.get("sort"),
        )

        if include_content:
            content = attachment.get("content")
            if content:
                # Wir hängen den Volltext an `text` an, wenn explizit gewünscht.
                hit_obj.text = content

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


class EntscheidsucheClient:
    """Asynchroner HTTP-Client für die entscheidsuche-Elasticsearch-API."""

    def __init__(
        self,
        es_url: str,
        facets_url: str,
        timeout: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.es_url = es_url
        self.facets_url = facets_url
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "EntscheidsucheClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Such-API
    # ------------------------------------------------------------------

    async def search(self, params: SearchParams) -> SearchResponse:
        """Hauptsuche mit Volltext, Filtern, Sortierung, Paginierung."""
        body = _build_query(params)
        logger.debug("ES query: %s", body)
        resp = await self._post(body)
        hits, total = _parse_hits(resp, params.language)
        next_cursor = hits[-1].sort if hits and len(hits) == params.size else None
        aggregations = _parse_aggregations(resp) if params.include_aggregations else None
        return SearchResponse(
            total=total,
            hits=hits,
            next_cursor=next_cursor,
            aggregations=aggregations,
        )

    async def get_document(
        self, doc_id: str, lang: Language, include_content: bool = False
    ) -> Optional[SearchHit]:
        """Einzelnes Dokument anhand seiner ID abrufen."""
        body: Dict[str, Any] = {
            "size": 1,
            "query": {"ids": {"values": [doc_id]}},
        }
        if not include_content:
            body["_source"] = {"excludes": ["attachment.content"]}
        resp = await self._post(body)
        hits, _total = _parse_hits(resp, lang, include_content=include_content)
        return hits[0] if hits else None

    async def list_hierarchy(
        self, query: str = "*", size: int = 1000
    ) -> HierarchyResponse:
        """Aggregierte Hierarchie-Buckets über die Treffermenge."""
        body = {
            "size": 0,
            "query": {
                "bool": {
                    "must": {
                        "query_string": {
                            "query": query or "*",
                            "default_operator": "AND",
                            "type": "cross_fields",
                            "fields": QUERY_FIELDS,
                        }
                    }
                }
            },
            "aggs": {
                "hierarchy": {"terms": {"size": size, "field": "hierarchy"}}
            },
        }
        resp = await self._post(body)
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
        resp = await self._client.get(self.facets_url, timeout=self._timeout)
        resp.raise_for_status()
        data = resp.json()
        return _transform_facets(data)

    # ------------------------------------------------------------------
    # interne Helfer
    # ------------------------------------------------------------------

    async def _post(self, body: dict) -> dict:
        resp = await self._client.post(
            self.es_url,
            json=body,
            timeout=self._timeout,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        return resp.json()


def _transform_facets(data: dict) -> List[FacetNode]:
    """`Facetten.json` → unsere Pydantic-Struktur."""
    if not isinstance(data, dict):
        return []
    facets: List[FacetNode] = []
    for key, value in data.items():
        first_level: List[FacetNode] = []
        for sec_key, sec_val in (value.get("Quellen") or {}).items():
            third_level: List[FacetNode] = []
            for third_key, third_val in (sec_val.get("Sammlungen") or {}).items():
                third_level.append(
                    FacetNode(
                        id=third_key,
                        label=FacetLabel(
                            de=third_val.get("de"),
                            fr=third_val.get("fr"),
                            it=third_val.get("it"),
                            en=third_val.get("en"),
                        ),
                    )
                )
            label = FacetLabel(
                de=sec_val.get("de"),
                fr=sec_val.get("fr"),
                it=sec_val.get("it"),
                en=sec_val.get("en"),
            )
            if len(third_level) <= 1:
                first_level.append(FacetNode(id=sec_key, label=label))
            else:
                first_level.append(
                    FacetNode(id=sec_key, label=label, children=third_level)
                )
        facets.append(
            FacetNode(
                id=key,
                label=FacetLabel(
                    de=value.get("de"),
                    fr=value.get("fr"),
                    it=value.get("it"),
                    en=value.get("en"),
                ),
                children=first_level,
            )
        )
    return facets
