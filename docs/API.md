# Schnittstellenbeschreibung — entscheidsuche-mcp

Dieser Server stellt die Suchfunktionen von [entscheidsuche.ch](https://entscheidsuche.ch)
über das **Model Context Protocol** (MCP) bereit. Transport: **Streamable HTTP**
(JSON-RPC 2.0 über `POST`).

Endpunkt in Produktion:

```
https://mcp.entscheidsuche.ch/mcp
```

Der Pfad ist konfigurierbar (`MCP_PATH`, Default `/mcp`).

Zum Ausprobieren ohne eigenen Client gibt es die Testoberfläche
[`https://mcp.entscheidsuche.ch/test`](https://mcp.entscheidsuche.ch/test): Sie
lädt die Tools vom Server, baut aus dem Eingabeschema ein Formular und zeigt
Request, `curl`-Befehl und Antwort.

## Inhalt

- [Protokoll-Grundlagen](#protokoll-grundlagen)
- [Welches Tool wofür?](#welches-tool-wofür)
- [Tools](#tools)
  - [`search`](#tool-search)
  - [`semantic_search`](#tool-semantic_search)
  - [`keyword_search`](#tool-keyword_search)
  - [`search_by_case_number`](#tool-search_by_case_number)
  - [`fetch_document` / `fetch`](#tool-fetch_document--fetch)
  - [`list_hierarchy`](#tool-list_hierarchy)
  - [`list_facets`](#tool-list_facets)
  - [`server_info`](#tool-server_info)
- [Datentypen](#datentypen)
- [Beispiel-Workflows](#beispiel-workflows)
- [Fehlerbehandlung](#fehlerbehandlung)
- [Änderungen in Version 0.3.0](#änderungen-in-version-030)
- [Änderungen in Version 0.2.0](#änderungen-in-version-020)

---

## Protokoll-Grundlagen

Streamable-HTTP-Transport gemäß
[MCP-Spec 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http):

- **POST `/mcp`** — JSON-RPC-Request. Diese Instanz antwortet mit einer
  einzelnen `application/json`-Antwort (JSON-Response-Modus), nicht mit einem
  Event-Stream.
- **GET `/mcp`** — ohne `Accept: text/event-stream` liefert der Server eine kurze
  JSON-Auskunft über den Endpunkt (für Browser und Verzeichnis-Crawler).
- **Protokollversionen:** 2024-11-05 bis 2025-11-25 mit `initialize`, ausserdem
  2026-07-28. In dieser Version entfällt der Handshake: Der Client sendet
  `MCP-Protocol-Version: 2026-07-28` und `Mcp-Method` als Header und nennt
  Protokollversion, Fähigkeiten und `clientInfo` in `params._meta`; mit
  `server/discover` fragt er Versionen und Fähigkeiten des Servers ab.
- Die Instanz läuft im **stateless HTTP**-Modus. Ein `Mcp-Session-Id`-Header
  ist nicht erforderlich; `initialize` kann trotzdem wie gewohnt gesendet werden.
- Empfohlener Header: `Accept: application/json, text/event-stream`. Requests
  mit nur `application/json`, `*/*` oder ohne `Accept` werden ebenfalls
  angenommen.
- Alle Tools sind als nur lesend annotiert (`readOnlyHint: true`,
  `destructiveHint: false`, `idempotentHint: true`).
- Tool-Antworten enthalten das Ergebnis doppelt: als `structuredContent`
  (Objekt gemäß `outputSchema`) und als JSON-Text in `content[0].text`.

---

## Welches Tool wofür?

| Aufgabe | Tool |
| --- | --- |
| Rechtsfrage oder Sachverhalt in natürlicher Sprache | `search` (oder direkt `semantic_search`) |
| Bestimmte Wörter, Phrasen, Artikel, Namen; vollständige Trefferlisten; Sortierung nach Datum; Paginierung | `keyword_search` |
| Geschäftsnummer, Aktenzeichen, BGE-Zitat | `search_by_case_number` (oder `search`) |
| Volltext eines Treffers lesen | `fetch_document` (Alias `fetch`) |
| IDs und Namen der Gerichte für den Filter `hierarchy` | `list_hierarchy`, `list_facets` |

Die zwei Sucharten im Vergleich:

| | KI-Suche (`semantic_search`) | Stichwortsuche (`keyword_search`) |
| --- | --- | --- |
| Sucht nach | Bedeutung (Embeddings) | Wörtern (Volltextindex) |
| Geeignete Anfrage | ganze Sätze: Frage, Sachverhalt, Konzept | Begriffe, Phrasen, Boolesche Ausdrücke |
| Sprache | sprachübergreifend | nur die eingegebenen Wörter |
| Reihenfolge | inhaltliche Nähe (`score`) | Relevanz, Entscheiddatum oder Scrape-Datum |
| Umfang | Entscheide unter den 100 am besten passenden Textabschnitten | alle Treffer |
| Paginierung | nein | ja (`next_cursor` → `search_after`) |
| Auszug (`text`) | am besten passender Abschnitt des Entscheids | Fundstellen, hervorgehoben mit `<em>…</em>` |

---

## Tools

### Tool: `search`

Standardsuche für Clients, die nicht zwischen den Sucharten wählen. Führt die
**KI-Suche** aus (wie `semantic_search`) und wechselt automatisch zur
**Stichwortsuche** (wie `keyword_search`), wenn eine der folgenden Bedingungen
zutrifft — geprüft in dieser Reihenfolge:

1. `sort` ist nicht `relevance`, oder `search_after` oder `include_aggregations`
   ist gesetzt (diese Parameter gibt es nur in der Stichwortsuche).
2. Die Anfrage ist leer oder `*`.
3. Die Anfrage ist als Ganzes eine Geschäftsnummer oder ein Zitat, z.B.
   `5A_396/2015`, `BGE 142 III 1`, `ZB.2021.48`, `NG240011`. Sie wird dann als
   exakte Phrase gesucht.
4. Die Anfrage enthält eine Phrase in Anführungszeichen.
5. Die Anfrage enthält Boolesche Operatoren zwischen zwei Begriffen
   (`AND`, `OR`, `NOT` in Grossbuchstaben). `OR` direkt nach einer
   Artikelangabe (`Art. 271 OR`) zählt nicht als Operator.
6. Die Anfrage enthält Wildcards (`Mietz*`, `Eigenbed?rf`). Ein Fragezeichen am
   Satzende ist kein Wildcard.

Welche Suche gelaufen ist, steht in `search_mode` (`"semantic"` oder
`"keyword"`); bei einem Wechsel nennt `note` den Grund.

#### Parameter

| Name | Typ | Default | Beschreibung |
| --- | --- | --- | --- |
| `query` | `string` | `"*"` | Rechtsfrage oder Sachverhalt in natürlicher Sprache (beliebige Sprache), oder Geschäftsnummer / Zitat. |
| `language` | `"de"\|"fr"\|"it"` | – | Bevorzugte Sprache für Titel und Abstract. Wirkt **nicht** als Filter. |
| `size` | `int` (1–100) | `10` | Anzahl Entscheide. Die KI-Suche liefert höchstens 50. |
| `decision_date_from` / `decision_date_to` | `date` (`YYYY-MM-DD`) | – | Entscheiddatum von / bis (inklusive). |
| `scrape_date_from` / `scrape_date_to` | `date` | – | Datum der Aufnahme in den Index von / bis. |
| `hierarchy` | `string[]` | – | Hierarchie-IDs, z.B. `["CH_BGer"]` oder `["ZH_OG", "BE"]`. Mehrere IDs gelten als ODER. Gross-/Kleinschreibung beachten. |
| `language_filter` | `("de"\|"fr"\|"it")[]` | – | Nur Entscheide in diesen Sprachen. |
| `include_context` | `bool` | `false` | Nur KI-Suche: pro Treffer zusätzlich den grösseren Abschnitt um den Auszug liefern (Feld `context`, bis etwa 6000 Zeichen). |
| `sort` | `"relevance"\|"date"\|"scrapedate"` | `"relevance"` | Nur Stichwortsuche. |
| `search_after` | `array` | – | Nur Stichwortsuche: Cursor aus `next_cursor`. |
| `include_aggregations` | `bool` | `false` | Nur Stichwortsuche: Verteilungen mitliefern. |

#### Antwort

Ein [`SearchResponse`](#searchresponse). Zusätzlich zu `hits` enthält die
Antwort von `search` das Feld `results`: eine kompakte Liste
`{id, title, url}` derselben Entscheide in derselben Reihenfolge. Dieses
Format erwartet ChatGPT von einem Tool namens `search`.

#### Beispiele

```json
{ "name": "search", "arguments": { "query": "Darf der Vermieter wegen Eigenbedarf kündigen, wenn seine Tochter einziehen will?" } }
```

→ `search_mode: "semantic"`

```json
{ "name": "search", "arguments": { "query": "5A_396/2015" } }
```

→ `search_mode: "keyword"`, `note: "Keyword search was used because the query looks like a case number or citation."`

---

### Tool: `semantic_search`

KI-Suche nach Bedeutung. Die Anfrage wird mit einem Embedding-Modell in einen
Vektor umgerechnet und mit den Textabschnitten aller Entscheide verglichen —
dasselbe Verfahren wie die Schaltfläche «KI-Suche» auf entscheidsuche.ch.

Ablauf:

1. Die 100 am besten passenden Textabschnitte werden ermittelt. Die Filter
   `hierarchy` und `decision_date_*` wirken direkt in diesem Schritt.
2. Mehrere Abschnitte desselben Entscheids ergeben einen Treffer; massgebend
   ist der beste Abschnitt.
3. `language_filter` und `scrape_date_*` werden danach auf die Entscheide
   angewendet. Sind sie gesetzt, werden 300 statt 100 Abschnitte ausgewertet,
   damit genug Entscheide übrig bleiben.
4. Pro Treffer wird der am besten passende Feinabschnitt (einige Sätze) als
   Auszug in `text` geliefert.

Besonderheiten:

- **Keine Paginierung, keine andere Sortierung.** `total` ist die Zahl der
  Entscheide unter den ausgewerteten Abschnitten, nicht die Zahl aller
  einschlägigen Entscheide.
- **Auszug kann fehlen.** Sind die Feinabschnitte eines Entscheids noch nicht
  indexiert, stösst der Server die Indexierung an und wartet darauf insgesamt
  höchstens 20 Sekunden. Liegt der Auszug dann nicht vor, ist `text` leer; der
  Treffer bleibt gültig. Bei einer späteren Wiederholung der Suche ist der
  Auszug in der Regel vorhanden.
- **Anfrage erforderlich.** Eine leere Anfrage oder `*` ergibt einen Fehler.

#### Parameter

| Name | Typ | Default | Beschreibung |
| --- | --- | --- | --- |
| `query` | `string` | **Pflicht** | Rechtsfrage oder Beschreibung des Sachverhalts, möglichst in ganzen Sätzen. Beliebige Sprache. |
| `language` | `"de"\|"fr"\|"it"` | – | Bevorzugte Sprache für Titel und Abstract. Wirkt **nicht** als Filter. |
| `size` | `int` (1–50) | `10` | Anzahl Entscheide. |
| `decision_date_from` / `decision_date_to` | `date` | – | Entscheiddatum von / bis (inklusive). |
| `scrape_date_from` / `scrape_date_to` | `date` | – | Datum der Aufnahme in den Index von / bis. |
| `hierarchy` | `string[]` | – | Hierarchie-IDs; mehrere gelten als ODER. |
| `language_filter` | `("de"\|"fr"\|"it")[]` | – | Nur Entscheide in diesen Sprachen. |
| `include_context` | `bool` | `false` | Pro Treffer zusätzlich den grösseren Abschnitt um den Auszug liefern (Feld `context`). |

#### Antwort

```jsonc
{
  "search_mode": "semantic",
  "note": "Semantic search: ranked by similarity of meaning, based on the 100 passages that best match the query. …",
  "total": 38,
  "hits": [
    {
      "id": "BS_APG_001_ZB-2014-14_2014-12-22",
      "title": "Basel-Stadt Appellationsgericht 22.12.2014 ZB.2014.14",
      "text": "Auf Vermieterseite fällt vor allem der Eigenbedarf für … die beiden Töchter …",
      "score": 0.8842,
      "context": null,
      "decision_date": "2014-12-22",
      "canton": "BS",
      "court": "BS_APG",
      "url": "https://entscheidsuche.ch/view/BS_APG_001_ZB-2014-14_2014-12-22",
      "document_url": "https://entscheidsuche.ch/docs/…"
      // weitere Felder siehe SearchHit
    }
  ],
  "next_cursor": null,
  "aggregations": null,
  "results": null
}
```

---

### Tool: `keyword_search`

Stichwortsuche im Volltext mit Filtern, Sortierung und Paginierung. Bis
Version 0.1.x hiess dieses Tool `search`.

#### Parameter

| Name | Typ | Default | Beschreibung |
| --- | --- | --- | --- |
| `query` | `string` | `"*"` | Suchbegriffe in Lucene-Syntax. Phrasen mit `"…"`, `AND`/`OR`/`NOT`, Wildcards `*` und `?`. Standardverknüpfung ist `AND`. `*` findet alle Entscheide. |
| `language` | `"de"\|"fr"\|"it"` | – | Bevorzugte Sprache für Highlight, Titel und Abstract. Wirkt **nicht** als Filter. |
| `sort` | `"relevance"\|"date"\|"scrapedate"` | `"relevance"` | Sortierung: Relevanz, Entscheiddatum (neueste zuerst) oder Scrape-Datum (neueste zuerst). |
| `size` | `int` (1–100) | `20` | Treffer pro Seite. |
| `search_after` | `array` | – | Cursor für die nächste Seite — `next_cursor` aus der Antwort der vorigen Seite. |
| `decision_date_from` / `decision_date_to` | `date` (`YYYY-MM-DD`) | – | Entscheiddatum von / bis (inklusive). |
| `scrape_date_from` / `scrape_date_to` | `date` | – | Scrape-Datum von / bis. |
| `hierarchy` | `string[]` | – | Hierarchie-IDs (z.B. `["CH_BGer", "ZH_OG"]`). Mehrere IDs gelten als ODER, mit anderen Filtern als UND. |
| `language_filter` | `("de"\|"fr"\|"it")[]` | – | Filter nach Dokumentsprache(n). |
| `include_aggregations` | `bool` | `false` | Verteilungen der Treffer nach Gericht, Sprache und Datum mitliefern. |

#### Robuste Anfragen

- **Ungültige Syntax:** Ist die Anfrage keine gültige Lucene-Syntax — etwa
  `5A_396/2015` ohne Anführungszeichen, ein Doppelpunkt, eine offene Klammer
  oder ein Operator am Ende —, wird sie als Folge gewöhnlicher Suchbegriffe
  ausgeführt. `note` weist darauf hin.
- **`Art. 271 OR`:** `OR` direkt nach einer Artikelangabe (auch mit `Abs.`,
  `lit.`, `Ziff.`) wird als Obligationenrecht gelesen, nicht als Operator.
  `note` weist darauf hin.
- **Geschäftsnummern** für einen exakten Treffer in Anführungszeichen setzen
  (`"5A_396/2015"`) oder `search_by_case_number` verwenden.

#### Antwort

```jsonc
{
  "search_mode": "keyword",
  "note": null,
  "total": 667,
  "hits": [
    {
      "id": "CH_BGE_004_BGE-99-II-50_1973-02-01",
      "title": "Bundesgericht (BGE) Teil II 01.02.1973 BGE 99 II 50",
      "abstract": "",
      "text": "Berufung auf <em>Eigenbedarf</em> im Sinne von Art. 267c lit. c OR …",
      "meta": "Eidgenossenschaft Bundesgericht (BGE) Teil II",
      "canton": "CH",
      "court": "CH_BGE",
      "decision_date": "1973-02-01",
      "scrape_date": "2026-05-04",
      "is_pdf": false,
      "url": "https://entscheidsuche.ch/view/CH_BGE_004_BGE-99-II-50_1973-02-01",
      "document_url": "https://entscheidsuche.ch/docs/CH_BGE/…html",
      "original_url": null,
      "sort": [27.00332, "CH_BGE_004_BGE-99-II-50_1973-02-01"]
    }
  ],
  "next_cursor": [27.00332, "CH_BGE_004_BGE-99-II-50_1973-02-01"],
  "aggregations": null,
  "results": null
}
```

#### Beispiel — Datumsbereich + Hierarchie

```json
{
  "name": "keyword_search",
  "arguments": {
    "query": "Mietzins",
    "hierarchy": ["ZH_OG"],
    "decision_date_from": "2024-01-01",
    "decision_date_to": "2024-12-31",
    "sort": "date"
  }
}
```

#### Beispiel — Paginierung

```json
{
  "name": "keyword_search",
  "arguments": {
    "query": "Mietzins",
    "sort": "date",
    "search_after": [1718409600000, "CH_BGer_001_5A_123_2024_2024-06-15"]
  }
}
```

Anfrage, Filter und Sortierung müssen auf allen Seiten gleich bleiben.

---

### Tool: `search_by_case_number`

Sucht eine Geschäftsnummer, ein Aktenzeichen oder ein BGE-Zitat als exakte
Phrase. Die Nummer wird automatisch in Anführungszeichen gesetzt (bereits
vorhandene werden entfernt). An erster Stelle steht in der Regel der
Entscheid selbst, danach folgen Entscheide, die ihn zitieren.

#### Parameter

| Name | Typ | Default | Beschreibung |
| --- | --- | --- | --- |
| `case_number` | `string` | **Pflicht** | z.B. `BGE 142 III 1`, `5A_396/2015`, `ZB.2021.48`. |

Alle übrigen Parameter wie bei [`keyword_search`](#tool-keyword_search)
(`language`, `sort`, `size`, `search_after`, Datums-, Hierarchie- und
Sprachfilter, `include_aggregations`).

#### Beispiel

```json
{ "name": "search_by_case_number", "arguments": { "case_number": "BGE 142 III 1" } }
```

---

### Tool: `fetch_document` / `fetch`

Ruft einen einzelnen Entscheid anhand seiner Dokument-ID ab und liefert den
vollständigen Volltext. `fetch` ist ein Alias mit identischem Verhalten;
ChatGPT erwartet für Deep Research ein Tool mit diesem Namen.

#### Parameter

| Name | Typ | Default | Beschreibung |
| --- | --- | --- | --- |
| `id` | `string` | **Pflicht** | Dokument-ID eines Treffers (Feld `id`), z.B. `CH_BGer_005_5A-396-2015_2017-06-22`. Gross-/Kleinschreibung beachten. |
| `language` | `"de"\|"fr"\|"it"` | – | Bevorzugte Sprache für Titel und Abstract. |

#### Antwort

Ein einzelnes [`SearchHit`](#searchhit)-Objekt; `text` enthält den Volltext,
`content_length` dessen Länge. Der indexierte Text ist auf 100'000 Zeichen
begrenzt: Ist `text_truncated` `true`, steht das vollständige Dokument unter
`document_url`.

Gibt es keinen Entscheid mit dieser ID, antwortet das Tool mit einem Fehler
(`isError: true`) und einem Hinweis, statt wie früher mit einer leeren
Antwort.

---

### Tool: `list_hierarchy`

Liefert die Hierarchie-IDs (Kanton / Gericht / Kammer) mit der Zahl der
Entscheide, optional eingeschränkt auf die Treffer einer Stichwort-Anfrage.
Geeignet, um die IDs für den Filter `hierarchy` zu ermitteln oder die
Verteilung der Treffer auf die Gerichte zu sehen.

#### Parameter

| Name | Typ | Default | Beschreibung |
| --- | --- | --- | --- |
| `query` | `string` | `"*"` | Optionale Stichwort-Anfrage (Syntax wie `keyword_search`). |
| `size` | `int` (1–10000) | `1000` | Maximale Anzahl Einträge. |

#### Antwort

```jsonc
{
  "entries": [
    { "id": "CH",      "count": 326904 },
    { "id": "CH_BGer", "count": 179844 },
    { "id": "ZH_OG",   "count":  6789 }
  ]
}
```

---

### Tool: `list_facets`

Liefert den Baum Kanton → Gericht → Kammer mit den Bezeichnungen in Deutsch,
Französisch und Italienisch. Im Gegensatz zu `list_hierarchy` ist die Antwort
statisch (Quelle: `Facetten.json` von entscheidsuche.ch) und enthält keine
Trefferzahlen. Die Antwort ist gross (rund 160'000 Zeichen).

#### Parameter

Keine.

#### Antwort

```jsonc
{
  "facets": [
    {
      "id": "ZH",
      "label": { "de": "Zürich", "fr": "Zurich", "it": "Zurigo", "en": null },
      "children": [
        {
          "id": "ZH_OG",
          "label": { "de": "Obergericht", "fr": "Obergericht", "it": "Obergericht", "en": null },
          "children": [
            { "id": "ZH_OG_001", "label": { "de": "Zivilkammern", "fr": "…", "it": "…", "en": null }, "children": null }
          ]
        }
      ]
    }
  ]
}
```

---

### Tool: `server_info`

Versionsinfo und konfigurierte Endpunkte.

#### Antwort

```jsonc
{
  "name": "entscheidsuche-mcp",
  "version": "0.3.2",
  "search_url": "https://entscheidsuche.ch/_search_KI.php/elasticsearch/_search",
  "semantic_search": true,
  "embedding_model": "qwen3-embedding-8b",
  "facets_url": "https://entscheidsuche.ch/docs/Facetten.json",
  "languages": ["de", "fr", "it"],
  "sort_orders": ["relevance", "date", "scrapedate"],
  "tools": ["search", "semantic_search", "keyword_search", "search_by_case_number", "fetch_document", "fetch", "list_hierarchy", "list_facets", "server_info"]
}
```

---

## Datentypen

### `SearchResponse`

| Feld | Typ | Beschreibung |
| --- | --- | --- |
| `search_mode` | `"keyword" \| "semantic"` | Welche Suche ausgeführt wurde. |
| `note` | `string \| null` | Hinweise zur Ausführung, z.B. dass die Anfrage als gewöhnliche Suchbegriffe gelesen wurde oder warum `search` zur Stichwortsuche gewechselt hat. |
| `total` | `int` | Stichwortsuche: Zahl aller Treffer. KI-Suche: Zahl der Entscheide unter den ausgewerteten Abschnitten. |
| `hits` | `SearchHit[]` | Die Treffer. |
| `next_cursor` | `array \| null` | Nur Stichwortsuche: Cursor für die nächste Seite, als `search_after` übergeben. |
| `aggregations` | `object \| null` | Nur mit `include_aggregations=true`: Name → Liste `{key, count}` (`hierarchy`, `language`, `decision_date`, `scrape_date`). |
| `results` | `{id, title, url}[] \| null` | Nur beim Tool `search`: kompakte Liste derselben Treffer. |

### `SearchHit`

| Feld | Typ | Beschreibung |
| --- | --- | --- |
| `id` | `string` | Dokument-ID. |
| `title` | `string` | Titel (Stichwortsuche: ggf. mit `<em>`-Hervorhebung). |
| `abstract` | `string` | Abstract / Regeste, falls vorhanden. |
| `text` | `string` | Stichwortsuche: Auszug mit Fundstellen in `<em>…</em>`. KI-Suche: am besten passender Abschnitt (kann leer sein). `fetch_document`: Volltext. |
| `meta` | `string` | Gericht und Kammer als Text. |
| `canton` | `string` | Kantonskürzel (z.B. `"ZH"`, `"CH"` für den Bund). |
| `court` | `string` | Gerichtskürzel, abgeleitet aus den ersten zwei Segmenten der ID. |
| `decision_date` | `string \| null` | Entscheiddatum `YYYY-MM-DD`. Bei BGE-Bänden teils nur der Jahresanfang. |
| `scrape_date` | `string \| null` | Datum der Aufnahme in den Index. |
| `is_pdf` | `bool` | `true`, wenn das Originaldokument ein PDF ist. |
| `url` | `string \| null` | Seite des Entscheids auf entscheidsuche.ch — für Zitate und Links. |
| `document_url` | `string \| null` | Direktlink auf das Originaldokument (HTML oder PDF). |
| `original_url` | `string \| null` | URL der Quell-Webseite, falls bekannt. |
| `content_length` | `int \| null` | Nur `fetch_document`: Länge des Volltexts in Zeichen. |
| `text_truncated` | `bool` | Nur `fetch_document`: Volltext bei 100'000 Zeichen abgeschnitten. |
| `sort` | `array \| null` | Nur Stichwortsuche: Sortierwerte des Treffers. |
| `score` | `number \| null` | Nur KI-Suche: Ähnlichkeit zwischen Anfrage und bestem Abschnitt (höher ist besser). |
| `context` | `string \| null` | Nur KI-Suche mit `include_context=true`: grösserer Abschnitt um den Auszug. |

### Datums-Format

Alle Datumsangaben (in Filtern und Antworten) verwenden das Format `YYYY-MM-DD`.

### Lucene-Syntax-Cheatsheet (Stichwortsuche)

| Aufgabe | Beispiel |
| --- | --- |
| Alle Begriffe | `Mietzins Kündigung` |
| Phrase | `"fristlose Kündigung"` |
| Geschäftsnummer | `"BGE 142 III 1"`, `"5A_123/2024"` |
| Gesetzesartikel | `Art. 271 OR` |
| Kombination | `Mietzins AND Erhöhung` |
| OR | `Mietzins OR Pachtzins` |
| Negation | `Mietzins NOT Erhöhung` |
| Wildcard | `Mietz*` |
| Pflicht / Verbot | `+Mietzins -Erhöhung` |
| Feld-Suche | `title.de:"Kündigung"` |

---

## Beispiel-Workflows

### 1. Rechtsfrage recherchieren

```text
Tool: search
Argumente: { "query": "Haftet der Hundehalter für einen Biss, wenn der Hund angeleint war?" }
→ Treffer mit passenden Textabschnitten sichten

Tool: fetch_document
Argumente: { "id": "<ID eines Treffers>" }
→ Volltext zur Auswertung
```

### 2. Geschäftsnummer auflösen

```text
Tool: search_by_case_number
Argumente: { "case_number": "BGE 142 III 1" }
→ erster Treffer ist in der Regel der Entscheid selbst

Tool: fetch_document
Argumente: { "id": "<ID>" }
```

### 3. Alle Mietrechts-Entscheide des Zürcher Obergerichts 2024

```text
Tool: list_hierarchy
Argumente: { "query": "Mietrecht" }
→ Hierarchie-IDs für Zürich identifizieren (z.B. "ZH_OG")

Tool: keyword_search
Argumente: {
  "query": "Mietrecht",
  "hierarchy": ["ZH_OG"],
  "decision_date_from": "2024-01-01",
  "decision_date_to": "2024-12-31",
  "sort": "date",
  "size": 50
}
→ bei Bedarf paginieren über next_cursor / search_after
```

### 4. Verteilung der Treffer pro Gericht

```text
Tool: keyword_search
Argumente: { "query": "Datenschutz", "include_aggregations": true, "size": 1 }
→ aggregations.hierarchy enthält die Verteilung
```

---

## Fehlerbehandlung

Fehler bei der Ausführung eines Tools kommen als normales Tool-Ergebnis mit
`isError: true`; `content[0].text` enthält eine Meldung in Englisch, die
einem Sprachmodell sagt, was zu ändern ist. Beispiele:

| Situation | Meldung (gekürzt) |
| --- | --- |
| Ungültiger Parameter (z.B. `size: 0`, falsches Datum) | `1 validation error … Input should be greater than or equal to 1` |
| `semantic_search` ohne Anfrage | `Semantic search needs a question or a description of the legal issue …` |
| Unbekannte Dokument-ID | `No decision found with id '…'. Document IDs are case-sensitive …` |
| Such-API nicht erreichbar oder Zeitüberschreitung | `The search backend of entscheidsuche.ch could not be reached. Try again later.` |
| Unbekanntes Tool | `Unknown tool: …` |

Die Meldungen enthalten keine internen URLs. Protokollfehler (ungültiges JSON,
unbekannte Methode) werden als JSON-RPC-Fehler gemäß Spec beantwortet.

---

## Änderungen in Version 0.3.2

- **Access-Log:** Lehnt der Server einen Request mit einem HTTP-Fehlerstatus
  ab, steht der Grund im Feld `err=<Code>:<Fehlertext>`.
- **Statistik:** Die Fehlerspalte nennt den Grund im Tooltip. Fehler von
  Prüfprogrammen (Client-Name oder Anfrage-ID mit «probe», «healthcheck»,
  «conformance», «scanner») werden getrennt als «Prüfanfragen» geführt und
  nicht mehr in die Fehlerzahl eingerechnet.
- Tools, Parameter und Antworten sind unverändert.

## Änderungen in Version 0.3.1

- **Hinweis auf den Volltext:** Die Beschreibungen der Such-Tools und die
  Server-Anweisungen sagen ausdrücklich, dass ein Treffer nur einen Auszug
  enthält und der Volltext mit `fetch` zu lesen ist, bevor wiedergegeben wird,
  was ein Entscheid festhält. `search_by_case_number` verlangt zudem den
  Abgleich von Gericht und Datum mit dem vorgegebenen Zitat.
- **Access-Log:** Mit `ESC_ACCESS_LOG_SESSION=true` schreibt der Server
  vorübergehend ein Feld `conv=` mit einer gehashten Kennung der Unterhaltung
  (ChatGPT: `_meta["openai/session"]`), um die Aufrufe einer Unterhaltung
  zusammenzuführen. Standard: aus.
- Tools, Parameter und Antworten sind unverändert.

## Änderungen in Version 0.3.0

- **Protokollversion 2026-07-28:** Der Server beantwortet `server/discover`
  und zustandslose Aufrufe ohne vorheriges `initialize`. Die bisherigen
  Protokollversionen (2024-11-05 bis 2025-11-25) mit `initialize` bleiben
  unverändert nutzbar.
- **MCP-SDK 2.x** (`mcp>=2.3,<3`) statt 1.x.
- **Zwischenspeichern:** `server/discover`, `tools/list`, `resources/list`
  und `prompts/list` tragen im neuen Protokoll `ttlMs: 3600000` und
  `cacheScope: "public"`.
- **Statistik:** Clients des neuen Protokolls werden über `clientInfo` in
  `params._meta` erkannt. Mit gesetztem `ESC_ACCESS_LOG_FILE` wird nicht mehr
  jede Zeile doppelt protokolliert.
- Tools, Parameter und Antworten sind unverändert.

## Änderungen in Version 0.2.0

- **Neuer Such-Endpunkt:** Der Server verwendet die Such-API
  `https://entscheidsuche.ch/_search_KI.php` statt des bisherigen
  Elasticsearch-Servers.
- **Neu `semantic_search`:** KI-Suche nach Bedeutung.
- **`search` umbenannt in `keyword_search`.** Der Name `search` bezeichnet
  jetzt die Standardsuche (KI-Suche mit automatischem Wechsel, siehe oben).
  Aufrufe von `search` mit `sort`, `search_after`, `include_aggregations`,
  `*`, Phrasen, Operatoren, Wildcards oder Geschäftsnummern verhalten sich
  wie bisher.
- **Neu `fetch`:** Alias für `fetch_document`.
- **Neue Antwortfelder:** `search_mode`, `note`, `url`, `score`, `context`,
  `results`.
- **`list_facets`** liefert jetzt auch Gerichte und Kammern (vorher nur die
  Kantone) und ein Objekt `{ "facets": [...] }` statt einer blossen Liste.
- **`fetch_document`** liefert das Objekt direkt (vorher verpackt in
  `{ "result": … }`) und bei unbekannter ID einen Fehler.
- **Ungültige Anfragen** (`Art. 271 OR`, `5A_396/2015`, offene Klammern)
  führen nicht mehr zu einem Fehler.
- **Annotationen:** alle Tools sind als nur lesend markiert.
- Das früher dokumentierte Tool `get_document` gibt es nicht; es wurde bereits
  in einer früheren Version entfernt.
