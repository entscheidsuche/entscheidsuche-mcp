# Einreichung ins Plugin-Verzeichnis von ChatGPT

Dieser Ordner ist das Plugin-Paket für das öffentliche Plugin-Verzeichnis von
OpenAI (ChatGPT und Codex). Eingereicht wird über
<https://platform.openai.com/plugins>. Massgeblich sind die Seiten
<https://developers.openai.com/plugins/deploy/submission>,
<https://developers.openai.com/plugins/deploy/app-review> und
<https://developers.openai.com/plugins/plugin-guidelines> (Stand 24.11.2026).

## Inhalt des Pakets

| Datei | Zweck |
|---|---|
| `plugin.json` | Manifest: Name, Texte für das Verzeichnis, Icons, Testfälle, Übersetzungen, Länder |
| `mcp.json` | der MCP-Server `https://mcp.entscheidsuche.ch/mcp`, ohne Authentifizierung |
| `skills/get-started/SKILL.md` | Anleitung für das Modell: welche Suche wofür, wie zitieren |
| `assets/` | `logo.png`, `logo-dark.png` (Listing), `icon.png`, `icon-dark.png` (Eingabefeld), je 512 × 512 |

ZIP erzeugen (im Ordner `chatgpt-plugin`, damit `plugin.json` im Wurzelverzeichnis des ZIP liegt):

    cd chatgpt-plugin
    zip -r ../entscheidsuche-plugin.zip plugin.json mcp.json skills assets

## Vor dem Hochladen noch zu klären oder einzutragen

1. **Demo-Video.** `extensions.com.openai.review.demo_recording_url` ist leer.
   OpenAI verlangt eine für die Prüfer abrufbare Aufnahme, die die Testfälle
   zeigt. Ablauf siehe unten. Die Adresse vor dem Zippen eintragen.
2. **Kategorie.** `interface.category` steht auf `Productivity`, weil die
   Dokumentation nur diesen und `Developer Tools` als Beispiele nennt. Die
   vollständige Liste zeigt erst das Portal; dort die passendste wählen und
   den Wert im Manifest angleichen.
3. **Nutzungsbedingungen.** `termsOfServiceURL` ist für die Prüfung Pflicht
   und zeigt vorläufig auf `https://entscheidsuche.ch/dataUsage`. Ob diese
   Seite als Nutzungsbedingungen taugt, ist zu entscheiden.
4. **Support-Adresse.** `supportURL` muss eine HTTPS-Adresse sein und zeigt
   auf `https://entscheidsuche.ch/about` (dort steht info@entscheidsuche.ch).
5. **Locale-Schlüssel der Übersetzungen.** Verwendet sind `de-DE`, `fr-FR`,
   `it-IT`; die Dokumentation nennt als Beispiele `fr-FR` und `ja-JP`. Lehnt
   das Portal einen Schlüssel ab, steht in der Fehlermeldung der zulässige.
6. **Datenschutzerklärung** ergänzen, siehe unten.

## Schritte

1. Im OpenAI-Platform-Dashboard die Organisation «Verein entscheidsuche.ch»
   verifizieren (Unternehmensverifizierung) und ein Projekt **ohne**
   EU-Datenresidenz verwenden.
2. Im Portal «Upload new or existing plugin» wählen, die verifizierte
   Identität auswählen, das ZIP hochladen, die automatischen Befunde beheben.
3. Reiter MCP: Server verbinden. Das Portal zeigt einen Token für die
   Domain-Verifizierung. Auf dem Server:

       echo -n 'TOKEN' | sudo tee /etc/entscheidsuche-mcp/openai-apps-challenge
       sudo chmod 644 /etc/entscheidsuche-mcp/openai-apps-challenge

   Voraussetzung ist die aktuelle `deploy/nginx.conf` (Abschnitt
   `/.well-known/openai-apps-challenge`). Kontrolle:
   `curl https://mcp.entscheidsuche.ch/.well-known/openai-apps-challenge`
   muss genau den Token liefern. Danach «Verify Domain» und «Scan Tools».
4. Testfälle und Video prüfen, «Submit for review», Richtlinien bestätigen.
5. Nach der Freigabe selbst veröffentlichen.

Nach der Veröffentlichung übernimmt OpenAI geänderte Tool-Beschreibungen
automatisch; die Adresse `https://mcp.entscheidsuche.ch` (Schema, Host, Port)
lässt sich zwischen Versionen nicht mehr ändern.

## Ablauf für das Demo-Video

Plugin in ChatGPT über den privaten Weg einbinden (Plugins → Plus-Zeichen →
«Add custom MCP server»), Bildschirm aufnehmen und nacheinander die fünf
Prompts aus `review.test_cases.positive` eingeben, jeweils die Tool-Aufrufe
aufklappen und einen Link zu entscheidsuche.ch öffnen. Zum Schluss einen der
drei negativen Prompts eingeben und zeigen, dass kein Tool aufgerufen wird.
Vorher jeden Testfall einmal durchspielen: Weichen die Ergebnisse von
`expected_behavior` ab, den Text im Manifest anpassen.

## Ergänzung der Datenschutzerklärung (Entwurf)

Die Erklärung unter <https://entscheidsuche.ch/datenschutz> nennt bisher nur
die Website. OpenAI verlangt Angaben zu Datenkategorien, Zwecken,
Empfängern, Aufbewahrungsdauer und Rechten der Nutzer; fehlende Angaben sind
ein häufiger Ablehnungsgrund. Vorschlag für einen zusätzlichen Abschnitt:

> **KI-Schnittstelle (MCP).** Unter mcp.entscheidsuche.ch bieten wir eine
> Schnittstelle an, über die KI-Assistenten wie ChatGPT oder Claude in
> unseren Entscheiden suchen können. Nutzen Sie die Schnittstelle über einen
> solchen Assistenten, übermittelt dessen Anbieter die Suchanfrage, die
> gewählten Filter und die Kennung der abgerufenen Entscheide an uns. Ihr
> Name, Ihr Konto beim Anbieter und der Verlauf Ihrer Unterhaltung werden
> uns nicht übermittelt; wir sehen in der Regel die IP-Adresse des Anbieters,
> nicht Ihre eigene. Wir protokollieren je Aufruf Zeitpunkt, aufgerufene
> Funktion, Name und Version des anfragenden Programms, Antwortgrösse,
> Dauer und IP-Adresse. [VARIANTE A: Die Suchanfragen selbst werden nicht
> protokolliert.] [VARIANTE B: Die Suchanfragen werden auf 200 Zeichen
> gekürzt mitprotokolliert.] Aus diesen Protokollen erstellen wir
> zusammengefasste Nutzungszahlen ohne Bezug zu einzelnen Personen; sie
> dienen dem Betrieb, der Fehlersuche und der Abwehr von Angriffen. Die
> Protokolle werden nach [DAUER] gelöscht. Zur Beantwortung der Anfrage
> wird sie an unsere Suchserver bei [cyon.ch und pansoft.de] weitergeleitet;
> die Schnittstelle selbst wird bei [HOSTER] betrieben. Eine Weitergabe an
> weitere Dritte findet nicht statt. Für die Verarbeitung beim Anbieter des
> KI-Assistenten gelten dessen Datenschutzbestimmungen. Auskunft und
> Löschung können Sie unter info@entscheidsuche.ch verlangen.

Die eckigen Klammern sind am Server zu prüfen:
`grep ESC_ACCESS_LOG /etc/entscheidsuche-mcp.env` (ohne
`ESC_ACCESS_LOG_ARGS_MAX=0` protokolliert der Server die Argumente bis 200
Zeichen), `cat /etc/logrotate.d/entscheidsuche-mcp` und die Aufbewahrung des
nginx-Protokolls `/var/log/nginx/mcp.entscheidsuche.ch.access.log`.

Für die Prüfer sollte die Erklärung auch auf Englisch vorliegen; die
italienische Fassung fehlt auf der Website bisher ganz.

## Durchsicht der Tools gegen die Richtlinien

Erfüllt: Alle neun Tools setzen `readOnlyHint: true`, `destructiveHint:
false` und `openWorldHint: true`; jedes hat eine Beschreibung mit Zweck,
Verhalten und Grenzen; keine Werbung, keine Vergleiche mit anderen Diensten.

Zur Entscheidung, bisher nicht geändert:

- `server_info` gibt interne Adressen aus (`search_url`, `facets_url`,
  `embedding_model`). Die Prüfregeln verlangen Antworten ohne interne
  Kennungen. Vorschlag: die drei Felder weglassen, Name und Version
  behalten.
- `fetch` und `fetch_document` sowie `search`, `semantic_search` und
  `keyword_search` überschneiden sich. Verboten ist das nicht; die
  Beschreibungen sagen, worin sie sich unterscheiden.
