# entscheidsuche-mcp

**MCP-Server für die offene Schweizer Rechtsprechung** —
KI-Suche nach Bedeutung, Stichwortsuche und Volltextzugriff auf publizierte Gerichtsentscheide
aller Schweizer Instanzen (Bundesgerichte, kantonale Gerichte,
Verwaltungsbehörden, Strafbefehle) in Deutsch, Französisch und
Italienisch — über das [Model Context Protocol](https://modelcontextprotocol.io).

> **MCP Server for Swiss case law.** AI search by meaning, keyword
> search and full-text retrieval of published
> court decisions from all Swiss instances (federal courts, all 26 cantons,
> administrative authorities, penal orders) in German, French, and Italian.
> Operated as open Swiss legal data infrastructure by the non-profit
> association [entscheidsuche.ch](https://entscheidsuche.ch), tax-exempt
> in Switzerland since 2020.

**Öffentlicher Endpunkt:** `https://mcp.entscheidsuche.ch/mcp`
(Streamable HTTP, ohne Authentifizierung)

**Landing Page für Endnutzer:** [mcp.entscheidsuche.ch](https://mcp.entscheidsuche.ch) —
dort steht alles Nötige zur Einrichtung in Claude.ai, ChatGPT, Cursor,
VS Code, Claude Desktop und anderen Clients. Dieses Repository richtet
sich primär an Entwickler, Drittanwendungen und Selbst-Betreiber.

[![Lizenz Code](https://img.shields.io/badge/code-MIT-blue.svg)](#lizenz)
[![Daten](https://img.shields.io/badge/data-open-green.svg)](https://entscheidsuche.ch)
[![Betrieb](https://img.shields.io/badge/operator-Verein%20entscheidsuche.ch-orange.svg)](https://entscheidsuche.ch)

---

## Inhalt

- [Über den MCP-Server](#über-den-mcp-server)
- [Tools](#tools)
- [Zwei Sucharten](#zwei-sucharten)
- [Suchsyntax der Stichwortsuche](#suchsyntax-der-stichwortsuche--cheatsheet)
- [Testoberfläche](#testoberfläche-test)
- [Schnelleinbindung in MCP-Clients](#schnelleinbindung-in-mcp-clients)
- [Lokale Entwicklung](#lokale-entwicklung)
- [Eigene Instanz deployen](#eigene-instanz-deployen-debian)
- [Architektur](#architektur)
- [Trägerverein](#trägerverein)
- [Beitragen](#beitragen)
- [Lizenz](#lizenz)

## Über den MCP-Server

Schweizer Gerichtsentscheide sind formal öffentlich, faktisch aber über
mehr als 26 kantonale Portale, mehrere Bundesgerichte und unterschiedlich
strukturierte Veröffentlichungsformate verteilt. Der **Verein
entscheidsuche.ch** sammelt diese Entscheide seit 2017 ein, indexiert
sie volltextlich und macht sie zugänglich. Eine Lizenz gibt es nicht, da
Gerichtsentscheide nicht dem Urheberrecht unterstehen. Dieser
MCP-Server bringt diesen Bestand zu KI-Assistenten und programmatischen
Clients.

**Was den Server unterscheidet**

- Betrieben vom **gemeinnützigen Verein entscheidsuche.ch** (gegründet
  2017 in Landquart GR, seit 2020 in Bern als gemeinnütziger Zweck
  steuerbefreit) — kollektiv getragene Schweizer Rechtsdaten-Infrastruktur,
  kein Bastelprojekt
- **Eigener Datenbestand**: tägliche Scrapes direkt aus den
  Originalquellen aller Schweizer Gerichte. Enthält auch Entscheide,
  die von Behörden nicht oder nicht in geeigneter Form publiziert
  werden und über die Upload-Funktion (seit 2021) eingereicht wurden,
  darunter Strafbefehle
- **Drei Amtssprachen** (DE / FR / IT) mit gezieltem Sprachfilter und
  sprachübergreifender Suche
- **Offen** — keine Authentifizierung, kein API-Key, lizenzfrei

## Tools

| Tool | Zweck |
| --- | --- |
| `search` | **Standardsuche.** Frage oder Sachverhalt in natürlicher Sprache → KI-Suche nach Bedeutung. Wechselt automatisch zur Stichwortsuche, wenn die Anfrage eine Geschäftsnummer, eine Phrase in Anführungszeichen, Boolesche Operatoren oder Wildcards enthält (Regeln in [docs/API.md](docs/API.md#tool-search)). |
| `semantic_search` | KI-Suche nach Bedeutung (Embeddings), sprachübergreifend. Liefert pro Entscheid den am besten passenden Textabschnitt. Filter nach Hierarchie, Entscheiddatum, Scrape-Datum und Sprache. |
| `keyword_search` | Stichwortsuche mit Lucene-Syntax. Dieselben Filter, dazu Sortierung nach Relevanz, Entscheiddatum oder Scrape-Datum, Paginierung über `next_cursor` / `search_after` und Verteilungen (Aggregationen). Auszüge mit hervorgehobenen Fundstellen. |
| `search_by_case_number` | Exakte Phrasensuche nach Geschäftsnummern, Aktenzeichen und BGE-Zitaten (z.B. `BGE 142 III 1`, `6B_1234/2025`, `5A_396/2015`). Setzt die Nummer automatisch in Anführungszeichen. |
| `fetch_document` | Vollständigen Entscheid samt Volltext anhand der ID abrufen. |
| `fetch` | Alias für `fetch_document` (ChatGPT erwartet für Deep Research die Namen `search` und `fetch`). |
| `list_hierarchy` | Hierarchie-IDs (Bund / Kanton / Gericht / Kammer) mit Trefferzahlen. |
| `list_facets` | Baum Kanton → Gericht → Kammer mit Bezeichnungen in DE/FR/IT. |
| `server_info` | Versions- und Konfigurationsinformationen. |

Alle Tools sind als nur lesend annotiert (`readOnlyHint`).

Vollständige Schnittstellenbeschreibung mit allen Parametern, Filtern,
Rückgabe-Schemata und Beispielen: [docs/API.md](docs/API.md).

## Zwei Sucharten

| | KI-Suche (`semantic_search`) | Stichwortsuche (`keyword_search`) |
| --- | --- | --- |
| Sucht nach | Bedeutung | Wörtern |
| Geeignete Anfrage | Rechtsfrage oder Sachverhalt in ganzen Sätzen | Begriffe, Phrasen, Artikel, Namen |
| Sprache | sprachübergreifend (deutsche Frage findet auch französische Entscheide) | findet nur die eingegebenen Wörter |
| Reihenfolge | inhaltliche Nähe (`score`) | Relevanz, Entscheiddatum oder Scrape-Datum |
| Umfang | Entscheide unter den 100 am besten passenden Textabschnitten, keine Paginierung | alle Treffer, Paginierung |
| Auszug (`text`) | am besten passender Abschnitt | Fundstellen in `<em>…</em>` |

Die KI-Suche verwendet dieselbe Infrastruktur wie die Schaltfläche
«KI-Suche» auf [entscheidsuche.ch](https://entscheidsuche.ch): Die Anfrage
wird in einen Embedding-Vektor umgerechnet, die inhaltlich nächsten
Textabschnitte werden ermittelt und den Entscheiden zugeordnet. Fehlt für
einen Treffer der Auszug, weil seine Feinabschnitte noch nicht indexiert
sind, stösst der Server die Indexierung an und wartet darauf höchstens
20 Sekunden (`ENTSCHEIDSUCHE_MICRO_CHUNK_WAIT`); danach kommt der Treffer
ohne Auszug.

## Suchsyntax der Stichwortsuche — Cheatsheet

| Aufgabe | Beispiel |
| --- | --- |
| Volltext, alle Begriffe (AND) | `Mietzins Kündigung` |
| Phrase, exakte Reihenfolge | `"fristlose Kündigung"` |
| Geschäftsnummer | `"BGE 142 III 1"` |
| Gesetzesartikel | `Art. 271 OR` |
| OR | `Mietzins OR Pachtzins` |
| Negation | `Mietzins NOT Erhöhung` |
| Wildcard | `Mietz*` |
| Feld-Suche | `title.de:"Kündigung"` |

Standardverknüpfung zwischen Wörtern ist `AND`. Gesucht wird in
`title`, `abstract`, `meta`, `attachment.content` und `reference`.

Ist eine Anfrage keine gültige Lucene-Syntax (z.B. `5A_396/2015` ohne
Anführungszeichen oder eine offene Klammer), wird sie als Folge
gewöhnlicher Suchbegriffe ausgeführt; die Antwort weist im Feld `note`
darauf hin. `OR` direkt nach einer Artikelangabe gilt als
Obligationenrecht, nicht als Operator.

`language` und `sort` sind optional. Ohne `language` erfolgt **keine**
Spracheinschränkung — der Server liefert das erste vorhandene
Sprachfeld zurück (de → fr → it). Ohne `sort` wird nach Relevanz
sortiert. Zur expliziten Sprachfilterung dient `language_filter`.
Erlaubte Sprachen: `de`, `fr`, `it`.

## Testoberfläche (`/test`)

Unter [mcp.entscheidsuche.ch/test](https://mcp.entscheidsuche.ch/test)
steht eine Testoberfläche im Stil von Swagger UI. Sie lädt die Tools per
`tools/list` vom Server, baut aus dem Eingabeschema ein Formular und
zeigt zu jedem Aufruf den JSON-RPC-Request, den passenden `curl`-Befehl,
das aufbereitete Ergebnis und die Rohantwort. Lokal ist sie unter
`http://127.0.0.1:8765/test` erreichbar.

## Schnelleinbindung in MCP-Clients

Ausführliche, client-spezifische Anleitungen mit Voraussetzungen
(Tarif-Anforderungen für claude.ai und ChatGPT, Konfigurations-Pfade)
stehen auf der [Landing Page](https://mcp.entscheidsuche.ch) und in
[docs/CLIENTS.md](docs/CLIENTS.md). Im Wesentlichen:

**Claude Code (CLI)**

```bash
claude mcp add --transport http entscheidsuche https://mcp.entscheidsuche.ch/mcp
```

**Cursor / Windsurf / VS Code / Generisch (`mcp.json`)**

```json
{
  "mcpServers": {
    "entscheidsuche": {
      "url": "https://mcp.entscheidsuche.ch/mcp"
    }
  }
}
```

**Claude Desktop** (stdio-only, daher über die `mcp-remote`-Bridge):

```json
{
  "mcpServers": {
    "entscheidsuche": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.entscheidsuche.ch/mcp"]
    }
  }
}
```

**MCP Inspector zum Testen**

```bash
npx @modelcontextprotocol/inspector https://mcp.entscheidsuche.ch/mcp
```

> Hinweis: Nicht jede Client-Anleitung ist vollständig durchgespielt;
> Tarife, Tool-Listen und Konfigurations-Pfade ändern sich bei Anthropic,
> OpenAI und anderen Anbietern regelmässig. Bei Abweichungen gilt die
> offizielle Doku des jeweiligen Clients — Rückmeldungen bitte als
> Issue oder PR.

## Lokale Entwicklung

```bash
git clone https://github.com/entscheidsuche/entscheidsuche-mcp.git
cd entscheidsuche-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# Server starten — Streamable HTTP auf 127.0.0.1:8765/mcp
python -m entscheidsuche_mcp

# Alternative: stdio (für lokale CLI-Clients)
python -m entscheidsuche_mcp --transport stdio
```

### Konfiguration (Umgebungsvariablen)

Vollständige Liste in [`.env.example`](.env.example). Die wichtigsten:

| Variable | Default | Bedeutung |
| --- | --- | --- |
| `ENTSCHEIDSUCHE_API_BASE` | `https://entscheidsuche.ch/_search_KI.php` | Such-API von entscheidsuche.ch; darunter liegen alle Endpunkte der Stichwort- und der KI-Suche |
| `ENTSCHEIDSUCHE_ES_URL` | `<API_BASE>/elasticsearch/_search` | Such-Endpunkt der Entscheide (überschreibt den Default einzeln) |
| `ENTSCHEIDSUCHE_SEMANTIC_SEARCH` | `true` | KI-Suche ein/aus; bei `false` führt `search` die Stichwortsuche aus |
| `ENTSCHEIDSUCHE_MICRO_CHUNK_WAIT` | `20` | Sekunden, die die KI-Suche höchstens auf nachzuindexierende Auszüge wartet |
| `ENTSCHEIDSUCHE_FACETS_URL` | `https://entscheidsuche.ch/docs/Facetten.json` | Facetten-Hierarchie |
| `HOST` / `PORT` | `127.0.0.1` / `8765` | Listen-Adresse |
| `MCP_PATH` | `/mcp` | HTTP-Pfad |
| `MCP_STATELESS_HTTP` | `true` | Streamable HTTP ohne Session-Pflicht |
| `MCP_DNS_REBINDING_PROTECTION` | `true` | Host-/Origin-Prüfung |
| `PUBLIC_BASE_URL` | `https://mcp.entscheidsuche.ch` | Öffentliche Basis-URL |
| `LOG_LEVEL` | `INFO` | Loglevel |

### Schnelltest mit `curl`

```bash
# initialize
curl -N -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     http://localhost:8765/mcp \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'

# tools/list
curl -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
     http://localhost:8765/mcp \
     -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'

# KI-Suche
curl -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
     http://localhost:8765/mcp \
     -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"semantic_search","arguments":{"query":"Darf der Vermieter wegen Eigenbedarf kündigen, wenn seine Tochter einziehen will?","size":3}}}'

# Stichwortsuche
curl -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
     http://localhost:8765/mcp \
     -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"keyword_search","arguments":{"query":"\"BGE 142 III 1\"","size":3}}}'
```

Bequemer geht es über die [Testoberfläche](#testoberfläche-test) unter
`http://localhost:8765/test`.

Komfortabler ist ein echter MCP-Client (MCP Inspector, Claude Code) —
der übernimmt die Streamable-HTTP-Session-Verwaltung automatisch.

## Eigene Instanz deployen (Debian)

Das Repository enthält eine systemd-Unit, einen nginx-vHost und ein
Installations-Script. Damit kann jeder einen eigenen MCP-Endpoint
gegen die Such-API von entscheidsuche.ch betreiben.

```bash
ssh root@your-server.example
git clone https://github.com/entscheidsuche/entscheidsuche-mcp.git /opt/entscheidsuche-mcp
sudo bash /opt/entscheidsuche-mcp/deploy/install.sh

# TLS-Zertifikat
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d mcp.example.org
```

Das Script legt den Systemnutzer `entscheidsuche` an, baut ein venv,
kopiert `.env.example` nach `/etc/entscheidsuche-mcp.env`, installiert
die systemd-Unit und den nginx-vHost.

Status prüfen:

```bash
systemctl status entscheidsuche-mcp
journalctl -u entscheidsuche-mcp -f
```

### Bestehende Instanz auf Version 0.2.0 aktualisieren

```bash
cd /opt/entscheidsuche-mcp && git pull --ff-only
.venv/bin/pip install --no-deps .          # entfällt bei editable-Installation (pip install -e)

# Alte Upstream-URLs aus der Konfiguration entfernen, sonst bleiben sie wirksam:
grep -nE 'ENTSCHEIDSUCHE_(ES|FACETS)_URL' /etc/entscheidsuche-mcp.env

sudo install -m 644 deploy/entscheidsuche-mcp.service /etc/systemd/system/
sudo install -m 644 deploy/nginx.conf /etc/nginx/sites-available/mcp.entscheidsuche.ch
sudo systemctl daemon-reload && sudo nginx -t && sudo systemctl reload nginx
sudo systemctl restart entscheidsuche-mcp
```

Die systemd-Unit begrenzt den Dienst auf 4 GB Arbeitsspeicher
(`MemoryMax=4G`, Drosselung ab `MemoryHigh=3G`).

## Nutzungsstatistik (`/statistik`)

Der MCP-Server liefert unter `/statistik` eine selbst-enthaltene
HTML-Seite mit Tageszahlen, Top-Tools, KI-Client-Klassifizierung
(`clientInfo.name`), Stunden-Sparklines und Methoden-Verteilung.
Wird **bei jedem Aufruf live** generiert; Vortage werden in einem
JSON-Cache (`/var/lib/entscheidsuche-mcp/stats-cache.json`) fixiert.

Basic Auth via Env-Variablen (in `/etc/entscheidsuche-mcp.env`):

```bash
ESC_STATS_USER=admin
ESC_STATS_PASS=DEIN_PASSWORT
```

Sind beide leer, ist `/statistik` ungeschützt zugänglich (Dev-Modus).
Der Authorization-Header wird vom nginx-vHost unverändert an den
Python-Server durchgereicht; die Prüfung passiert dort per
`hmac.compare_digest` gegen die Env-Variablen.

Inhalte sind reine Aggregate. Mit `ESC_ACCESS_LOG_ARGS_MAX=0` (Default
im `.env.example`) werden Tool-Argumente — also die Suchqueries der
Nutzer — gar nicht erst geloggt.

Datenquelle: `/var/log/entscheidsuche-mcp/access.log` (geschrieben von
der `entscheidsuche_mcp.access`-Middleware). Logrotation z. B. via
`/etc/logrotate.d/entscheidsuche-mcp` einrichten:

```
/var/log/entscheidsuche-mcp/access.log {
    weekly
    rotate 12
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
```

`copytruncate` ist wichtig, damit der Logger weiter in die rotierte
Datei schreibt, ohne dass der Server neu gestartet werden muss.

## Architektur

```
MCP-Client (Claude, ChatGPT, Cursor, ...)
       │  Streamable HTTP (JSON-RPC)
       ▼
mcp.entscheidsuche.ch
       │  TLS, nginx Reverse Proxy
       ▼
127.0.0.1:8765/mcp  ← uvicorn + FastMCP
       │  HTTPS
       ▼
entscheidsuche.ch/_search_KI.php  ← Such-API (Proxy)
       ├─ Elasticsearch: Entscheide (entscheidsuche.v2-*)
       ├─ Elasticsearch: Textabschnitte mit Embeddings (grosse Chunks, Micro-Chunks)
       └─ Embedding-Modelle (qwen3-embedding-8b / -0.6b)
```

Implementierung: Python + [FastMCP](https://github.com/jlowin/fastmcp).

## Trägerverein

Der MCP-Server, die zugrundeliegende Plattform und der Datenbestand
werden vom **Verein entscheidsuche.ch** betrieben.

- **Gegründet:** 2017 in Landquart (GR)
- **Vereinszweck:** Die Rechtsprechung schweizerischer Gerichte für
  jedermann durchsuchbar und zugreifbar machen
  ([Statuten](https://entscheidsuche.ch))
- **Gemeinnützigkeit:** Seit 2020 vom Kanton Bern wegen Verfolgung
  gemeinnütziger Zwecke von der Steuerpflicht befreit
- **Vorstand:** Jörn Erbguth, Daniel Kettiger, Claudia Schreiber
- **Kontakt:** `info@entscheidsuche.ch`
- **Postadresse:** Verein entscheidsuche.ch, 3000 Bern
- **Spendenkonto:** Postfinance IBAN `CH04 0900 0000 1412 0685 4`,
  Verein entscheidsuche.ch, 8000 Zürich (Spendenquittung auf Anfrage)
- **Mitgliedschaft:** 100 CHF/Jahr für natürliche Personen,
  1'000 CHF/Jahr für institutionelle Mitglieder

Die Plattform wurde 2018–2020 durch eine
[Crowdfunding-Aktion auf wemakeit](https://wemakeit.com/projects/entscheidsuche-ch)
finanziert. Eine kurze Vorstellung des Projekts erschien im Juli 2021
in der Zeitschrift für Zivilprozess- und Zwangsvollstreckungsrecht (ZZZ).

Die Scraper, mit denen die Entscheide aus den Originalquellen geholt
werden, sind ebenfalls open source — siehe Repositories des Vereins
auf GitHub.

## Beitragen

Issues, Pull Requests und Vorschläge sind willkommen, insbesondere:

- Fehlerberichte zur Suche oder zur API
- Erweiterungen der MCP-Tools (z.B. Aggregations-/Statistik-Tools,
  Zitationsgraph)
- Verbesserungen der Client-Anleitungen in [docs/CLIENTS.md](docs/CLIENTS.md)
- Übersetzungen

Bei **Datenfehlern** oder fehlenden Entscheiden bitte direkt an
`info@entscheidsuche.ch` — der Datenbestand wird zentral gepflegt,
nicht in diesem Repo.

## Lizenz

**Code:** [MIT](LICENSE)

**Daten:** Die zugrundeliegenden Gerichtsentscheide sind amtliche Werke
und stehen gemäss URG Art. 5 nicht unter Urheberrechtsschutz. Die
strukturierten Aufbereitungen des Vereins entscheidsuche.ch sind
frei nutzbar — Details unter [entscheidsuche.ch](https://entscheidsuche.ch).
