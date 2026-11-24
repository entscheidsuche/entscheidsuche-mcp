---
name: get-started
description: How to research Swiss court decisions with entscheidsuche.ch — which search to use, how to filter by court and date, and how to cite decisions with links.
---

# Researching Swiss case law with entscheidsuche.ch

entscheidsuche.ch contains published decisions of Swiss courts and authorities:
the Federal Supreme Court (BGer, BGE), the other federal courts, the cantonal
courts and administrative authorities. Decisions are written in German, French
or Italian. It contains court decisions only — no legislation and no
commentary.

## Which tool to use

- A legal question or a description of facts: call `search` with the question
  in full sentences, in any language. It searches by meaning and also finds
  decisions in the other national languages.
- A case number or citation such as `5A_396/2015`, `BGE 142 III 1` or
  `ZB.2021.48`: call `search_by_case_number`.
- Exact words, a quoted phrase, Boolean operators, a complete result list,
  sorting by date or pagination: call `keyword_search`.
- The full text of a decision: call `fetch` with the `id` of a hit. Read the
  full text before summarising a decision or quoting from it; a search hit
  contains only an excerpt.
- Restricting results to a canton or court: pass hierarchy IDs such as
  `CH_BGer` (Federal Supreme Court) or `ZH_OG` (Zurich Obergericht) in
  `hierarchy`. `list_hierarchy` returns the IDs with decision counts.

## Presenting results

- Name the court, the decision date and the case number of every decision
  you rely on, and link to it with the `url` field of the hit.
- Quote a decision in its original language and add a translation if the user
  writes in another language.
- The AI search evaluates the best-matching passages, so `total` is not the
  number of all relevant decisions. Do not present it as a complete count;
  use `keyword_search` when completeness matters.
- Say so when the search finds nothing suitable. Do not cite decisions that
  the tools did not return.
- The decisions are information about case law, not legal advice on an
  individual case.
