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

## The two ladders

Retrieval: routing card -> digest -> domain index -> exact pages, under
`max_candidates` and `max_context_chars` budgets from the registry.

Evolution: idea (proposal) -> topic page -> micro-wiki folder -> top-level
wiki. Each promotion needs more evidence; the last one always needs explicit
human approval (`--approve-new-wiki`).

## Determinism

`megamind-axi route` is lexical token overlap with fixed weights and stable tie-breaking.
Same vault plus same query always gives the same answer. No embeddings, no
network, no model calls; that also means no synonym matching in v0.1.
