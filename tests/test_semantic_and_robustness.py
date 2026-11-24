"""Tests für die semantische Suche, robuste Anfragen und die Tool-Registrierung.

Abgedeckt sind die Fehler, die der Live-Test vom 23.11.2026 gefunden hat:

* Anfragen wie ``Art. 271 OR`` oder ``5A_396/2015`` endeten mit HTTP 400.
* `list_facets` lieferte nur Kantone (Facetten.json nutzt ``gerichte``/``kammern``).
* Tools hatten keine Annotationen; `initialize` meldete die SDK-Version.
* Unbekannte Dokument-IDs lieferten eine leere Antwort ohne Hinweis.

Alle Tests laufen ohne Netzwerk gegen einen Mock-Transport.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date

import httpx
import pytest

from entscheidsuche_mcp import __version__
from entscheidsuche_mcp.models import DateRange, Language, SearchParams
from entscheidsuche_mcp.search import (
    DECISION_INDEX_PREFIX,
    EntscheidsucheClient,
    QuerySyntaxError,
    SearchError,
    _build_query,
    _previous_chunk_id,
    _transform_facets,
    plain_terms_query,
    protect_statute_or,
    result_refs,
)
from entscheidsuche_mcp.server import build_server, keyword_reason

ES = "https://example.test/es/_search"
CHUNKS = "https://example.test/es/chunks/_search"
MICRO = "https://example.test/es/micro/_search"
EMBED_BIG = "https://example.test/embed/big"
EMBED_MICRO = "https://example.test/embed/micro"
INDEX_MICRO = "https://example.test/indexMicroChunk"
INDEX_FILTER = {"prefix": {"_index": DECISION_INDEX_PREFIX}}


def _doc(doc_id: str, language: str = "de") -> dict:
    return {
        "_id": doc_id,
        "_source": {
            "title": {"de": f"Titel {doc_id}"},
            "date": "2024-05-13",
            "scrapedate": "2026-01-01",
            "canton": "ZH",
            "hierarchy": ["ZH", "ZH_OG"],
            "attachment": {
                "content_type": "text/html",
                "content_url": f"https://entscheidsuche.ch/docs/{doc_id}.html",
                "language": language,
            },
        },
    }


def _client(handler, **kwargs) -> EntscheidsucheClient:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EntscheidsucheClient(
        es_url=ES,
        facets_url="https://example.test/Facetten.json",
        client=http_client,
        chunk_search_url=CHUNKS,
        micro_chunk_search_url=MICRO,
        embed_big_url=EMBED_BIG,
        embed_micro_url=EMBED_MICRO,
        index_micro_chunk_url=INDEX_MICRO,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Anfragen robust machen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, expected",
    [
        ("Art. 271 OR", "Art. 271 or"),
        ("5A_396/2015", "5A_396\\/2015"),
        ("(Eigenbedarf AND", "\\(Eigenbedarf and"),
        ("BGE 142 III 1: Miete / Pacht", "BGE 142 III 1\\: Miete \\/ Pacht"),
        ('"Eigenbedarf', '\\"Eigenbedarf'),
        ("", "*"),
    ],
)
def test_plain_terms_query(query, expected):
    assert plain_terms_query(query) == expected


@pytest.mark.parametrize(
    "query, expected, changed",
    [
        ("Art. 271 OR", 'Art. 271 "OR"', True),
        ("Art. 271 OR Kündigung", 'Art. 271 "OR" Kündigung', True),
        ("Art. 41 Abs. 1 OR Haftung", 'Art. 41 Abs. 1 "OR" Haftung', True),
        ("art. 271a Abs. 1 lit. a OR", 'art. 271a Abs. 1 lit. a "OR"', True),
        # echter Boolescher Operator bleibt unverändert
        ("Miete OR Pacht", "Miete OR Pacht", False),
        ("Art. 8 ZGB OR Art. 9 ZGB", "Art. 8 ZGB OR Art. 9 ZGB", False),
    ],
)
def test_protect_statute_or(query, expected, changed):
    assert protect_statute_or(query) == (expected, changed)


def test_build_query_restricts_to_decision_indices():
    """Der Such-Endpunkt läuft über alle Indizes — jede Abfrage wird eingeschränkt."""
    body = _build_query(SearchParams(query="Mietzins"))
    assert body["query"]["bool"]["filter"] == [INDEX_FILTER]

    body = _build_query(SearchParams(query="Mietzins", hierarchy=["ZH_OG"]))
    assert body["query"]["bool"]["filter"][0] == INDEX_FILTER
    assert {"terms": {"hierarchy": ["ZH_OG"]}} in body["query"]["bool"]["filter"]

    # ohne Präfix (ES_URL zeigt direkt auf einen Index-Pattern): kein Filter
    body = _build_query(SearchParams(query="Mietzins"), index_prefix="")
    assert "filter" not in body["query"]["bool"]


@pytest.mark.asyncio
async def test_search_retries_as_plain_terms_on_http_400():
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["query"]["bool"]["must"]["query_string"]["query"]
        queries.append(query)
        if query == "5A_396/2015":
            return httpx.Response(400, json={"error": {"type": "search_phase_execution_exception"}})
        return httpx.Response(200, json={"hits": {"total": {"value": 1}, "hits": [_doc("A")]}})

    async with _client(handler) as client:
        resp = await client.search(SearchParams(query="5A_396/2015"))

    assert queries == ["5A_396/2015", "5A_396\\/2015"]
    assert resp.total == 1
    assert resp.note and "plain terms" in resp.note
    assert resp.search_mode == "keyword"


@pytest.mark.asyncio
async def test_search_detects_partial_shard_failure_as_syntax_error():
    """HTTP 200 mit fehlgeschlagenen Entscheid-Shards sah früher wie «0 Treffer» aus."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "_shards": {
                        "total": 77,
                        "failed": 54,
                        "failures": [
                            {
                                "index": "entscheidsuche.v2-ch_bger",
                                "reason": {"type": "query_shard_exception"},
                            }
                        ],
                    },
                    "hits": {"total": {"value": 0}, "hits": []},
                },
            )
        return httpx.Response(200, json={"hits": {"total": {"value": 3}, "hits": []}})

    async with _client(handler) as client:
        resp = await client.search(SearchParams(query="(Eigenbedarf AND"))

    assert calls == 2
    assert resp.total == 3
    assert resp.note


@pytest.mark.asyncio
async def test_search_ignores_shard_failures_on_other_indices():
    """Die Sortierung nach `id` scheitert auf den Chunk-Indizes — das ist belanglos."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "_shards": {
                    "total": 77,
                    "failed": 8,
                    "failures": [
                        {
                            "index": "embeddings_qwen3-embedding-8b",
                            "reason": {"type": "query_shard_exception"},
                        }
                    ],
                },
                "hits": {"total": {"value": 5}, "hits": [_doc("A")]},
            },
        )

    async with _client(handler) as client:
        resp = await client.search(SearchParams(query="Eigenbedarf"))

    assert calls == 1
    assert resp.total == 5
    assert resp.note is None


@pytest.mark.asyncio
async def test_search_error_message_is_clean_when_query_cannot_be_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "boom"})

    async with _client(handler) as client:
        with pytest.raises(QuerySyntaxError) as excinfo:
            await client.search(SearchParams(query="x"))

    message = str(excinfo.value)
    assert "http" not in message.lower()
    assert "example.test" not in message
    assert "quotes" in message


@pytest.mark.asyncio
async def test_statute_or_is_quoted_before_sending():
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(json.loads(request.content)["query"]["bool"]["must"]["query_string"]["query"])
        return httpx.Response(200, json={"hits": {"total": {"value": 1}, "hits": []}})

    async with _client(handler) as client:
        resp = await client.search(SearchParams(query="Art. 271 OR Kündigung"))

    assert queries == ['Art. 271 "OR" Kündigung']
    assert resp.note and "Obligationenrecht" in resp.note


@pytest.mark.asyncio
async def test_upstream_errors_are_translated():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    async with _client(handler) as client:
        with pytest.raises(SearchError) as excinfo:
            await client.search(SearchParams(query="x"))
    assert "HTTP 502" in str(excinfo.value)
    assert "example.test" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_get_document_rejects_empty_id_and_filters_by_index():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"hits": {"total": {"value": 0}, "hits": []}})

    async with _client(handler) as client:
        with pytest.raises(SearchError):
            await client.get_document("   ")
        assert await client.get_document("does-not-exist") is None

    assert len(bodies) == 1
    assert bodies[0]["query"]["bool"]["filter"] == [INDEX_FILTER]
    assert bodies[0]["query"]["bool"]["must"] == {"ids": {"values": ["does-not-exist"]}}


@pytest.mark.asyncio
async def test_list_hierarchy_retries_and_filters_by_index():
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["query"]["bool"]["filter"] == [INDEX_FILTER]
        query = body["query"]["bool"]["must"]["query_string"]["query"]
        queries.append(query)
        if query == "foo/bar":
            return httpx.Response(400, json={})
        return httpx.Response(
            200,
            json={"aggregations": {"hierarchy": {"buckets": [{"key": "CH", "doc_count": 7}]}}},
        )

    async with _client(handler) as client:
        resp = await client.list_hierarchy("foo/bar")

    assert queries == ["foo/bar", "foo\\/bar"]
    assert resp.entries[0].id == "CH"


# ---------------------------------------------------------------------------
# Facetten
# ---------------------------------------------------------------------------


def test_transform_facets_current_format():
    """Format von https://entscheidsuche.ch/docs/Facetten.json (gerichte/kammern)."""
    data = {
        "ZH": {
            "de": "Zürich",
            "fr": "Zurich",
            "it": "Zurigo",
            "gerichte": {
                "ZH_OG": {
                    "de": "Obergericht",
                    "fr": "Obergericht",
                    "it": "Obergericht",
                    "kammern": {
                        "ZH_OG_001": {"spider": "ZH_OG", "de": "Zivilkammern"},
                        "ZH_OG_002": {"spider": "ZH_OG", "de": "Strafkammern"},
                    },
                },
                # leere Ebene steht in der Datei als leere Liste
                "ZH_XX": {"de": "Sonstige", "kammern": []},
            },
        }
    }
    nodes = _transform_facets(data)
    assert [n.id for n in nodes] == ["ZH"]
    courts = nodes[0].children
    assert [c.id for c in courts] == ["ZH_OG", "ZH_XX"]
    assert [k.id for k in courts[0].children] == ["ZH_OG_001", "ZH_OG_002"]
    assert courts[0].children[0].label.de == "Zivilkammern"
    assert courts[1].children is None


# ---------------------------------------------------------------------------
# Semantische Suche
# ---------------------------------------------------------------------------


class Upstream:
    """Mock der Such-API: Embeddings, Chunk-kNN, Hauptindex, Micro-Chunks."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.chunk_hits = [
            ("B_C0003", "B", 0.91),
            ("A_C0001", "A", 0.90),
            ("B_C0007", "B", 0.80),  # zweiter Abschnitt desselben Entscheids
            ("C_C0002", "C", 0.70),
        ]
        self.docs = {"A": _doc("A"), "B": _doc("B", "fr"), "C": _doc("C")}
        self.micro = {"B_C0003": "Auszug B", "A_C0001": "Auszug A", "C_C0002": "Auszug C"}
        self.micro_after_index: dict[str, str] = {}
        self.index_delay = 0.0
        self.indexed: list[dict] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content)
        self.calls.append((url, body))
        if url.startswith(EMBED_BIG):
            return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
        if url.startswith(EMBED_MICRO):
            return httpx.Response(200, json={"data": [{"embedding": [0.3]}]})
        if url == INDEX_MICRO:
            if self.index_delay:
                await asyncio.sleep(self.index_delay)
            self.indexed.append(body)
            self.micro.update(self.micro_after_index)
            return httpx.Response(200, text="ok")
        if url == CHUNKS and "knn" in body:
            return httpx.Response(
                200,
                json={
                    "hits": {
                        "hits": [
                            {"_id": cid, "_score": score, "fields": {"documentId": [doc]}}
                            for cid, doc, score in self.chunk_hits
                        ]
                    }
                },
            )
        if url == CHUNKS:
            ids = body["query"]["ids"]["values"]
            return httpx.Response(
                200,
                json={
                    "hits": {
                        "hits": [
                            {"_id": cid, "fields": {"chunkText": [f"Abschnitt {cid}"]}}
                            for cid in ids
                        ]
                    }
                },
            )
        if url == MICRO:
            ids = body["knn"]["filter"]["terms"]["chunkId"]
            return httpx.Response(
                200,
                json={
                    "hits": {
                        "hits": [
                            {"_id": cid + "_M", "fields": {"chunkId": [cid], "chunkText": [self.micro[cid]]}}
                            for cid in ids
                            if cid in self.micro
                        ]
                    }
                },
            )
        if url == ES:
            ids = body["query"]["bool"]["must"]["ids"]["values"]
            wanted_lang = None
            for clause in body["query"]["bool"]["filter"]:
                if "terms" in clause and "attachment.language" in clause["terms"]:
                    wanted_lang = clause["terms"]["attachment.language"]
            hits = [
                self.docs[i]
                for i in ids
                if i in self.docs
                and (wanted_lang is None or self.docs[i]["_source"]["attachment"]["language"] in wanted_lang)
            ]
            return httpx.Response(200, json={"hits": {"total": {"value": len(hits)}, "hits": hits}})
        raise AssertionError(f"unerwarteter Aufruf {url}")

    def bodies(self, url: str) -> list[dict]:
        return [body for called, body in self.calls if called == url]


@pytest.mark.asyncio
async def test_semantic_search_ranks_by_best_chunk_and_adds_passages():
    upstream = Upstream()
    async with _client(upstream) as client:
        resp = await client.semantic_search("Darf der Vermieter kündigen?", size=10)

    assert resp.search_mode == "semantic"
    assert resp.total == 3
    # ein Treffer pro Entscheid, sortiert nach dem besten Abschnitt
    assert [h.id for h in resp.hits] == ["B", "A", "C"]
    assert [h.score for h in resp.hits] == [0.91, 0.9, 0.7]
    assert [h.text for h in resp.hits] == ["Auszug B", "Auszug A", "Auszug C"]
    assert resp.hits[0].url == "https://entscheidsuche.ch/view/B"
    assert resp.next_cursor is None
    assert all(h.context is None for h in resp.hits)

    # Anfrage wird mit der Instruktion des Frontends eingebettet
    embed_body = upstream.bodies(EMBED_BIG + "/v1/embeddings")[0]
    assert embed_body["model"] == "qwen3-embedding-8b"
    assert embed_body["input"].startswith("Instruct: Given a legal search query")
    assert embed_body["input"].endswith("Query: Darf der Vermieter kündigen?")

    knn = upstream.bodies(CHUNKS)[0]
    assert knn["size"] == 100 and knn["knn"]["k"] == 150 and knn["knn"]["num_candidates"] == 500
    assert "filter" not in knn["knn"]

    # Micro-Chunks: ein Aufruf für alle Treffer, zusammengefasst je Abschnitt
    micro = upstream.bodies(MICRO)
    assert len(micro) == 1
    assert micro[0]["collapse"] == {"field": "chunkId"}
    assert micro[0]["knn"]["filter"] == {"terms": {"chunkId": ["B_C0003", "A_C0001", "C_C0002"]}}
    assert upstream.indexed == []


@pytest.mark.asyncio
async def test_semantic_search_filters_and_context():
    upstream = Upstream()
    async with _client(upstream) as client:
        resp = await client.semantic_search(
            "résiliation du bail",
            size=1,
            hierarchy=["ZH_OG"],
            decision_date=DateRange(**{"from": date(2024, 1, 1)}),
            language_filter=[Language.fr],
            include_context=True,
        )

    # Hierarchie und Entscheiddatum wirken im kNN …
    knn = upstream.bodies(CHUNKS)[0]["knn"]
    assert knn["filter"] == [
        {"terms": {"hierarchy": ["ZH_OG"]}},
        {"range": {"date": {"gte": "2024-01-01"}}},
    ]
    # … der Sprachfilter erst auf dem Hauptindex; dafür mehr Abschnitte
    assert upstream.bodies(CHUNKS)[0]["size"] == 300
    doc_filter = upstream.bodies(ES)[0]["query"]["bool"]["filter"]
    assert INDEX_FILTER in doc_filter
    assert {"terms": {"attachment.language": ["fr"]}} in doc_filter

    assert [h.id for h in resp.hits] == ["B"]
    assert resp.total == 1
    assert resp.hits[0].context == "Abschnitt B_C0003"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "   ", "*"])
async def test_semantic_search_needs_a_query(query):
    upstream = Upstream()
    async with _client(upstream) as client:
        with pytest.raises(SearchError):
            await client.semantic_search(query)
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_semantic_search_without_results():
    upstream = Upstream()
    upstream.chunk_hits = []
    async with _client(upstream) as client:
        resp = await client.semantic_search("irgendwas")
    assert resp.total == 0 and resp.hits == []


def test_previous_chunk_id():
    assert _previous_chunk_id("ZH_OG_001_NG1_2024-01-01_C0008") == "ZH_OG_001_NG1_2024-01-01_C0007"
    assert _previous_chunk_id("X_C0000") is None
    assert _previous_chunk_id("kein-chunk") is None


@pytest.mark.asyncio
async def test_missing_micro_chunks_are_indexed_like_the_frontend():
    """Fehlt der Micro-Chunk: indexMicroChunk aufrufen, dann erneut suchen."""
    upstream = Upstream()
    del upstream.micro["A_C0001"]
    upstream.micro_after_index = {"A_C0001": "Auszug A nach Indexierung"}

    async with _client(upstream) as client:
        resp = await client.semantic_search("Frage", size=10)

    assert upstream.indexed == [{"id": "A", "chunkId": "A_C0001"}]
    assert {h.id: h.text for h in resp.hits}["A"] == "Auszug A nach Indexierung"


@pytest.mark.asyncio
async def test_missing_micro_chunks_fall_back_to_previous_chunk(caplog):
    upstream = Upstream()
    del upstream.micro["B_C0003"]
    upstream.micro_after_index = {"B_C0002": "Auszug aus dem vorhergehenden Abschnitt"}

    async with _client(upstream) as client:
        resp = await client.semantic_search("Frage", size=10)

    # zweiter Versuch mit dem vorhergehenden Abschnitt — Body wie im Frontend
    assert upstream.indexed == [
        {"id": "B", "chunkId": "B_C0003"},
        {"id": "B", "newChunkId": "B_C0002"},
    ]
    assert resp.hits[0].id == "B"
    assert resp.hits[0].text == "Auszug aus dem vorhergehenden Abschnitt"


@pytest.mark.asyncio
async def test_hit_without_passage_is_returned_without_text_and_logged(caplog):
    """Nach Ablauf der Wartezeit: Treffer ohne Auszug, Fehler nur im Log."""
    upstream = Upstream()
    del upstream.micro["C_C0002"]
    upstream.index_delay = 1.0  # Indexierung dauert länger als die Wartezeit

    with caplog.at_level(logging.ERROR, logger="entscheidsuche_mcp.search"):
        async with _client(upstream, micro_chunk_wait=0.05) as client:
            resp = await client.semantic_search("Frage", size=10)

    by_id = {h.id: h for h in resp.hits}
    assert by_id["C"].text == ""
    assert by_id["A"].text == "Auszug A"
    assert len(resp.hits) == 3
    # keine Fehlermeldung an den Client …
    assert "C" not in (resp.note or "").split()
    assert "Auszug" not in (resp.note or "")
    # … aber ein Fehler im Log
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("kein Auszug für C" in message and "0 s" in message for message in errors)


@pytest.mark.asyncio
async def test_failed_micro_embedding_does_not_break_the_search(caplog):
    upstream = Upstream()
    original = upstream.__call__

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(EMBED_MICRO):
            return httpx.Response(500, text="down")
        return await original(request)

    with caplog.at_level(logging.ERROR, logger="entscheidsuche_mcp.search"):
        async with _client(handler) as client:
            resp = await client.semantic_search("Frage")

    assert [h.id for h in resp.hits] == ["B", "A", "C"]
    assert all(h.text == "" for h in resp.hits)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


def test_result_refs_strip_highlighting():
    upstream = Upstream()
    from entscheidsuche_mcp.search import _parse_hits

    hits, _ = _parse_hits({"hits": {"total": {"value": 1}, "hits": [upstream.docs["A"]]}}, None)
    hits[0].title = "Urteil <em>5A_396</em>/2015"
    refs = result_refs(hits)
    assert refs[0].model_dump() == {
        "id": "A",
        "title": "Urteil 5A_396/2015",
        "url": "https://entscheidsuche.ch/view/A",
    }


# ---------------------------------------------------------------------------
# `search`: Wahl zwischen KI-Suche und Stichwortsuche
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "Darf der Vermieter wegen Eigenbedarf kündigen, wenn seine Tochter einziehen will?",
        "Eigenbedarf",
        "Haftung des Tierhalters bei Hundebiss",
        "Art. 271 OR",  # OR = Obligationenrecht, kein Operator
        "Art. 271 OR Kündigung missbräuchlich",
        "résiliation du bail pour besoin propre",
    ],
)
def test_search_uses_semantic_search_for_natural_language(query):
    assert keyword_reason(query) is None


@pytest.mark.parametrize(
    "query",
    [
        "",
        "*",
        "5A_396/2015",
        "4A.123/2007",
        "BGE 142 III 1",
        "ATF 142 III 1",
        "ZB.2021.48",
        "NG240011",
        "B-6350/2015",
        '"dringender Eigenbedarf"',
        "Eigenbedarf AND Tochter",
        "Miete OR Pacht",
        "Eigenbed*",
        "Eigenbed?rf",
    ],
)
def test_search_uses_keyword_search_for_exact_queries(query):
    assert keyword_reason(query) is not None


def test_search_uses_keyword_search_for_keyword_only_parameters():
    assert keyword_reason("Eigenbedarf", sort="date") is not None
    assert keyword_reason("Eigenbedarf", search_after=[1, "x"]) is not None
    assert keyword_reason("Eigenbedarf", include_aggregations=True) is not None


# ---------------------------------------------------------------------------
# Tool-Registrierung
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registered_tools_and_annotations():
    mcp = build_server()
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert list(tools) == [
        "search",
        "semantic_search",
        "keyword_search",
        "search_by_case_number",
        "fetch_document",
        "fetch",
        "list_hierarchy",
        "list_facets",
        "server_info",
    ]
    for tool in tools.values():
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.annotations.open_world_hint is True, tool.name

    # `fetch` ist ein Alias: gleiche Parameter wie `fetch_document`
    assert tools["fetch"].input_schema["properties"].keys() == tools["fetch_document"].input_schema["properties"].keys()
    assert tools["fetch"].input_schema["required"] == ["id"]
    # die KI-Suche verlangt eine Anfrage, die Standardsuche nicht (Alt-Clients)
    assert tools["semantic_search"].input_schema["required"] == ["query"]
    assert "required" not in tools["search"].input_schema
    assert tools["semantic_search"].input_schema["properties"]["size"]["maximum"] == 50


def test_server_reports_its_own_version():
    """`initialize` meldete früher die Version des MCP-SDK."""
    mcp = build_server()
    assert mcp.version == __version__
