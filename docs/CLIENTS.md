# entscheidsuche-mcp in Clients einbinden

Der Server ist erreichbar unter

```
https://mcp.entscheidsuche.ch/mcp
```

Transport: **Streamable HTTP** (Model Context Protocol, JSON-RPC 2.0 über
`POST` mit optionalem SSE-Streaming für Server-Push). Es ist keine
Authentifizierung nötig — ein Konto ist nicht erforderlich, der Server
ist öffentlich.

> **Stand und Vorbehalt:** Die hier beschriebenen Schritte sind nicht in
> jedem aufgeführten Client praktisch durchgespielt — sie beruhen teils
> auf den offiziellen Dokumentationen der jeweiligen Anbieter. MCP ist
> ein junges Protokoll, und Anbieter ändern Konfigurations-Pfade,
> Tarif-Einteilungen, Tool-Namen und Discovery-Mechanismen regelmässig
> und manchmal kurzfristig. Bei Abweichungen zur offiziellen Doku gilt
> die offizielle Doku — Verweise auf die jeweiligen Hilfeseiten stehen
> am Ende des entsprechenden Abschnitts. Bitte Rückmeldung geben, wenn
> ein Schritt nicht mehr passt.

## Übersicht

| Client | Frontend / Service | MCP-Anbindung | Subscription für MCP |
| --- | --- | --- | --- |
| [Claude.ai (Web/App)](#claude-web-und-mobile-app) | claude.ai, iOS/Android, Desktop | Custom Connectors (HTTP) | Claude Pro / Max / Team / Enterprise |
| [Claude Desktop](#claude-desktop-stdio-bridge) | macOS-/Windows-App | JSON-Config (stdio über Bridge) | gratis möglich, Pro empfohlen |
| [Claude Code](#claude-code-cli) | Terminal | `claude mcp add` (HTTP nativ) | API-Credits oder Pro/Max-Plan |
| [Claude API](#claude-api-direkt) | Eigener Code | `mcp_servers` im Tools-Aufruf | API-Konto (pay-as-you-go) |
| [ChatGPT](#chatgpt) | chatgpt.com, Desktop, Mobile | Custom Connectors (Deep Research / Agent) | ChatGPT Pro / Team / Enterprise / Edu |
| [VS Code + Copilot](#vs-code-mit-github-copilot) | VS Code | `.vscode/mcp.json` | GitHub Copilot Pro o.ä. |
| [Cursor](#cursor) | Cursor-Editor | UI oder `~/.cursor/mcp.json` | gratis und kostenpflichtig |
| [Cline / Continue.dev / Zed](#weitere-open-source-clients) | VS Code / Editor | Editor-spezifische Config | Open Source / gratis |
| [MCP Inspector](#mcp-inspector-zum-testen) | Browser-Tool | URL eingeben | gratis |

---

## Claude (Web und mobile App)

Wer ein Claude-Konto mit Pro-, Max-, Team- oder Enterprise-Plan hat, kann
den Server als **Custom Connector** anlegen — der ist dann im Web, in der
mobilen App und in der Desktop-App gleichzeitig verfügbar.

1. claude.ai → **Settings → Connectors** (in einigen Tarifen
   **Settings → Integrations**).
2. *„Add custom connector"* wählen.
3. Eintragen:
   - **Name:** `entscheidsuche`
   - **Remote MCP server URL:** `https://mcp.entscheidsuche.ch/mcp`
   - **Authentication:** *None*
4. Bestätigen — die Tools `search`, `search_by_case_number`,
   `fetch_document`, `list_hierarchy`, `list_facets`, `server_info` müssen
   anschliessend in der Tool-Liste erscheinen.

In einer neuen Konversation kann der Connector dann pro Chat aktiviert
werden (Symbol unter dem Eingabefeld).

> **Voraussetzung:** Claude Pro / Max / Team / Enterprise. Custom
> Connectors sind im Free-Plan nicht verfügbar. Team- und
> Enterprise-Workspaces können verlangen, dass der Workspace-Admin den
> Connector zentral freischaltet.
>
> Doku: <https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp>

---

## Claude Desktop (stdio-Bridge)

Die Desktop-App liest MCP-Konfigurationen aus
`claude_desktop_config.json`. Der Pfad:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Die Desktop-App spricht historisch nur **stdio**-MCP-Server. Für unseren
HTTP-Server gibt es die kleine Bridge `mcp-remote`, die per `npx`
ausgeführt wird:

```jsonc
{
  "mcpServers": {
    "entscheidsuche": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://mcp.entscheidsuche.ch/mcp"]
    }
  }
}
```

Voraussetzung: Node.js 18+ ist installiert (für `npx`). Nach dem
Speichern Claude Desktop einmal komplett beenden und neu starten.

> **Subscription:** Claude Desktop selbst ist gratis. Wer ein Pro-Konto
> hat, kann alternativ auf den Web-Connector-Weg ausweichen, ohne lokal
> Node zu installieren.
>
> Doku: <https://modelcontextprotocol.io/docs/develop/connect-local-servers> ·
> `mcp-remote`: <https://www.npmjs.com/package/mcp-remote>

---

## Claude Code (CLI)

`claude` (Anthropic CLI) unterstützt Streamable-HTTP-MCP-Server nativ:

```bash
claude mcp add --transport http entscheidsuche https://mcp.entscheidsuche.ch/mcp
```

Der Server steht danach in jeder `claude`-Session zur Verfügung. Status
prüfen mit `claude mcp list`, entfernen mit
`claude mcp remove entscheidsuche`.

> **Voraussetzung:** Claude Code (Installation per
> `npm install -g @anthropic-ai/claude-code`) und ein Anthropic-Konto mit
> API-Credits oder ein Pro-/Max-Plan.
>
> Doku: <https://docs.claude.com/en/docs/claude-code/mcp>

---

## Claude API (direkt)

Wer eigene Anwendungen baut, kann den Server in jedem `messages`-Aufruf
über das `mcp_servers`-Feld einbinden — ohne lokal etwas zu installieren:

```python
import anthropic

client = anthropic.Anthropic()
resp = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=2048,
    mcp_servers=[
        {
            "type": "url",
            "url": "https://mcp.entscheidsuche.ch/mcp",
            "name": "entscheidsuche",
        }
    ],
    messages=[{"role": "user", "content": "Suche Bundesgerichtsentscheide zu BGE 142 III 1."}],
    extra_headers={"anthropic-beta": "mcp-client-2025-04-04"},
)
print(resp.content)
```

> **Voraussetzung:** Anthropic-API-Konto mit Pay-as-you-go-Credits.
> Der MCP-Connector-Beta-Header ist Stand 2025/2026 nötig — Name kann
> wechseln, daher Doku konsultieren.
>
> Doku: <https://docs.claude.com/en/docs/agents-and-tools/mcp-connector>

---

## ChatGPT

ChatGPT unterstützt Custom-MCP-Server in den **Connectors / Deep
Research**. Einrichtung über die Web-Oberfläche:

1. chatgpt.com → **Settings → Connectors** → *„Create"*.
2. **MCP Server URL:** `https://mcp.entscheidsuche.ch/mcp`,
   Auth = *No authentication*.
3. Connector im Chat aktivieren (Tools-Menü) bzw. in einem
   Deep-Research-Run mit anhaken.

> **Voraussetzung:** ChatGPT Pro, Team, Enterprise oder Edu.
> Im Free- und Plus-Tier waren Custom-MCP-Connectors zuletzt nicht
> verfügbar — der Status kann sich ändern.
>
> **Wichtig:** OpenAIs Deep-Research-Modus erwartet aus historischen
> Gründen Tools mit den Namen `search` und `fetch`. Unser Server stellt
> `search` und `fetch_document` zur Verfügung; bei Deep-Research-Aufrufen
> kann es sein, dass `fetch_document` nicht automatisch erkannt wird.
> Im normalen Chat-Modus mit Connector-Toolauswahl ist das kein Problem.
>
> Doku: <https://platform.openai.com/docs/mcp>

---

## VS Code mit GitHub Copilot

Copilot Chat (Agent-Mode) liest MCP-Konfigurationen aus
`.vscode/mcp.json` im Workspace **oder** aus der Settings-UI:

```jsonc
// .vscode/mcp.json
{
  "servers": {
    "entscheidsuche": {
      "type": "http",
      "url": "https://mcp.entscheidsuche.ch/mcp"
    }
  }
}
```

Aktivieren über *Command Palette → „MCP: Add Server"* oder durch direktes
Editieren der Datei. Im Chat dann *„Agent"*-Modus wählen, das
Werkzeug-Symbol klicken und die Tools aktivieren.

> **Voraussetzung:** GitHub Copilot Pro, Pro+, Business oder Enterprise.
>
> Doku: <https://code.visualstudio.com/docs/copilot/customization/mcp-servers>

---

## Cursor

Cursor (KI-Code-Editor, Fork von VS Code) unterstützt MCP über
`~/.cursor/mcp.json`:

```jsonc
{
  "mcpServers": {
    "entscheidsuche": {
      "url": "https://mcp.entscheidsuche.ch/mcp"
    }
  }
}
```

Alternativ über die UI: **Settings → MCP → Add new MCP server**.

> **Voraussetzung:** Cursor (gratis Tier verfügbar; bezahlte Pläne für
> erweiterte KI-Features). MCP-Funktionalität ist in allen Tiers
> enthalten.
>
> Doku: <https://docs.cursor.com/context/model-context-protocol>

---

## Weitere Open-Source-Clients

Diese Clients sprechen den Server gleichermassen an — mit kleinen
Konfigurations-Unterschieden:

- **Cline** (VS Code Extension, Open Source): `cline_mcp_settings.json`
  mit demselben Schema wie Cursor.
- **Continue.dev** (VS Code Extension): `~/.continue/config.json` →
  `mcpServers`-Block analog.
- **Zed** (Editor): `Settings → Tools → MCP Servers` → URL eintragen.
- **5ire**, **goose**, **LibreChat**, **Open WebUI**, **LobeChat**:
  jeweils ein „Custom MCP server"-Eintrag mit der URL oben.

> **Subscription:** Editor-/Frontend-Lizenz nach Wahl, MCP-Anbindung
> grundsätzlich kostenlos. Was kostet, ist das Sprachmodell, das hinten
> dran hängt — wer einen lokalen Llama, Qwen o.ä. nutzt, kommt komplett
> ohne kommerzielles Abo aus.

---

## MCP Inspector (zum Testen)

Vor jeder produktiven Einbindung lohnt ein kurzer Smoke-Test mit dem
offiziellen Inspector:

```bash
npx @modelcontextprotocol/inspector
```

Im Browser-UI:

- Transport: **HTTP / Streamable HTTP**
- Server-URL: `https://mcp.entscheidsuche.ch/mcp`
- Auth: leer

Damit lassen sich Tool-Listen, Schemas und einzelne Aufrufe ohne
Subscription oder Account ausprobieren — ideal als Erstkontakt und für
Debugging.

> **Voraussetzung:** Node.js 18+. Komplett gratis und Open Source.
>
> Doku: <https://github.com/modelcontextprotocol/inspector>

---

## Hinweise zu Datenschutz und Lizenz

- Der MCP-Server selbst speichert keine Anfragen.
- Was die jeweils gewählte LLM-Plattform mit den übermittelten Anfragen
  macht, regeln deren Nutzungsbedingungen — beim Einsatz in einem
  beruflich-juristischen Kontext sind insbesondere Berufsgeheimnis und
  DSGVO/DSG zu prüfen.
- Die Inhalte stammen aus [entscheidsuche.ch](https://entscheidsuche.ch);
  Lizenzbedingungen und Quellenangaben dort beachten.
