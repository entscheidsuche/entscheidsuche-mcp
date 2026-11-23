"""Datenmodelle (Pydantic) für die MCP-Tools."""

from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Eingabe-Modelle (MCP-Tool-Parameter)
# ---------------------------------------------------------------------------


class Language(str, Enum):
    """Verfügbare Sprachen der Entscheide."""

    de = "de"
    fr = "fr"
    it = "it"


class SortOrder(str, Enum):
    """Sortierreihenfolge der Suchergebnisse."""

    relevance = "relevance"
    date = "date"
    scrapedate = "scrapedate"


class DateRange(BaseModel):
    """Datums-Range mit optionalem Anfang und Ende (ISO-8601, YYYY-MM-DD)."""

    model_config = ConfigDict(extra="forbid")

    from_: Optional[date] = Field(
        default=None,
        alias="from",
        description="Startdatum inklusive, im Format YYYY-MM-DD",
    )
    to: Optional[date] = Field(
        default=None,
        description="Enddatum inklusive, im Format YYYY-MM-DD",
    )


class SearchParams(BaseModel):
    """Parameter für die Stichwortsuche (`keyword_search`)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    query: str = Field(
        default="*",
        description=(
            "Volltext-Suchanfrage. Unterstützt Lucene-Query-Syntax: "
            "Anführungszeichen für Phrasen-Suche (z.B. exakte Geschäftsnummer "
            "wie \"BGE 142 III 1\"), AND/OR/NOT-Operatoren, Wildcards (*, ?). "
            "Wenn nichts gesucht werden soll: '*'."
        ),
    )
    language: Optional[Language] = Field(
        default=None,
        description=(
            "Bevorzugte Sprache für Highlight-Auswertung und Titel/Abstract-"
            "Rückgabe. Wenn nicht gesetzt, wird das erste vorhandene Sprachfeld "
            "des Dokuments verwendet (de → fr → it). "
            "Hat KEINE Wirkung als Filter — dafür `language_filter` verwenden."
        ),
    )
    sort: SortOrder = Field(
        default=SortOrder.relevance,
        description="Sortierreihenfolge: 'relevance', 'date' (Entscheiddatum), 'scrapedate'.",
    )
    size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Anzahl Treffer pro Seite (1–100).",
    )
    search_after: Optional[List[Any]] = Field(
        default=None,
        description=(
            "Cursor für die nächste Seite — der `next_cursor`-Wert aus der vorherigen "
            "Antwort. Beim ersten Request leer lassen."
        ),
    )

    # Filter
    decision_date: Optional[DateRange] = Field(
        default=None,
        description="Filter nach Entscheiddatum (Datum des Entscheids).",
    )
    scrape_date: Optional[DateRange] = Field(
        default=None,
        description="Filter nach Scraping-Datum (wann der Entscheid eingelesen wurde).",
    )
    hierarchy: Optional[List[str]] = Field(
        default=None,
        description=(
            "Filter nach Hierarchie-IDs (Kanton/Gericht/Kammer). "
            "IDs aus dem `list_hierarchy`-Tool übernehmen, z.B. ['CH_BGE', 'ZH_OG']. "
            "Mehrere IDs werden mit OR verknüpft."
        ),
    )
    language_filter: Optional[List[Language]] = Field(
        default=None,
        description="Filter nach Sprache(n) der Dokumente, z.B. ['de', 'fr'].",
    )
    include_aggregations: bool = Field(
        default=False,
        description=(
            "Wenn True: Aggregationen (Hierarchie-, Sprach-, Datums-Verteilungen) "
            "über die Suchergebnisse mitliefern."
        ),
    )


class GetDocumentParams(BaseModel):
    """Parameter für das `fetch_document`-Tool."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="Dokument-ID (entspricht `_id` im Index, z.B. 'CH_BGer_001_5A_123_2024_2024-06-15').",
    )
    language: Optional[Language] = Field(
        default=None,
        description=(
            "Bevorzugte Sprache für Titel/Abstract-Rückgabe. Wenn nicht gesetzt, "
            "wird das erste vorhandene Sprachfeld zurückgegeben."
        ),
    )


class ListHierarchyParams(BaseModel):
    """Parameter für das `list_hierarchy`-Tool."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        default="*",
        description="Optionale Volltext-Anfrage zur Eingrenzung der Hierarchie-Aggregation.",
    )
    size: int = Field(
        default=1000,
        ge=1,
        le=10000,
        description="Maximale Anzahl Hierarchie-Einträge.",
    )


# ---------------------------------------------------------------------------
# Ausgabe-Modelle (Tool-Antworten)
# ---------------------------------------------------------------------------


class SearchHit(BaseModel):
    """Ein einzelner Such-Treffer."""

    id: str
    title: str = ""
    abstract: str = ""
    text: str = Field(
        default="",
        description=(
            "Excerpt from the decision. Keyword search: highlight snippet with "
            "the matched terms wrapped in <em>…</em>. Semantic search: the "
            "passage of the decision that best matches the query. "
            "`fetch_document`: the full text."
        ),
    )
    meta: str = ""
    canton: str = ""
    court: str = Field(default="", description="Court (derived from the ID).")
    decision_date: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD.")
    scrape_date: Optional[str] = Field(default=None, description="ISO date YYYY-MM-DD.")
    is_pdf: bool = False
    url: Optional[str] = Field(
        default=None,
        description="Web page of the decision on entscheidsuche.ch (use this link for citations).",
    )
    document_url: Optional[str] = Field(
        default=None, description="Direct download link to the original document."
    )
    original_url: Optional[str] = Field(
        default=None, description="URL of the original source web page."
    )
    content_length: Optional[int] = Field(
        default=None,
        description=(
            "Length of the indexed full text in characters. Only set by "
            "`fetch_document`."
        ),
    )
    text_truncated: bool = Field(
        default=False,
        description=(
            "True if the indexed full text was truncated by Elasticsearch at "
            "100,000 characters. In that case `text` contains only the beginning "
            "of the decision; the complete document is available at `document_url`."
        ),
    )
    sort: Optional[List[Any]] = Field(
        default=None, description="Sort values of the hit (for search_after)."
    )
    score: Optional[float] = Field(
        default=None,
        description=(
            "Semantic search only: similarity between the query and the best "
            "matching passage of this decision (higher is better)."
        ),
    )
    context: Optional[str] = Field(
        default=None,
        description=(
            "Semantic search with `include_context=true` only: the larger "
            "section of the decision (up to about 6,000 characters) that "
            "contains the passage in `text`."
        ),
    )


class AggregationBucket(BaseModel):
    key: Any
    count: int


class ResultRef(BaseModel):
    """Kompakter Treffer-Verweis (id, title, url) für Clients, die genau dieses Format erwarten."""

    id: str
    title: str = ""
    url: Optional[str] = None


class SearchResponse(BaseModel):
    """Antwort der Such-Tools."""

    search_mode: str = Field(
        default="keyword",
        description="Which search was executed: 'keyword' or 'semantic'.",
    )
    note: Optional[str] = Field(
        default=None,
        description=(
            "Remarks on how the query was executed, e.g. that it was not valid "
            "search syntax and was searched as plain terms."
        ),
    )
    total: int = Field(
        description=(
            "Keyword search: number of all matching decisions. Semantic search: "
            "number of decisions among the best-matching passages."
        )
    )
    hits: List[SearchHit]
    next_cursor: Optional[List[Any]] = Field(
        default=None,
        description=(
            "Cursor for the next page. Pass it again as `search_after` in the "
            "next request. None if there are no further results."
        ),
    )
    aggregations: Optional[dict[str, List[AggregationBucket]]] = None
    results: Optional[List[ResultRef]] = Field(
        default=None,
        description=(
            "Only set by the `search` tool: compact list (id, title, url) of the "
            "same decisions as in `hits`, in the same order."
        ),
    )


class HierarchyEntry(BaseModel):
    id: str
    count: int


class HierarchyResponse(BaseModel):
    entries: List[HierarchyEntry]


class FacetLabel(BaseModel):
    de: Optional[str] = None
    fr: Optional[str] = None
    it: Optional[str] = None
    en: Optional[str] = None


class FacetNode(BaseModel):
    id: str
    label: FacetLabel
    children: Optional[List["FacetNode"]] = None


class FacetsResponse(BaseModel):
    facets: List[FacetNode]


FacetNode.model_rebuild()
