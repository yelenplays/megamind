# Concepts

## Knowledge types

`fact` (verifiable statement), `decision` (choice plus rationale),
`hypothesis` (unverified belief), `procedure` (how-to), `example`
(concrete instance), `guidance` (source-backed recommendation).

## Lifecycle states

`proposed` -> `confirmed` or `active` once reviewed; `shaky` when doubt
appears; `rejected` when disproven; `superseded` when replaced (page keeps its
history and points to the successor via `superseded_by`). Pages carry
`created`, `updated`, and `provenance` frontmatter.

## Privacy classes (per wiki, in the registry)

- `public-reference`: content may be routed and quoted anywhere.
- `company-private`: routable locally; keep content out of public artifacts.
- `personal-local`: routable locally; never leaves the machine.
- `digest-only`: only the wiki's digest is routed, never exact pages.
- `pointer-only`: only the wiki's path is returned, never any content.

## Model access (schema v2, per wiki card)

Each card declares what a `local` or `cloud` model context may receive:
`full`, `digest-only`, or `none`. Unset axes derive from the privacy class;
unknown, unclassified, or broken classifications always resolve restrictively
(cloud `none`, personal wikis never broader than cloud `digest-only`), and
explicit values that contradict a ceiling are clamped down. `routing_mode:
pointer` returns location metadata and zero content. `catalog_visibility`
(`full`, `redacted`, `hidden`) controls how much of a card appears in the
generated fleet catalog. No host or routing layer can widen these decisions.

## The two ladders

Retrieval: routing card -> digest -> domain index -> exact pages, under
`max_candidates` and `max_context_chars` budgets from the registry.

Evolution: idea (proposal) -> topic page -> micro-wiki folder -> top-level
wiki. Each promotion needs more evidence; the last one always needs explicit
human approval (`--approve-new-wiki`).

## Confidence

Route, claim, and answer confidence are separate scores in [0, 1] against the
fixed 0.75 reliance floor. Route confidence blends the strongest per-token
routing signal with query coverage; the thresholds are 0.75 (load), 0.25
(offer below, no-match under), and a 0.05 ambiguity band. Claim confidence
weighs eligible source quality (primary, synthesis, hypothesis, prior),
independent corroboration (one origin counts once), freshness, lifecycle, and
contradictions. Answer confidence is the weakest relied-upon claim.
`unknown` is first-class: no evidence, no number.

## Determinism

`megamind-axi route` is lexical token overlap with fixed weights and stable tie-breaking.
Same vault plus same query always gives the same answer. No embeddings, no
network, no model calls; `--semantic` adds a deterministic local char-ngram
rerank that only reorders already-authorized candidates and reports a typed
status, so the lexical baseline is always available.
