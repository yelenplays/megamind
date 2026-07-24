# Schemas

Copy-ready templates live in [templates/](../templates/). This page defines
the fields.

## Registry (`.megamind/registry.json`)

```json
{
  "version": 1,
  "budgets": {
    "max_candidates": 5,
    "max_context_chars": 8000,
    "stale_days": 180,
    "micro_wiki_pages": 4,
    "top_level_topics": 3
  },
  "wikis": [
    {
      "name": "ProductWiki",
      "path": "ProductWiki",
      "privacy": "public-reference",
      "description": "What questions this wiki answers.",
      "keywords": ["product", "pricing"],
      "card": "ProductWiki/CARD.md",
      "digest": "ProductWiki/DIGEST.md",
      "index": "ProductWiki/INDEX.md"
    }
  ]
}
```

Rules: paths are root-relative (absolute paths and `..` are rejected), wiki
names are unique, `privacy` is one of `public-reference`, `company-private`,
`personal-local`, `digest-only`, `pointer-only`, and budgets are positive.
`card`, `digest`, and `index` are optional; routing degrades gracefully
without them.

## Frontmatter subset

Megamind reads and writes a deterministic YAML subset: `key: value` scalars
(strings, integers, `true`/`false`), flow lists (`[a, b]`), and block lists of
scalars. Nested maps are not supported; doctor reports files it cannot parse.
Obsidian reads this subset fine.

## Routing card (`CARD.md`)

Frontmatter: `megamind: routing-card`, `wiki`, `privacy`, `keywords`.
Body: what the wiki answers and does not answer. The card is the first rung of
the retrieval ladder, so its keywords are the strongest routing signal.

## Digest (`DIGEST.md`) and index (`INDEX.md`)

Digest: `megamind: digest` plus a body kept deliberately small; it is what
gets read when no exact page matches, and the only routable content of
`digest-only` wikis. Index: `megamind: index` plus one Markdown link per topic
page with a short hint; the router scores these entries to find exact pages.

## Topic page

```
---
title: Pricing model
type: decision            # fact|decision|hypothesis|procedure|example|guidance
status: active            # proposed|confirmed|active|shaky|rejected|superseded
created: 2026-01-10
updated: 2026-01-10
provenance:
  - where this came from
superseded_by: path.md    # required when status is superseded
---
```

## Proposal (`.megamind/proposals/<id>.md`)

Written by `capture`: `megamind: proposal`, `id` (content hash, equals the
filename), `type`, `status` (`proposed`, `applied`, or `rejected`), `source`,
`captured`, `suggested_destination`, `route_reasons`, and after an apply,
`applied_to` and `applied_on`.

## Top-level wiki proposal

See [templates/top-level-wiki-proposal.md](../templates/top-level-wiki-proposal.md).
It documents why the new wiki should exist and is only ever applied with
`--approve-new-wiki` after human approval.

## Audit records (`.megamind/audit/log.jsonl`)

One JSON object per line: `ts` (UTC ISO), `action` (`init`, `capture`,
`evolve-apply`, `evolve-apply-proposal-status`, `router-refresh`), and
action-specific fields such as `path`, `proposal_id`, `plan_id`, and `backup`.
Backups of every mutated file live in
`.megamind/audit/backups/<name>.<content-hash>.bak`.
