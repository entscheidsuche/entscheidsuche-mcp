# Ablauf für das Demo-Video

Die Prompts unten sind dieselben wie in `plugin.json` unter
`extensions.com.openai.review.test_cases`. Wird hier etwas geändert, muss es
dort ebenfalls geändert werden.

## Vorbereitung

- In ChatGPT (Browser) ist das Plugin über «Add custom MCP server» mit
  `https://mcp.entscheidsuche.ch/mcp` angelegt; auf der Plugins-Seite vorher
  «Refresh» wählen.
- Seitenleiste einklappen, damit keine anderen Unterhaltungen im Bild sind.
- Jeden Fall in einer **neuen Unterhaltung** stellen und das Plugin über `@`
  auswählen.
- Erst ein Durchlauf ohne Aufnahme. Weicht ein Tool-Aufruf oder ein Ergebnis
  vom erwarteten Verhalten ab, zuerst das Manifest anpassen.
- Aufnahme: macOS-Bildschirmaufnahme (Cmd+Shift+5), nur das Browserfenster.
  Pausen und Wartezeiten müssen nicht vermieden werden; sie werden in der
  Nachbearbeitung gekürzt.

## Aufrufe im Server-Log festhalten

ChatGPT zeigt die einzelnen Tool-Aufrufe nicht an. Für das Video werden sie
aus dem Access-Log des Servers belegt. Dazu vor der Aufnahme in
`/etc/entscheidsuche-mcp.env` vorübergehend setzen und den Dienst neu starten:

    ESC_ACCESS_LOG_SESSION=true
    ESC_ACCESS_LOG_ARGS_MAX=1000

`ESC_ACCESS_LOG_SESSION` schreibt je Aufruf ein Feld `conv=` mit einer
gehashten Kennung der Unterhaltung (ChatGPT schickt sie als
`_meta["openai/session"]`); alle Aufrufe einer Unterhaltung tragen dieselbe
Kennung. `ESC_ACCESS_LOG_ARGS_MAX` protokolliert die Argumente bis zur
angegebenen Länge, also auch die Suchanfragen.

Nach der Aufnahme:

    grep "$(date +%F)" /var/log/entscheidsuche-mcp/access.log | grep "conv=" > ~/video-log.txt
    cut -d' ' -f1-2 --complement ~/video-log.txt | grep -o "conv=[0-9a-f-]*" | sort | uniq -c

Die zweite Zeile zeigt je Unterhaltung die Zahl der Aufrufe. Danach beide
Einstellungen wieder auf den vorherigen Stand setzen
(`ESC_ACCESS_LOG_SESSION=false`) und den Dienst neu starten.

## Was bei jedem Fall zu sehen sein soll

1. Der Prompt wird eingegeben (einfügen genügt).
2. Der Tool-Aufruf wird aufgeklappt, sodass Tool-Name und Argumente lesbar
   sind.
3. Die Antwort wird einmal ganz durchgescrollt.
4. Bei Fall 1 und Fall 2 je einen Link anklicken, sodass der Entscheid auf
   entscheidsuche.ch erscheint.

## Die fünf Fälle des Manifests

### Fall 1: Frage eines ausländischen Besuchers (englisch)

Erwartet: mehrere Aufrufe von `search` (KI-Suche), `fetch` für die einschlägigen Entscheide; Antwort auf Englisch mit Entscheiden in Französisch und Deutsch (u. a. Vaud CR.2005.0130, Genf A/5147/2007, Neuchâtel ARMP.2019.85, Vaud CREP 27 mars 2013/186).

```text
If, as a tourist, I exceed the speed limit of 50 km/h with a rental car by more than 25 km/h or more than 40 km/h, what kind of sanction can apply to me? Is this limited to a fine that might not be enforceable abroad, can I go to jail directly in Switzerland and can the car be confiscated? Please provide references in the Swiss case law by using entscheidsuche.ch
```

### Fall 2: Zitate in einem Aufsatz prüfen (französisch)

Erwartet: je Zitat `search_by_case_number` (oder `search`) und `fetch`. Drei Zitate stimmen (ATF 138 III 59, Genf C/14467/2021, 4A_639/2018). Zwei sind falsch: «4A_32/2009 du 24 mars 2009» ist ein Zahlendreher (gemeint ist 4A_23/2009; ein Urteil 4A_32/2009 gibt es, aber vom 7. Oktober 2009), und 4A_447/2013 sagt das Gegenteil dessen, was der Auszug behauptet (Beweislast beim Vermieter, nicht beim Mieter).

```text
Utilise entscheidsuche.ch. Voici un extrait d'un article juridique. Recherche chaque décision citée et vérifie si elle existe, si le tribunal et la date sont exacts et si la décision dit bien ce que l'extrait lui attribue. Indique un lien pour chaque décision.

En droit suisse du bail, le congé donné par le bailleur pour ses propres besoins soulève plusieurs questions. Un congé ordinaire ne suppose pas de motif particulier; il trouve toutefois sa limite dans les règles de la bonne foi (ATF 138 III 59 consid. 2.1). Dans ce cadre, le besoin invoqué par le bailleur ne doit pas être immédiat ou urgent (Cour de justice de Genève, Chambre des baux et loyers, arrêt du 12 décembre 2024, C/14467/2021). Lorsque le bailleur invoque en revanche un besoin propre et urgent, ce besoin s'apprécie au moment de la notification du congé (arrêt du Tribunal fédéral 4A_639/2018 du 21 novembre 2019). Il appartient alors au bailleur d'établir l'existence d'un besoin urgent (arrêt du Tribunal fédéral 4A_32/2009 du 24 mars 2009). Selon l'arrêt du Tribunal fédéral 4A_447/2013 du 20 novembre 2013, c'est toutefois au locataire de prouver que le bailleur n'a pas de besoin urgent.
```

### Fall 3: Recherche für eine Falllösung, Aufgabe als Link (deutsch)

Erwartet: ChatGPT liest die Aufgabe über den Link (eigener Webzugriff, nicht das Plugin), danach mehrere Aufrufe von `search` und für die einschlägigen Entscheide `fetch` (Volltext), Entscheide nach Rechtsfrage geordnet, mit Link und kurzer Zusammenfassung. Die Aufgabe ist «Heimlichers Hausschwamm», Universität Bern, Frühjahrssemester 2014.

```text
Nutze entscheidsuche.ch. Unter diesem Link steht die Aufgabenstellung einer Falllösungsaufgabe (Hausarbeit) im Privatrecht (Universität Bern): https://www.ziv.unibe.ch/studium/fallloesungen_bachelorarbeit/fruehere_fallloesungen/e205616/e205708/HeimlichersHausschwamm.pdf Lies den Sachverhalt und die Fragen und suche mir zu den wesentlichen Rechtsfragen einschlägige Schweizer Gerichtsentscheide. Ordne sie nach Rechtsfrage und gib zu jedem Entscheid Gericht, Datum, Geschäftsnummer und Link mit kurzer Zusammenfassung an. Identifiziere dafür relevante Urteile und lese diese dann im Volltext durch, um die Aussagen zu verifizieren.
```

### Fall 4: Neu aufgenommene Entscheide zu einem Thema (deutsch)

Erwartet: `keyword_search` (oder `search`) mit `scrape_date_from` etwa eine Woche und `decision_date_from` etwa sechs Monate vor dem heutigen Datum.

```text
Nutze entscheidsuche.ch: Gib mir neue Entscheide zum Thema Datenschutz, die erst in der letzten Woche in die Sammlung aufgenommen (gescrapt) wurden und nicht älter als sechs Monate sind.
```

### Fall 5: Entscheide eines Kantons und Jahres zählen (italienisch)

Erwartet: `keyword_search` mit `query` «*», `hierarchy` ["GE"] und dem Jahr 2026 als Datumsbereich; die Zahl steht im Feld `total` (am 24.11.2026: 5338).

```text
Usa entscheidsuche.ch: quante decisioni dei tribunali del Cantone di Ginevra pronunciate nel 2026 sono pubblicate nella raccolta?
```

### Zugabe (nicht im Manifest): Rechtsfrage mit kurzem Sachverhalt (deutsch)

Kann im Video zusätzlich gezeigt werden. Erwartet: `search`, für die Auswertung `fetch`.

```text
Ein Hundehalter hat einen an sich gutmütigen Hund, der bislang nicht auffällig war. Eines Tages beisst der Hund ein laut spielendes und sich dabei hektisch bewegendes Kind in der Umgebung. Muss der Hundehalter hier haften? Suche über entscheidsuche.ch einschlägige Rechtsprechung hierzu und werte diese aus.
```

## Zum Schluss: ein Fall, in dem das Plugin nicht handeln soll

Einen der drei Prompts ohne `@`-Auswahl eingeben; es darf kein Tool von entscheidsuche.ch aufgerufen werden.

```text
What will the weather be like in Zurich tomorrow?
Find recent decisions of the US Supreme Court on qualified immunity.
Write a short birthday poem for my colleague Anna.
```
