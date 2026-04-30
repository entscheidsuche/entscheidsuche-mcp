"""Unit-Tests für den Query-Builder und den Result-Parser.

Die Tests laufen ohne Netzwerk: der `EntscheidsucheClient` wird mit einem
Mock-`httpx`-Client befüttert.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from entscheidsuche_mcp.models import (
    DateRange,
    Language,
    SearchParams,
    SortOrder,
)
from entscheidsuche_mcp.search import (
    EntscheidsucheClient,
    _build_filters,
    _build_query,
    _calendar_interval,
    _parse_aggregations,
    _parse_hits,
    _serialise_date,
    _transform_facets,
)


# ---------------------------------------------------------------------------
# _serialise_date
# ---------------------------------------------------------------------------


def test_serialise_date_from_date():
    assert _serialise_date(date(2024, 6, 15)) == "2024-06-15"


def test_serialise_date_from_iso_string():
    assert _serialise_date("2024-06-15") == "2024-06-15"


def test_serialise_date_from_positive_ms():
    assert _serialise_date(1_700_000_000_000) == 1_700_000_000_000


def test_serialise_date_from_negative_ms():
    # Negative Millisekunden → ISO-Datum (für Daten vor 1970)
    result = _serialise_date(-1_000_000_000_000)
    assert isinstance(result, str)
    assert result.startswith("19")
    assert len(result) == 10  # YYYY-MM-DD


# ---------------------------------------------------------------------------
# Filter-Builder
# ---------------------------------------------------------------------------


def test_build_filters_decision_date_range():
    params = SearchParams(
        query="*",
        decision_date=DateRange(**{"from": date(2024, 1, 1), "to": date(2024, 12, 31)}),
    )
    filters = _build_filters(params)
    assert filters == [
        {"range": {"date": {"gte": "2024-01-01", "lte": "2024-12-31"}}}
    ]


def test_build_filters_open_ended_date():
    params = SearchParams(
        query="*",
        decision_date=DateRange(**{"from": date(2024, 1, 1)}),
    )
    filters = _build_filters(params)
    assert filters == [{"range": {"date": {"gte": "2024-01-01"}}}]


def test_build_filters_hierarchy_and_language():
    params = SearchParams(
        query="*",
        hierarchy=["CH_BGer", "ZH_OG"],
        language_filter=[Language.de, Language.fr],
    )
    filters = _build_filters(params)
    assert {"terms": {"hierarchy": ["CH_BGer", "ZH_OG"]}} in filters
    assert {"terms": {"attachment.language": ["de", "fr"]}} in filters


def test_build_filters_combined():
    params = SearchParams(
        query="Mietzins",
        decision_date=DateRange(**{"from": date(2024, 1, 1), "to": date(2024, 12, 31)}),
        scrape_date=DateRange(**{"from": date(2024, 6, 1)}),
        hierarchy=["ZH_OG"],
        language_filter=[Language.de],
    )
    filters = _build_filters(params)
    assert len(filters) == 4


# ---------------------------------------------------------------------------
# Query-Builder
# ---------------------------------------------------------------------------


def test_build_query_minimal():
    params = SearchParams(query="Mietzins", language=Language.de)
    body = _build_query(params)
    assert body["size"] == 20
    assert body["track_total_hits"] is True
    assert body["_source"] == {"excludes": ["attachment.content"]}
    qs = body["query"]["bool"]["must"]["query_string"]
    assert qs["query"] == "Mietzins"
    assert qs["default_operator"] == "AND"
    assert qs["type"] == "cross_fields"
    assert "title.*^5" in qs["fields"]
    assert "attachment.content" in qs["fields"]
    # Sort: Relevance + id-Tiebreaker
    assert body["sort"] == [{"_score": "desc"}, {"id": "desc"}]
    # Highlight: sprachspezifisch
    assert "title.de" in body["highlight"]["fields"]
    assert "abstract.de" in body["highlight"]["fields"]
    assert "attachment.content" in body["highlight"]["fields"]
    # Keine Aggregationen, weil nicht angefordert
    assert "aggs" not in body


def test_build_query_phrase_for_geschaeftsnummer():
    """Geschäftsnummer wird in Anführungszeichen als Phrase übergeben."""
    params = SearchParams(query='"BGE 142 III 1"', language=Language.de)
    body = _build_query(params)
    assert body["query"]["bool"]["must"]["query_string"]["query"] == '"BGE 142 III 1"'


def test_build_query_sort_by_date():
    params = SearchParams(query="*", sort=SortOrder.date)
    body = _build_query(params)
    assert body["sort"][0] == {"date": "desc"}


def test_build_query_sort_by_scrapedate():
    params = SearchParams(query="*", sort=SortOrder.scrapedate)
    body = _build_query(params)
    assert body["sort"][0] == {"scrapedate": "desc"}


def test_build_query_with_filters():
    params = SearchParams(
        query="Mietrecht",
        hierarchy=["ZH_OG"],
        decision_date=DateRange(**{"from": date(2024, 1, 1), "to": date(2024, 12, 31)}),
    )
    body = _build_query(params)
    filter_clauses = body["query"]["bool"]["filter"]
    assert any("hierarchy" in str(c) for c in filter_clauses)
    assert any("date" in str(c) for c in filter_clauses)


def test_build_query_pagination_via_search_after():
    params = SearchParams(
        query="*", search_after=[1234567890, "id-xyz"], include_aggregations=True
    )
    body = _build_query(params)
    assert body["search_after"] == [1234567890, "id-xyz"]
    # Aggregationen werden bei Folge-Seiten ausgelassen
    assert "aggs" not in body


def test_build_query_with_aggregations():
    params = SearchParams(query="*", include_aggregations=True)
    body = _build_query(params)
    assert "aggs" in body
    assert "hierarchy" in body["aggs"]
    assert "language" in body["aggs"]
    assert "decision_date" in body["aggs"]
    assert "scrape_date" in body["aggs"]


def test_build_query_aggs_skip_filtered_facets():
    """Wenn Hierarchie schon gefiltert ist, keine Hierarchie-Aggregation."""
    params = SearchParams(
        query="*",
        hierarchy=["ZH_OG"],
        include_aggregations=True,
    )
    body = _build_query(params)
    assert "hierarchy" not in body["aggs"]
    assert "language" in body["aggs"]


def test_build_query_size_limit():
    params = SearchParams(query="*", size=100)
    body = _build_query(params)
    assert body["size"] == 100


# ---------------------------------------------------------------------------
# _calendar_interval
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "from_,to,expected",
    [
        (date(2024, 1, 1), date(2024, 1, 30), "day"),       # < 40 Tage
        (date(2024, 1, 1), date(2024, 6, 30), "week"),       # < 280 Tage
        (date(2024, 1, 1), date(2026, 1, 1), "month"),       # < 1200 Tage
        (date(2020, 1, 1), date(2026, 1, 1), "quarter"),     # > 1200 Tage
        (None, None, "quarter"),                              # ohne Range
    ],
)
def test_calendar_interval(from_, to, expected):
    if from_ is None and to is None:
        assert _calendar_interval(None) == expected
    else:
        assert (
            _calendar_interval(DateRange(**{"from": from_, "to": to})) == expected
        )


# ---------------------------------------------------------------------------
# Result-Parser
# ---------------------------------------------------------------------------


SAMPLE_HIT = {
    "_id": "CH_BGer_001_5A_123_2024_2024-06-15",
    "_source": {
        "title": {"de": "Beschwerde gegen Mietzinserhöhung", "fr": "..."},
        "abstract": {"de": "Abstract DE", "fr": "..."},
        "meta": {"de": "Meta DE"},
        "url": {"de": "https://www.bger.ch/abc"},
        "date": "2024-06-15",
        "scrapedate": "2024-09-12",
        "canton": "ch",
        "attachment": {
            "content_type": "application/pdf",
            "content_url": "https://...pdf",
            "language": "de",
        },
    },
    "highlight": {
        "attachment.content": ["… <em>Mietzins</em> …", "… weiterer Auszug …"],
        "title.de": ["Beschwerde gegen <em>Mietzins</em>erhöhung"],
    },
    "sort": [1718409600000, "CH_BGer_001_5A_123_2024_2024-06-15"],
}


def test_parse_hits_basic():
    raw = {"hits": {"total": {"value": 1}, "hits": [SAMPLE_HIT]}}
    hits, total = _parse_hits(raw, Language.de)
    assert total == 1
    assert len(hits) == 1
    h = hits[0]
    assert h.id == "CH_BGer_001_5A_123_2024_2024-06-15"
    assert h.title == "Beschwerde gegen <em>Mietzins</em>erhöhung"
    assert "Mietzins" in h.text
    assert h.canton == "CH"
    assert h.court == "CH_BGer"
    assert h.decision_date == "2024-06-15"
    assert h.scrape_date == "2024-09-12"
    assert h.is_pdf is True
    assert h.document_url == "https://...pdf"
    assert h.original_url == "https://www.bger.ch/abc"
    assert h.sort == [1718409600000, "CH_BGer_001_5A_123_2024_2024-06-15"]


def test_parse_hits_falls_back_to_other_language_when_requested_missing():
    """Wenn das angefragte Sprachfeld leer ist, wird auf die nächste verfügbare
    Sprache zurückgegriffen (de → fr → it)."""
    hit = {
        "_id": "VD_TC_001_xy",
        "_source": {
            "title": {"fr": "Titre en français"},
            "abstract": {"fr": "Résumé"},
            "url": {"fr": "https://example.test/fr"},
            "date": "2024-01-02",
            "scrapedate": "2024-01-03",
            "canton": "vd",
            "attachment": {},
        },
        "highlight": {},
        "sort": [123, "VD_TC_001_xy"],
    }
    raw = {"hits": {"total": {"value": 1}, "hits": [hit]}}
    hits, _ = _parse_hits(raw, Language.de)
    assert hits[0].title == "Titre en français"
    assert hits[0].abstract == "Résumé"
    assert hits[0].original_url == "https://example.test/fr"


def test_parse_hits_with_no_language_uses_first_available():
    hit = {
        "_id": "TI_TC_001_xy",
        "_source": {
            "title": {"it": "Titolo italiano", "fr": "Titre français"},
            "abstract": {"it": "Sommario"},
            "url": {},
            "date": "2024-01-02",
            "canton": "ti",
            "attachment": {},
        },
        "highlight": {},
        "sort": [123, "TI_TC_001_xy"],
    }
    raw = {"hits": {"total": {"value": 1}, "hits": [hit]}}
    hits, _ = _parse_hits(raw, None)
    # Fallback-Reihenfolge ist de → fr → it; ohne `de` wird `fr` genommen.
    assert hits[0].title == "Titre français"
    assert hits[0].abstract == "Sommario"


def test_build_query_highlight_wildcard_when_no_language():
    params = SearchParams(query="Mietzins", language=None)
    body = _build_query(params)
    fields = body["highlight"]["fields"]
    assert "title.*" in fields
    assert "abstract.*" in fields
    assert "attachment.content" in fields
    assert "title.de" not in fields


def test_parse_hits_invalid_date_becomes_none():
    hit = dict(SAMPLE_HIT)
    hit["_source"] = dict(SAMPLE_HIT["_source"])
    hit["_source"]["date"] = ""
    raw = {"hits": {"total": {"value": 1}, "hits": [hit]}}
    hits, _ = _parse_hits(raw, Language.de)
    assert hits[0].decision_date is None


def test_parse_aggregations_terms_and_min_max():
    raw = {
        "aggregations": {
            "hierarchy": {"buckets": [{"key": "CH_BGer", "doc_count": 100}]},
            "min_decision_date": {"value": 1_500_000_000_000},
        }
    }
    aggs = _parse_aggregations(raw)
    assert aggs is not None
    assert aggs["hierarchy"][0].key == "CH_BGer"
    assert aggs["hierarchy"][0].count == 100
    assert aggs["min_decision_date"][0].key == 1_500_000_000_000


# ---------------------------------------------------------------------------
# Facetten-Transformation
# ---------------------------------------------------------------------------


SAMPLE_FACETS = {
    "CH": {
        "de": "Bund",
        "fr": "Confédération",
        "it": "Confederazione",
        "en": "Federal",
        "Quellen": {
            "CH_BGer": {
                "de": "Bundesgericht",
                "fr": "Tribunal fédéral",
                "it": "Tribunale federale",
                "en": "Federal Supreme Court",
                "Sammlungen": {
                    "CH_BGer_Pub": {
                        "de": "publiziert",
                        "fr": "publié",
                        "it": "pubblicato",
                        "en": "published",
                    },
                    "CH_BGer_Unp": {
                        "de": "unpubliziert",
                        "fr": "non publié",
                        "it": "non pubblicato",
                        "en": "unpublished",
                    },
                },
            }
        },
    }
}


def test_transform_facets():
    nodes = _transform_facets(SAMPLE_FACETS)
    assert len(nodes) == 1
    assert nodes[0].id == "CH"
    assert nodes[0].label.de == "Bund"
    assert nodes[0].children is not None
    assert nodes[0].children[0].id == "CH_BGer"
    # Hat 2 Sammlungen → wird mit children ausgegeben
    assert nodes[0].children[0].children is not None
    assert len(nodes[0].children[0].children) == 2


def test_transform_facets_preserves_single_child_leaf_ids():
    sample_facets = {
        "ZH": {
            "de": "Zuerich",
            "Quellen": {
                "ZH_OG": {
                    "de": "Obergericht",
                    "Sammlungen": {
                        "ZH_OG_ZK": {
                            "de": "Zivilkammer",
                        }
                    },
                }
            },
        }
    }

    nodes = _transform_facets(sample_facets)
    assert len(nodes) == 1
    assert nodes[0].children is not None
    assert nodes[0].children[0].id == "ZH_OG"
    assert nodes[0].children[0].children is not None
    assert len(nodes[0].children[0].children) == 1
    assert nodes[0].children[0].children[0].id == "ZH_OG_ZK"


# ---------------------------------------------------------------------------
# EntscheidsucheClient mit Mock-Transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_search_with_mock_transport():
    """End-to-end-Test des Clients gegen einen Mock-Transport."""

    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json
        captured.append({
            "url": str(request.url),
            "body": _json.loads(request.content),
        })
        return httpx.Response(
            200,
            json={
                "hits": {"total": {"value": 1}, "hits": [SAMPLE_HIT]},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = EntscheidsucheClient(
            es_url="https://example.test/_search",
            facets_url="https://example.test/Facetten.json",
            client=http_client,
        )
        params = SearchParams(query='"BGE 142 III 1"', language=Language.de)
        resp = await client.search(params)

    assert resp.total == 1
    assert resp.hits[0].id == SAMPLE_HIT["_id"]
    assert captured[0]["body"]["query"]["bool"]["must"]["query_string"]["query"] == '"BGE 142 III 1"'


@pytest.mark.asyncio
async def test_client_get_document_returns_none_when_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"total": {"value": 0}, "hits": []}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = EntscheidsucheClient(
            es_url="https://example.test/_search",
            facets_url="https://example.test/Facetten.json",
            client=http_client,
        )
        result = await client.get_document("doesnotexist", Language.de)
        assert result is None


@pytest.mark.asyncio
async def test_client_get_document_always_returns_full_text():
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        captured.append(_json.loads(request.content))
        hit = dict(SAMPLE_HIT)
        hit["_source"] = dict(SAMPLE_HIT["_source"])
        attachment = dict(SAMPLE_HIT["_source"]["attachment"])
        attachment["content"] = "Volltext des Entscheids"
        hit["_source"]["attachment"] = attachment
        return httpx.Response(200, json={"hits": {"total": {"value": 1}, "hits": [hit]}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = EntscheidsucheClient(
            es_url="https://example.test/_search",
            facets_url="https://example.test/Facetten.json",
            client=http_client,
        )
        result = await client.get_document("someid", Language.de)

    assert result is not None
    assert result.text == "Volltext des Entscheids"
    assert "_source" not in captured[0] or captured[0].get("_source") != {"excludes": ["attachment.content"]}


@pytest.mark.asyncio
async def test_client_list_hierarchy_parses_buckets():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hits": {"total": {"value": 0}, "hits": []},
                "aggregations": {
                    "hierarchy": {
                        "buckets": [
                            {"key": "CH_BGer", "doc_count": 1234},
                            {"key": "ZH_OG", "doc_count": 567},
                        ]
                    }
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = EntscheidsucheClient(
            es_url="https://example.test/_search",
            facets_url="https://example.test/Facetten.json",
            client=http_client,
        )
        resp = await client.list_hierarchy("Mietzins")
        assert len(resp.entries) == 2
        assert resp.entries[0].id == "CH_BGer"
        assert resp.entries[0].count == 1234


@pytest.mark.asyncio
async def test_client_search_pagination_yields_next_cursor():
    """Wenn `size` Hits zurückkommen, wird `next_cursor` aus dem letzten Hit genommen."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hits": {
                    "total": {"value": 100},
                    "hits": [SAMPLE_HIT, SAMPLE_HIT],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = EntscheidsucheClient(
            es_url="https://example.test/_search",
            facets_url="https://example.test/Facetten.json",
            client=http_client,
        )
        params = SearchParams(query="*", size=2)
        resp = await client.search(params)
        assert resp.next_cursor == SAMPLE_HIT["sort"]


@pytest.mark.asyncio
async def test_client_search_no_next_cursor_when_fewer_hits():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"hits": {"total": {"value": 1}, "hits": [SAMPLE_HIT]}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = EntscheidsucheClient(
            es_url="https://example.test/_search",
            facets_url="https://example.test/Facetten.json",
            client=http_client,
        )
        params = SearchParams(query="*", size=20)
        resp = await client.search(params)
        assert resp.next_cursor is None


@pytest.mark.asyncio
async def test_client_search_no_next_cursor_when_total_equals_first_page_size():
    """Edge-Case: erste Seite mit genau `size` Treffern und `total == size`.

    Ohne Sonderbehandlung würde der Client einen Cursor liefern, der dann auf
    eine leere Folge-Seite zeigt. Auf der ersten Seite kennen wir `total`, also
    fixen wir den Fall hier.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "hits": {
                    "total": {"value": 2},
                    "hits": [SAMPLE_HIT, SAMPLE_HIT],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = EntscheidsucheClient(
            es_url="https://example.test/_search",
            facets_url="https://example.test/Facetten.json",
            client=http_client,
        )
        params = SearchParams(query="*", size=2)
        resp = await client.search(params)
        assert resp.next_cursor is None
