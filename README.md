# entscheidsuche-mcp

MCP-Server für [entscheidsuche.ch](https://entscheidsuche.ch) — die Volltext-Suchmaschine
für Schweizer Gerichtsentscheide, Gerichtsurteile und Rechtsprechung.

Stellt die Such-API als [Model Context Protocol](https://modelcontextprotocol.io)-Tools
über **Streamable HTTP** bereit, sodass Claude und andere MCP-Clients direkt gegen
den Index suchen können.

## Funktionen

- Volltextsuche mit Lucene-Query-Syntax (Phrasen mit `"…"`, `AND`/`OR`/`NOT`,
  Wildcards `*` und `?`)
- **Geschäftsnummer-Suche** über Phrasen in Anführungszeichen, z.B. `"BGE 142 III 1"`
- **Spezialtool für Geschäftsnummern** und BGE-Zitate, das automatisch als Phrase sucht
- Filter nach **Entscheiddatum**, **Scrape-Datum**, **Hierarchie**
  (Kanton/Gericht/Kammer) und **Sprache**
- Sortierung nach Relevanz, Entscheiddatum oder Scrape-Datum
- Paginierung über `next_cursor` / `search_after`
- Aggregationen (Buckets) für Facetten-Auswertung
- Lokalisierter Facetten-Baum (de/fr/it/en)

## Architektur

```
MCP-Client (Claude, etc.)
       │  Streamable HTTP (JSON-RPC)
       ▼
mcp.entscheidsuche.ch
       │  TLS, nginx Reverse Proxy
       ▼
127.0.0.1:8765/mcp  ← uvicorn + FastMCP
       │  HTTPS
       ▼
entscheidsuche.pansoft.de:9200/entscheidsuche.v2-*/_search
```

Tools:

| Tool | Zweck |
| --- | --- |
| `search` | Volltext-Suche in Schweizer Rechtsprechung mit Filtern, Sortierung, Paginierung |
| `search_by_business_number` | Exakte Suche nach Geschäftsnummern, Urteilsnummern und BGE-Zitaten |
| `get_document` | Einzelnes Dokument anhand der ID, optional mit Volltext |
| `list_hierarchy` | Hierarchie-IDs mit Trefferzahlen |
| `list_facets` | Hierarchischer Facetten-Baum mit lokalisierten Labels |
| `server_info` | Versions- und Konfigurations-Info |

Vollständige Schnittstellenbeschreibung: [docs/API.md](docs/API.md).

## Lokal entwickeln

```bash
cd entscheidsuche-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# Server starten — Streamable HTTP auf 127.0.0.1:8765/mcp
python -m entscheidsuche_mcp

# Alternative: stdio (für lokale CLI-Clients)
python -m entscheidsuche_mcp --transport stdio
```

Konfiguration über Umgebungsvariablen (siehe `.env.example`):

| Variable | Default | Bedeutung |
| --- | --- | --- |
| `ENTSCHEIDSUCHE_ES_URL` | `https://entscheidsuche.pansoft.de:9200/entscheidsuche.v2-*/_search` | Elasticsearch-Endpoint |
| `ENTSCHEIDSUCHE_FACETS_URL` | `https://www.recherche.histoirerurale.ch/Facetten.json` | Facetten-Hierarchie-JSON |
| `HOST` | `127.0.0.1` | Listen-Host |
| `PORT` | `8765` | Listen-Port |
| `MCP_PATH` | `/mcp` | HTTP-Pfad-Präfix |
| `MCP_STATELESS_HTTP` | `true` | Streamable HTTP ohne serverseitige Session-Pflicht |
| `HTTP_TIMEOUT` | `30` | Request-Timeout in Sekunden |
| `ENTSCHEIDSUCHE_VERIFY_SSL` | `true` | TLS-Prüfung für Elasticsearch-/Facetten-Upstream |
| `CORS_ALLOW_ORIGINS` | `*` | Erlaubte Browser-Origin(s) für den HTTP-Transport |
| `LOG_LEVEL` | `INFO` | Loglevel |

## Schnelltest mit `curl`

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

# search
curl -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
     http://localhost:8765/mcp \
     -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search","arguments":{"query":"\"BGE 142 III 1\"","language":"de","size":3}}}'
```

Praktischer ist allerdings ein echter MCP-Client (z.B. der MCP-Inspector,
`npx @modelcontextprotocol/inspector`) — dieser übernimmt die Streamable-HTTP-
Session-Verwaltung automatisch.

## Deployment auf Debian (mcp.entscheidsuche.ch)

Das Repository enthält eine systemd-Unit, einen nginx-vHost und ein
Installations-Script:

```bash
ssh root@mcp.entscheidsuche.ch
git clone https://github.com/<org>/entscheidsuche-mcp /opt/entscheidsuche-mcp
sudo bash /opt/entscheidsuche-mcp/deploy/install.sh

# TLS-Zertifikat:
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d mcp.entscheidsuche.ch
```

Das Installations-Script:

1. Legt den Systemnutzer `entscheidsuche` an
2. Erzeugt ein Python-venv unter `/opt/entscheidsuche-mcp/.venv`
3. Kopiert `/etc/entscheidsuche-mcp.env` aus `.env.example`
4. Installiert die systemd-Unit und startet den Service
5. Installiert den nginx-vHost (TLS-Zertifikat anschließend mit certbot holen)

Status prüfen:

```bash
systemctl status entscheidsuche-mcp
journalctl -u entscheidsuche-mcp -f
```

DNS — `mcp.entscheidsuche.ch` muss auf den Server zeigen (A/AAAA-Record).

## Konfiguration in MCP-Clients

### Claude Desktop / Claude Code

`~/.claude/mcp.json` (oder via `claude mcp add ...`):

```jsonc
{
  "mcpServers": {
    "entscheidsuche": {
      "type": "http",
      "url": "https://mcp.entscheidsuche.ch/mcp"
    }
  }
}
```

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector https://mcp.entscheidsuche.ch/mcp
```

## Suchsyntax — Cheatsheet

| Aufgabe | Beispiel |
| --- | --- |
| Volltext, alle Begriffe (AND) | `Mietzins Kündigung` |
| Phrase, exakte Reihenfolge | `"fristlose Kündigung"` |
| Geschäftsnummer | `"BGE 142 III 1"` |
| OR | `Mietzins OR Pachtzins` |
| Negation | `Mietzins NOT Erhöhung` |
| Wildcard | `Mietz*` |
| Feld-Suche | `title.de:"Kündigung"` |

Die Standard-Verknüpfung zwischen Wörtern ist `AND`. Gesucht wird in
`title`, `abstract`, `meta`, `attachment.content` und `reference`.

Die Tool-Parameter `language` und `sort` sind optional. Wenn `language`
weggelassen wird, verwendet der Server `de`. Wenn `sort` fehlt, wird nach
`relevance` sortiert. Erlaubte Sprachen für Such- und Dokument-Requests sind
`de`, `fr` und `it`.

Für Geschäftsnummern, BGE-Zitate und ähnliche Referenzen gibt es zusätzlich
das Tool `search_by_business_number`. Es setzt die angegebene Nummer automatisch
in Anführungszeichen und führt damit eine exakte Phrasensuche aus.

## Lizenz

MIT
