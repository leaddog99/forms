# Candidate filters — one per-domain rule surface, modelled on SEMrush's advanced filter

**Status: DESIGN. Nothing built.** Written 2026-08-12.

## The problem

Five separate mechanisms drop candidate URLs before we ever fetch them, and they overlap:

| ledger reason | mechanism | domains using it | overturnable |
|---|---|---|---|
| `disallowed-domain` / `disallowed-path` | global curator blocklist | global | no |
| `domain-exclude` | per-domain `exclude_words` | **1** of 322 | no |
| `off-path` | per-domain `recipe_path` (single prefix) | **13** of 322 | no |
| `archive-url` | hardcoded `/tag/`, `/category/`, feeds | global | no |
| `url-prefilter` | AI-built two-list vocabulary | **322** of 322 | yes |

Each was added for a real case, none was designed against the others, and the curator has
to know which of five places to go. `exclude_words` on exactly one domain suggests at least
one is dead weight rather than a thing to preserve.

`recipe_path` is also mis-shelved in the form: it sits outside both the SERP and SEMrush
blocks, and its label says "scopes EVERY source" while 12 of its 13 users are
`backlinks_file` domains. It is not a SERP field.

## The model: copy SEMrush's advanced filter

The curator already uses SEMrush's advanced filter daily, so the mental model transfers at
zero training cost. Its shape is a list of conditions, ANDed:

    { include | exclude ,  field ,  criterion ,  value }

**This is the opposite of what we deleted on 2026-08-12.** That change removed code which
*generated* SEMrush's filter into a URL — their side, undocumented `fld`/`cri` codes, where
copy-paste beats any generator. Copying the filter's *semantics* for a filter **we**
evaluate is safe: we own the evaluator, so there is nothing to guess and nothing to
silently mis-key.

## What we can filter on (grounded — these exist pre-fetch)

The whole point of this stage is that it runs **before any fetch**, so it is free. A
SEMrush export row and a SERP result both give us more than the URL:

| field | source | type |
|---|---|---|
| `url` | both | string |
| `url_path` / `url_segment` / `url_depth` | derived from url | string / int |
| `title` | SEMrush export column, SERP result | string |
| `traffic` | SEMrush export | number |
| `traffic_pct` | SEMrush export | number |
| `rank` | SEMrush export order / SERP position | number |

`title` and `traffic` are the reason this is worth doing. A prefix can express
"under /recipes"; it cannot express *"under /recipes, but not the roundups, and only pages
pulling real traffic"* — which is the actual editorial intent behind most of the current
`recipe_path` + `exclude_words` pairings.

## Criteria

String fields: `containing`, `not containing`, `exactly`, `starts with`, `ends with`,
`matches` (regex).
Number fields: `>`, `>=`, `<`, `<=`, `=`.

Conditions AND together. A rule is `{keep: [...conditions], drop: [...conditions]}` —
drop wins on conflict, and an empty `keep` means "keep everything not dropped".

## Hard constraint: no LLM per candidate

A publisher refresh evaluates ~400 candidates. An LLM call per candidate is the wrong
shape — it converts a free stage into a per-run cost, at the exact point in the pipeline
whose job is to *avoid* spending.

The right shape already exists in this codebase: **`url_prefilter` has an AI build a
two-list vocabulary offline, and the runtime applies it cheaply.** Follow that. The curator
describes the rule in plain English; an LLM **compiles it once** into the condition list
above; runtime evaluates a bare string and a couple of numbers with no I/O.

    curator: "recipes only, skip the gift guides and anything with no traffic"
       LLM compiles, once:
         keep: url_path starts with /recipes
         drop: title containing "gift guide"
         drop: traffic < 50

The compiled conditions are shown back for editing — the curator can always hand-author or
correct them, and the LLM is a convenience for authoring, never a runtime dependency.

## Provenance decides overturnability

The candidate ledger already encodes the distinction this design must preserve
(`_REASON_MAP` in `input/pipeline/candidate_ledger.py`): a rule the curator set by hand is
**not overturnable** — it is a fact about their intent. An inference we drew **is**
overturnable, so the AI editor may argue with it.

So each condition carries its author:

- `author: curator` → drops are **not** overturnable.
- `author: llm` → drops **are** overturnable, and land in the reconsiderable pool.

A curator who edits an LLM-compiled condition takes ownership of it and it becomes
curator-authored — the same rule as `paywall_adj_source='manual'`.

## Migration

1. `recipe_path` → one `keep` condition (`url_path starts with /<value>`) per domain. 13 rows.
2. `exclude_words` → `drop` conditions. 1 row. Confirm it is wanted at all before porting.
3. `archive-url` and the global blocklist stay **code**, not per-domain config — they are
   universal facts about URL shapes, not editorial choices, and belong in one place.
4. `url_prefilter` stays as-is. It is a different thing: a learned vocabulary over the whole
   corpus, not a per-publisher editorial rule. Do not fold it in.

So the collapse is 5 → 3: **per-domain rules** (this), **universal URL shapes** (code), and
**the learned vocabulary** (`url_prefilter`).

## UI

One block, in the harvest section, above "Keep top N" — it applies to every source, so it
belongs with the source-agnostic settings and NOT inside the SERP or SEMrush containers.
Rows of `[Include|Exclude] [field] [criterion] [value]` with an add/remove control, exactly
like SEMrush's. A plain-English box above it that compiles to rows on demand.

Delete the current `recipe_path` field and its explanatory paragraph once ported.

## Open questions

- Is `exclude_words` (1 domain) worth porting, or just retired?
- Should a rule be attachable to a **collection** rather than a domain, so the same
  editorial scope can apply across publishers? Probably later.
- `traffic` is only present on the `backlinks_file` path. A traffic condition on a `serp`
  domain has nothing to evaluate — treat as "condition not applicable → does not drop",
  and say so in the UI rather than silently passing everything.
