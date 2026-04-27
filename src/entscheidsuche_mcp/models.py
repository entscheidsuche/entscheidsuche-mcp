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
    """Parameter für das `search`-Tool."""

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
    language: Language = Field(
        default=Language.de,
        description=(
            "Sprache für Highlight-Auswertung und Titel/Abstract-Rückgabe. "
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
    """Parameter für das `get_document`-Tool."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(
        description="Dokument-ID (entspricht `_id` im Index, z.B. 'CH_BGer_001_5A_123_2024_2024-06-15').",
    )
    language: Language = Field(
        default=Language.de,
        description="Sprache für Titel/Abstract-Rückgabe.",
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
    text: str = Field(default="", description="Highlight-Auszug aus dem Volltext.")
    meta: str = ""
    canton: str = ""
    court: str = Field(default="", description="Gericht (abgeleitet aus der ID).")
    decision_date: Optional[str] = Field(default=None, description="ISO-Datum YYYY-MM-DD.")
    scrape_date: Optional[str] = Field(default=None, description="ISO-Datum YYYY-MM-DD.")
    is_pdf: bool = False
    document_url: Optional[str] = Field(
        default=None, description="Direkter Download-Link auf das Originaldokument."
    )
    original_url: Optional[str] = Field(
        default=None, description="URL der ursprünglichen Quell-Webseite."
    )
    sort: Optional[List[Any]] = Field(
        default=None, description="Sort-Werte des Treffers (für search_after)."
    )


class AggregationBucket(BaseModel):
    key: Any
    count: int


class SearchResponse(BaseModel):
    """Antwort des `search`-Tools."""

    total: int
    hits: List[SearchHit]
    next_cursor: Optional[List[Any]] = Field(
        default=None,
        description=(
            "Cursor für die nächste Seite. Im nächsten Request als `search_after` "
            "wieder mitgeben. None, wenn keine weiteren Treffer vorhanden sind."
        ),
    )
    aggregations: Optional[dict[str, List[AggregationBucket]]] = None


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
