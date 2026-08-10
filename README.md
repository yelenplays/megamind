# Megamind

Provider-neutral, agent-native knowledge gardener for evolving Markdown wikis.

[![CI](https://github.com/yelenplays/megamind/actions/workflows/ci.yml/badge.svg)](https://github.com/yelenplays/megamind/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)

Megamind turns conversations and software work into proposed, structured
knowledge; routes future questions to the smallest useful context; evolves or
supersedes existing pages instead of duplicating them; and validates the
result. It is local-first and works on plain Markdown and Obsidian-style
vaults. No embeddings, no hosted model, no vector database required.

Its executable is **`megamind-axi`**, a first-class
[AXI](https://axi.md/) (agent experience interface): every invocation prints
exactly one typed TOON document (or the same object as JSON), with stable
schemas, precomputed aggregates, definitive empty states, structured errors,
and executable next steps. Agents drive it directly; humans read the same
document.

## Why

Knowledge bases rot in predictable ways: notes pile up uncategorized,
the same fact gets written three times, outdated pages keep getting cited,
and both humans and AI agents end up stuffing entire vaults into context
because nothing tells them where to look. Megamind addresses this with two
ladders and one rule:

- **Retrieval ladder**: routing card -> digest -> domain index -> exact pages,
  with strict context budgets. A query returns the few files worth opening and
  the reasons why, not a wall of content.
- **Evolution ladder**: idea -> topic page -> micro-wiki folder -> top-level
  wiki. Knowledge is promoted when it earns it. Creating a new top-level wiki
  always requires explicit human approval.
- **Proposal-first rule**: automatic capture only ever writes a local proposal
  draft. Permanent wiki changes require review and an explicit approval token.

## Install

```sh
pip install git+https://github.com/yelenplays/megamind
```

The PyPI distribution is not published yet. From a checkout, run
`pip install .`. Uninstall with `pip uninstall megamind-axi`. Megamind stores
its state inside your vault under `.megamind/`; deleting that directory removes
everything Megamind ever added (your wiki pages stay untouched unless you
approved changes to them).

## Quickstart

```sh
megamind-axi init my-vault      # non-destructive; synthetic starter structure
cd my-vault

megamind-axi                    # home: wikis, proposal/review/doctor aggregates
megamind-axi route "how does the release process work"
megamind-axi capture --text "Pricing moves to two tiers next quarter." --type decision
megamind-axi review             # proposals, duplicates, stale pages, dead links
megamind-axi evolve <proposal-id>                          # dry-run diff + plan id
megamind-axi evolve <proposal-id> --apply --plan-id <id>   # apply exactly that diff
megamind-axi doctor             # integrity validation; exit 1 on errors
```

Every command accepts `--format json` and `--root <path>`. A query that
matches nothing is a definitive structured result, not silence:

```toon
schema_version: megamind/route-result/v1
query: quantum llama farming
matched: false
candidates[0]:
...
help[2]:
  Run `megamind-axi config show` to see registered wikis and their keywords
  ...
```

The full output contract (schemas, truncation, exit codes, error codes) is in
[docs/axi.md](docs/axi.md). A ready-made demo vault lives in
[`examples/vault/`](examples/vault/).

## Federation: many wikis, one catalog

Wikis stay separate roots with their own owners, governance, and privacy.
Megamind projects one generated, read-only fleet catalog from their cards and
routes substantive requests across it under the host's declared model class:

```sh
megamind-axi catalog --estate ~/Wikis --emit-projection   # fleet view
megamind-axi catalog --estate ~/Wikis --check-projection ~/Wikis/CATALOG.md
megamind-axi preflight "how do we price cleanup offers" --estate ~/Wikis --model-class cloud
```

- **Wiki cards** (registry schema v2 or a standalone `.megamind/wiki-card.json`)
  declare purpose, scope and exclusions, owners, sensitivity, local/cloud
  model access, source policy, freshness expectations, routing triggers and
  negative triggers, dependencies, budgets, and catalog visibility. Unset or
  broken classifications default restrictively; v1 registries load unchanged
  and `megamind-axi migrate` upgrades them in place.
- **Canonical wiki roots** follow the Karpathy layout: `AGENTS.md`, an
  immutable human-curated `raw/`, the AI-maintained compiled `wiki/` with
  `index.md` and `log.md`, and `.megamind/` state. `megamind-axi init <path>
  --wiki <Name>` scaffolds a new one; `megamind-axi adopt <path>` onboards an
  existing directory non-destructively (dry-run plan, approval-gated apply,
  content-verified rollback; existing pages are never moved or rewritten).
- **Preflight** is catalog-level only: it filters by model access before any
  path is returned (`full`, `digest-only`, `none`, and pointer modes are all
  honored), reports `matched`, `ambiguous`, `no-match`, `unavailable`, or
  `privacy-filtered`, gives exact per-wiki follow-up commands, and emits a
  deterministic `preflight_id` so a host can prove the consultation happened.
  It never reads page content, never writes anything, and never calls a model
  or the network; whether and when to run it is the host's policy.

## Concepts

**Knowledge types**: `fact`, `decision`, `hypothesis`, `procedure`, `example`,
`guidance`. **Lifecycle states**: `proposed`, `confirmed`, `active`, `shaky`,
`rejected`, `superseded`, with dates and provenance in page frontmatter.

**Privacy classes** per wiki: `public-reference`, `company-private`,
`personal-local`, `digest-only` (only the digest is routable), and
`pointer-only` (only the path is ever returned, never content).

**Registry**: `.megamind/registry.json` (schema v2) lists your wikis, their
routing cards, digests, indexes, privacy and access classes, and context
budgets. `ROUTER.md` is a generated projection of it; `megamind-axi doctor`
verifies they never drift. Standalone canonical wikis carry the same card
fields in `.megamind/wiki-card.json` instead. See
[CONTEXT.md](CONTEXT.md) for the domain vocabulary.

## Safety guarantees

- All vault writes are contained to the vault root; path traversal and
  symlinks that escape the root are rejected. The only write outside a vault is
  `setup skill --dest`, which goes exactly where you point it.
- Writes are atomic; every mutation of an existing file leaves a backup under
  `.megamind/audit/backups/` and an audit record in `.megamind/audit/log.jsonl`.
- `evolve` is dry-run by default and requires the plan id from the dry run as
  an approval token, so what you apply is exactly what you reviewed. Creating
  a new top-level wiki additionally requires `--approve-new-wiki`; the apply
  then registers the wiki with its card and index skeletons in the same step.
- `adopt` follows the same gate for onboarding an existing wiki directory and
  only ever adds files; `adopt --rollback` removes exactly what it created.
- Access policy is enforced by the card, not the host: unknown or contradictory
  classifications resolve to the most restrictive value.
- Re-runs are idempotent: identical captures dedupe, applied plans become
  no-ops, and both are reported as structured successes.
- No command ever touches the network.

## For agents

- `megamind-axi` with no arguments is a content-first local home: identity,
  root, wikis, proposal/review/doctor aggregates, next actions.
- Every document carries `help[]` with ready-to-run next commands (the exact
  apply command after a dry run, for example).
- `megamind-axi setup skill --dest <skills-dir>` installs the bundled
  [Agent Skills](https://agentskills.io)-compatible skill (also checked in at
  [`skills/megamind/`](skills/megamind/)). Setup is explicit, local, and
  zero-network; uninstall by deleting the installed directory.

## Honest limitations

Routing is lexical and deterministic: token overlap against routing cards,
digests, and indexes with fixed weights. That is a feature (reproducible,
explainable, offline) and a limitation (no synonym or semantic matching).
Optional local semantic adapters are deferred to a later release; see
[docs/roadmap.md](docs/roadmap.md). English stopwords only for now. Preflight
routes at the catalog level and never loads page content from other roots;
running it before every substantive request is a host-side policy that no
host integration enforces yet.

## Documentation

- [AXI output contract](docs/axi.md) - schemas, formats, exit codes
- [Architecture](docs/architecture.md) - modules, ladders, scoring weights
- [Schemas and templates](docs/schemas.md) - registry, cards, pages, proposals
- [Domain vocabulary](CONTEXT.md) - the settled domain terms
- [Decision records](docs/adr/) - the hard-to-reverse tradeoffs
- [Roadmap](docs/roadmap.md)
- [Contributing](CONTRIBUTING.md) - development setup, tests, style
- [Security policy](SECURITY.md)

## Disclaimer

Megamind is an independent open-source project and is not affiliated with,
endorsed by, or connected to DreamWorks Animation or any of its franchises.
The name is used solely as this software project's name; the project uses no
franchise artwork, characters, typography, or other franchise material.

## License

[MIT](LICENSE)
