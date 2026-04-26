# Schnittstellenbeschreibung — entscheidsuche-mcp

Dieser Server stellt die Suchfunktionen von [entscheidsuche.ch](https://entscheidsuche.ch)
über das **Model Context Protocol** (MCP) bereit. Transport: **Streamable HTTP**
(JSON-RPC 2.0 über `POST` mit optionalem SSE-Streaming für Server-zu-Client-
Nachrichten).

Endpunkt in Produktion:

```
https://mcp.entscheidsuche.ch/mcp
```

Der Pfad ist konfigurierbar (`MCP_PATH`, Default `/mcp`).

## Inhalt

- [Protokoll-Grundlagen](#protokoll-grundlagen)
- [Tools](#tools)
  - [`search`](#tool-search)
  - [`get_document`](#tool-get_document)
  - [`list_hierarchy`](#tool-list_hierarchy)
  - [`list_facets`](#tool-list_facets)
  - [`server_info`](#tool-server_info)
- [Datentypen](#datentypen)
- [Beispiel-Workflows](#beispiel-workflows)
- [Fehlerbehandlung](#fehlerbehandlung)

---

## Protokoll-Grundlagen

Streamable-HTTP-Transport gemäß
[MCP-Spec 2025-06-18](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports#streamable-http):

- **POST `/mcp`** — JSON-RPC-Request, Antwort entweder als `application/json` oder
  als `text/event-stream` (Server-Sent Events).
- **GET `/mcp`** — Server-zu-Client-Push (optional).
- **DELETE `/mcp`** — Session beenden.
- Diese Instanz läuft standardmäßig im **stateless HTTP**-Modus. Ein
  `Mcp-Session-Id`-Header ist daher normalerweise nicht erforderlich.
  Zustandsbehaftete Clients können `initialize` trotzdem wie gewohnt senden.
- Header `Accept: application/json, text/event-stream` ist Pflicht.

**Tipp:** Verwende einen MCP-Client (Claude, Inspector, SDKs); der nimmt dir die
Session-Verwaltung ab. Direkter `curl`-Zugriff funktioniert, ist aber umständlich.

---

## Tools

### Tool: `search`

Volltext-Suche in den Entscheiden mit Filtern, Sortierung und Paginierung.

#### Parameter

| Name | Typ | Default | Beschreibung |
| --- | --- | --- | --- |
| `query` | `string` | `"*"` | Volltext-Anfrage in Lucene-Syntax. Phrasen mit `"…"`, `AND`/`OR`/`NOT`, Wildcards `*` und `?`. Default-Operator zwischen Wörtern ist `AND`. `*` matcht alles. |
| `language` | `"de"\|"fr"\|"it"` | `"de"` | Optionale Sprache für Highlight, Titel- und Abstract-Auswahl. Wirkt **nicht** als Filter. |
| `sort` | `"relevance"\|"date"\|"scrapedate"` | `"relevance"` | Optionale Sortierreihenfolge. Ohne Angabe wird nach Relevanz sortiert. |
| `size` | `int` (1–100) | `20` | Anzahl Treffer pro Seite. |
| `search_after` | `array` | – | Cursor für die nächste Seite — `next_cursor` aus der Antwort der vorigen Seite. |
| `decision_date_from` | `date` (`YYYY-MM-DD`) | – | Untergrenze Entscheiddatum (inklusive). |
| `decision_date_to` | `date` | – | Obergrenze Entscheiddatum (inklusive). |
| `scrape_date_from` | `date` | – | Untergrenze Scrape-Datum. |
| `scrape_date_to` | `date` | – | Obergrenze Scrape-Datum. |
| `hierarchy` | `string[]` | – | Liste von Hierarchie-IDs (z.B. `["CH_BGer", "ZH_OG"]`). Mehrere IDs werden mit OR verknüpft, mit anderen Filtern AND. |
| `language_filter` | `("de"\|"fr"\|"it")[]` | – | Filter nach Dokumentsprache(n). |
| `include_aggregations` | `bool` | `false` | Aggregations-Buckets über die Treffermenge mitliefern. |

> **Geschäftsnummer-Suche:** Die Geschäftsnummer in Anführungszeichen setzen,
> z.B. `query="\"5A_123/2024\""` oder `"BGE 142 III 1"`. Damit wird sie als
> exakte Phrase gesucht.

#### Antwort

```jsonc
{
  "total": 1234,
  "hits": [
    {
      "id": "CH_BGer_001_5A_123_2024_2024-06-15",
      "title": "...",
      "abstract": "...",
      "text": "Highlight-Auszug aus dem Volltext",
      "meta": "...",
      "canton": "CH",
      "court": "CH_BGer",
      "decision_date": "2024-06-15",
      "scrape_date": "2024-09-12",
      "is_pdf": true,
      "document_url": "https://...pdf",
      "original_url": "https://www.bger.ch/...",
      "sort": [1718409600000, "CH_BGer_001_5A_123_2024_2024-06-15"]
    }
  ],
  "next_cursor": [1718409600000, "CH_BGer_001_5A_123_2024_2024-06-15"],
  "aggregations": null
}
```

Felder:

| Feld | Beschreibung |
| --- | --- |
| `total` | Gesamtanzahl der Treffer (über alle Seiten). |
| `hits` | Liste der Treffer (siehe [`SearchHit`](#searchhit)). |
| `next_cursor` | Cursor für die nächste Seite. `null` wenn keine weiteren Treffer vorhanden sind. Im nächsten Aufruf als `search_after` übergeben. |
| `aggregations` | Nur gesetzt wenn `include_aggregations=true`. Map von Aggregations-Name → Liste `{key, count}`. |

#### Beispiel — Geschäftsnummer

```json
{
  "name": "search",
  "arguments": {
    "query": "\"5A_123/2024\"",
    "language": "de"
  }
}
```

#### Beispiel — Datumsbereich + Hierarchie

```json
{
  "name": "search",
  "arguments": {
    "query": "Mietzins",
    "decision_date_from": "2023-01-01",
    "decision_date_to": "2023-12-31",
    "hierarchy": ["ZH_OG"],
    "sort": "date",
    "size": 50
  }
}
```

#### Beispiel — Paginierung

```json
// Aufruf 1
{ "name": "search", "arguments": { "query": "Mietzins", "size": 20 } }
// Antwort liefert "next_cursor": [...]

// Aufruf 2 — Folge-Seite
{ "name": "search", "arguments": { "query": "Mietzins", "size": 20, "search_after": [...] } }
```

---

### Tool: `get_document`

Ruft einen einzelnen Entscheid anhand seiner Dokument-ID ab.

#### Parameter

| Name | Typ | Default | Beschreibung |
| --- | --- | --- | --- |
| `id` | `string` | – | Dokument-ID (entspricht `_id` im Index). |
| `language` | `"de"\|"fr"\|"it"` | `"de"` | Optionale Sprache für Anzeige. |
| `include_content` | `bool` | `false` | Wenn `true`: kompletter Volltext (`attachment.content`) wird mitgeliefert. **Achtung:** kann sehr groß werden. |

#### Antwort

Ein einzelnes [`SearchHit`](#searchhit)-Objekt oder `null`, wenn die ID
nicht gefunden wurde.

---

### Tool: `list_hierarchy`

Liefert alle Hierarchie-Buckets (Kanton/Gericht/Kammer) mit der jeweiligen
Trefferzahl. Geeignet, um die für `search.hierarchy` verfügbaren IDs zu
ermitteln, oder um die Verteilung der Treffer auf Gerichte zu sehen.

#### Parameter

| Name | Typ | Default | Beschreibung |
| --- | --- | --- | --- |
| `query` | `string` | `"*"` | Optionale Volltext-Anfrage. |
| `size` | `int` (1–10000) | `1000` | Maximale Anzahl Einträge. |

#### Antwort

```jsonc
{
  "entries": [
    { "id": "CH_BGer", "count": 12345 },
    { "id": "ZH_OG",   "count":  6789 },
    ...
  ]
}
```

---

### Tool: `list_facets`

Liefert den hierarchischen Facetten-Baum mit lokalisierten Bezeichnungen.
Im Gegensatz zu `list_hierarchy` ist die Antwort **statisch** (basiert auf
`Facetten.json`) und enthält Labels in vier Sprachen.

#### Parameter

Keine.

#### Antwort

```jsonc
[
  {
    "id": "CH",
    "label": { "de": "Bund", "fr": "Confédération", "it": "...", "en": "..." },
    "children": [
      {
        "id": "CH_BGer",
        "label": { "de": "Bundesgericht", ... },
        "children": [ ... ]
      }
    ]
  },
  {
    "id": "ZH",
    "label": { "de": "Zürich", ... },
    "children": [ ... ]
  }
]
```

---

### Tool: `server_info`

Versionsinfo und konfigurierte Endpunkt-URLs.

#### Antwort

```jsonc
{
  "name": "entscheidsuche-mcp",
  "version": "0.1.0",
  "elasticsearch_url": "https://entscheidsuche.pansoft.de:9200/entscheidsuche.v2-*/_search",
  "facets_url": "https://www.recherche.histoirerurale.ch/Facetten.json",
  "languages": ["de", "fr", "it"],
  "sort_orders": ["relevance", "date", "scrapedate"]
}
```

---

## Datentypen

### `SearchHit`

| Feld | Typ | Beschreibung |
| --- | --- | --- |
| `id` | `string` | Dokument-ID. |
| `title` | `string` | Titel (in der angeforderten Sprache, ggf. mit Highlight). |
| `abstract` | `string` | Abstract / Regeste (mit Highlight, falls vorhanden). |
| `text` | `string` | Highlight-Auszug aus dem Volltext (`attachment.content`). Bei `get_document(include_content=true)` der komplette Volltext. |
| `meta` | `string` | Meta-Information (Index-Feld `meta`). |
| `canton` | `string` | Kanton-Kürzel (z.B. `"ZH"`, `"CH"`). |
| `court` | `string` | Gericht-Kürzel, abgeleitet aus den ersten zwei Segmenten der ID. |
| `decision_date` | `string \| null` | ISO-Datum `YYYY-MM-DD`. |
| `scrape_date` | `string \| null` | ISO-Datum `YYYY-MM-DD`. |
| `is_pdf` | `bool` | `true`, wenn das Originaldokument ein PDF ist. |
| `document_url` | `string \| null` | Direkt-Link auf das Originaldokument. |
| `original_url` | `string \| null` | URL der Quell-Webseite. |
| `sort` | `array \| null` | Sort-Werte (Cursor-Bestandteil). |

### Datums-Format

Alle Datumsangaben (in Filtern und Antworten) verwenden ISO-8601-Datums-Strings
im Format `YYYY-MM-DD`.

### Lucene-Syntax-Cheatsheet

| Aufgabe | Beispiel |
| --- | --- |
| Phrase | `"fristlose Kündigung"` |
| Geschäftsnummer | `"BGE 142 III 1"`, `"5A_123/2024"` |
| Kombination | `"Mietzins" AND "Erhöhung"` |
| OR | `Mietzins OR Pachtzins` |
| Negation | `Mietzins NOT Erhöhung` |
| Wildcard | `Mietz*` |
| Pflicht / Verbot | `+Mietzins -Erhöhung` |
| Feld-Suche | `title.de:"Kündigung"` |

---

## Beispiel-Workflows

### 1. Geschäftsnummer auflösen

```text
Tool: search
Argumente: { "query": "\"BGE 142 III 1\"" }
→ Treffer prüfen → ID notieren

Tool: get_document
Argumente: { "id": "<ID>", "include_content": true }
→ Volltext zur Auswertung
```

### 2. Alle Mietrechts-Entscheide des Zürcher Obergerichts 2024

```text
Tool: list_hierarchy
Argumente: { "query": "Mietrecht" }
→ Hierarchie-IDs für Zürich identifizieren (z.B. "ZH_OG")

Tool: search
Argumente: {
  "query": "Mietrecht",
  "hierarchy": ["ZH_OG"],
  "decision_date_from": "2024-01-01",
  "decision_date_to": "2024-12-31",
  "sort": "date",
  "size": 50
}
→ Bei Bedarf paginieren über next_cursor / search_after
```

### 3. Verteilung der Treffer pro Gericht

```text
Tool: search
Argumente: {
  "query": "Datenschutz",
  "include_aggregations": true,
  "size": 0  → ungültig (min=1), stattdessen size=1 setzen
}
→ aggregations.hierarchy enthält die Verteilung
```

---

## Fehlerbehandlung

JSON-RPC-Fehler werden gemäß Spec zurückgegeben:

| Code | Bedeutung |
| --- | --- |
| `-32602` | Ungültige Parameter (Schema-Validierung fehlgeschlagen). |
| `-32603` | Interner Server-Fehler (z.B. Elasticsearch nicht erreichbar). |
| `-32000` | Generische Tool-Fehler. |

Bei HTTP-Problemen mit dem Backend (Timeout, 5xx) gibt der Server einen
JSON-RPC-Fehler mit beschreibender `message` zurück.
