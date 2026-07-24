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

## Concepts

**Knowledge types**: `fact`, `decision`, `hypothesis`, `procedure`, `example`,
`guidance`. **Lifecycle states**: `proposed`, `confirmed`, `active`, `shaky`,
`rejected`, `superseded`, with dates and provenance in page frontmatter.

**Privacy classes** per wiki: `public-reference`, `company-private`,
`personal-local`, `digest-only` (only the digest is routable), and
`pointer-only` (only the path is ever returned, never content).

**Registry**: `.megamind/registry.json` lists your wikis, their routing cards,
digests, indexes, privacy classes, and context budgets. `ROUTER.md` is a
generated projection of it; `megamind-axi doctor` verifies they never drift.

## Safety guarantees

- All vault writes are contained to the vault root; path traversal and
  symlinks that escape the root are rejected. The only write outside a vault is
  `setup skill --dest`, which goes exactly where you point it.
- Writes are atomic; every mutation of an existing file leaves a backup under
  `.megamind/audit/backups/` and an audit record in `.megamind/audit/log.jsonl`.
- `evolve` is dry-run by default and requires the plan id from the dry run as
  an approval token, so what you apply is exactly what you reviewed. Creating
  a new top-level wiki additionally requires `--approve-new-wiki`.
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

## Honest limitations (v0.1)

Routing is lexical and deterministic: token overlap against routing cards,
digests, and indexes with fixed weights. That is a feature (reproducible,
explainable, offline) and a limitation (no synonym or semantic matching).
Optional semantic/embedding adapters are deferred to a later release; see
[docs/roadmap.md](docs/roadmap.md). English stopwords only for now.

## Documentation

- [AXI output contract](docs/axi.md) - schemas, formats, exit codes
- [Architecture](docs/architecture.md) - modules, ladders, scoring weights
- [Schemas and templates](docs/schemas.md) - registry, cards, pages, proposals
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
