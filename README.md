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
  wiki. Knowledge is promoted when it earns it, and a new wiki is never a side
  effect: promoting a micro-wiki needs explicit human approval, and the
  provisional creation path needs every qualification criterion stated up
  front.
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
megamind-axi evolve <proposal-id>                           # dry-run diff + plan id
megamind-axi evolve <proposal-id> --apply --plan-id <id>    # apply exactly that diff
megamind-axi evolve <proposal-id> --rollback --plan-id <id> # content-verified rollback
megamind-axi doctor             # integrity validation; exit 1 on errors
```

Every command accepts `--format json` and `--root <path>`. A query that
matches nothing is a definitive structured result, not silence:

```toon
schema_version: megamind/route-result/v2
query: quantum llama farming
matched: false
decision: no-match
...
help[2]:
  Run `megamind-axi config show` to see registered wikis and their keywords
  ...
```

Route output carries a `decision`: `load` at or above the 0.75 reliance
floor, `offer` below it or inside the ambiguity band (choices, nothing
auto-loads), `no-match` under the 0.25 floor. Confidence is necessary but not
sufficient: a wiki still marked `provisional` is never an authorized load, so
it leaves a `load` packet and degrades the whole result to `offer` when every
candidate that cleared the floor is provisional, however confident the match
is. The `governance[]` sidecar states `provisional`, `trusted`, and a
`disposition` of `load` or `offer` on every route, covering the emitted packet
plus the provisional candidates a load withheld, so nothing is offered only in
prose. `--semantic` opts into local
char-ngram reranking of the authorized candidates; `--today` makes freshness
(`updated`/`age_days`/`stale`) reproducible. `megamind-axi assess claim` and
`assess answer` score evidence against the same reliance floor.

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
  honored) and applies the route-confidence thresholds: a confident match
  (`>= 0.75`) reports `matched` with per-match confidence, freshness, and
  evidence; weaker matches report `ambiguous` (offers with no loadable paths)
  or a quiet `no-match`; `unavailable` and `privacy-filtered` are explicit.
  It emits a deterministic `preflight_id` so a host can prove the
  consultation happened. It never reads page content, never writes anything,
  and never calls a model or the network; whether and when to run it is the
  host's policy.
- **Explicit offer selection** closes the intentional no-load state after a
  user chooses one offered wiki. Record the complete original JSON packet with
  `--full`, then run `megamind-axi select-offer <Wiki> --request "<exact original
  request>" --preflight-result <packet.json> --model-class local|cloud` against
  the same root or estate. The additive selection result revalidates the whole
  preflight identity and current cards, authorizes only that one still-loadable
  offer, preserves its confidence and evidence, and exposes no broader access
  than the card already declared. A rephrased request is not a selection.
- **Different existing wiki** is a separate `select-existing` path, never an
  unoffered `select-offer` call. Megamind derives the eligible list from the
  complete current catalog and binds its one-time selection identity to the
  exact request, model class, owner/session, home, catalog, and freshness date.
  It preserves effective access and budget but sets `threshold_matched: false`;
  explicit choice grants consultation authority, not confidence or answerability.

## Governed gardening: gaps, research waves, provisional wikis

Megamind carries gardening work across sessions without becoming an agent
runtime. It stores the facts and emits the plans; the host keeps dispatch,
external research, model choice, quotas, scheduling, approvals, and cost:

```sh
megamind-axi gap create --wiki ProductWiki --topic "rate limits" --kind weak
megamind-axi research-wave <gap-id> --capacity-known \
  --applicable-quota 100 --reserve-quota 25    # a plan, never a dispatch
megamind-axi research-result --nomination-json <json> --result-json <json>
```

- **Durable gaps** (`.megamind/gaps.jsonl`) record missing, weak, stale, and
  contradictory coverage under a semantic identity, so the same gap reported
  twice coalesces instead of piling up. Priority, attempts, cooldowns,
  rejection, reopen, and supersession are kept, never overwritten: repeating a
  transition a gap already holds is an idempotent no-op, and anything that
  would rewrite a terminal fact refuses instead.
- **Research waves** are one hop: the direct gap plus bounded first-order
  topics, deeper topics deferred as nominations. Unknown capacity, a full
  three-worker fleet, a worker already on the target wiki, active captain
  work, or less than a 25 percent quota reserve is a typed pause or refusal.
  Megamind starts no worker and calls no quota tool.
- **Host research results** return by correlation id and are replay-safe.
  v1 results remain readable only as restrictive legacy nominations; v2
  acceptance requires typed host facts for derived origin identity, dated
  retrieval/publication, snapshot digests, rights/quotation posture, and a
  clean correction check. Megamind derives eligibility and writes no proposal
  for missing, unknown, malformed, contradictory, or retracted facts. Nothing
  is fetched, and `raw/` stays human-curated.
- **Governed evidence receipts** (`research`) record host-supplied discovery,
  artifacts, hash-bound quotations, claims, corrections, and contradictions
  under each wiki's restrictive research policy. Missing policy, unresolved
  spans, unknown gates, and retractions deny support; Megamind never fetches,
  dispatches, or turns a research packet into answer context. See
  [docs/axi.md](docs/axi.md) for the offline command contract.
- **Provisional wikis** (`provision-wiki`) are the qualified local creation
  path: accepted domain, repeat demand, multiple topics, overlap, scope and
  exclusions, owner, source policy, privacy and model access, seed topics, and
  maintenance must all pass. Planning is side-effect-free and hands back a
  content-bound `plan_id`; applying it is a write-ahead transaction whose
  manifest and backups land before the first file changes, so an interrupted
  apply resumes or rolls back completely and a replay is a verified no-op. The
  wiki is registered restrictively as `provisional` - surfaced and offered,
  never auto-loaded - until confidence coverage and an evaluation pass clear
  it. No remote repository, account, publication, or spend is ever involved.

[docs/schemas.md](docs/schemas.md) owns the record formats and
[docs/architecture.md](docs/architecture.md) the design.

## Evaluation: a frozen benchmark and blinded three-arm runs

Megamind measures itself offline and never runs the model under test. Both
surfaces are planning and arithmetic only: the host executes, Megamind freezes
the inputs, validates, scores, and records.

```sh
megamind-axi bench run --fixtures evals/fixtures/release-mini \
  --queries evals/queries.jsonl --thresholds evals/thresholds.toml --repeat
megamind-axi experiment keygen --out blinding.key   # the private blinding key
megamind-axi experiment plan --tasks evals/experiment-tasks-v1.json ...
```

- **Release benchmark** (`bench run|check`) drives the real `preflight` and
  `route` commands over a frozen synthetic fixture, reporting exact, near,
  paraphrase, ambiguous, no-match, and privacy tiers separately, plus
  authorized-context accounting, canary and model-access safety counts,
  repeatability, and honest local grep/full-vault baselines. Thresholds are
  preregistered against the corpus, query-set, and task-set digests they gate,
  so a post-hoc change to them is visible instead of silent.
- **Three-arm value evaluation** (`experiment keygen|plan|validate|score|record`)
  runs identical pre-authored tasks under no-wiki, current-wiki, and
  updated-wiki conditions with isolated snapshots, sessions, caches, and
  outputs. Conditions sit behind blind labels, the host executes the arms and
  returns opaque-label outputs, and scoring seals the blind scores before it
  unblinds. The result is `promoted`, `rollback-required`, or `unsettled`.

[docs/evaluation.md](docs/evaluation.md) owns the contract. The evidence is
about routing, context, and safety; neither surface claims model quality.

## Governed host rollout: evidence, not deployment

`megamind-axi rollout` promotes one wiki surface to one host only after current
card access, matching and quiet no-match preflights, host enforcement evidence,
a passing value evaluation, doctor health, separate governance/access approval
references, and the incremental privacy-order proof chain all pass. Dry run
returns a content-bound plan; apply writes a typed local promotion proof.

```sh
megamind-axi rollout promote --state-root <local-state> --estate <estate> \
  --wiki-root <root> --wiki <name> --host-id <opaque-id> \
  --model-class cloud --host-evidence <host.json> \
  --preflight-evidence <matched.json> --no-match-evidence <no-match.json> \
  --evaluation-evidence <score.json> --governance-approval <ref> \
  --access-approval <ref> --sequence 0 --today 2026-10-01
```

Promotion and rollback are write-ahead, idempotent, interruption-resumable, and
foreign-content-safe. Health fails closed on card, access, trust, or doctor
drift. Provisional, unapproved, pointer, `none`, or failed-evaluation wikis stay
unloadable. State is outside every wiki and contains no raw request or root.
Megamind never edits host configuration and has no provider, worker, model,
network, publication, repository, account, collaborator, merge, or billing
adapter. See [docs/rollout.md](docs/rollout.md).

The reusable machinery is shipped, but no real host/wiki binding is promoted
by this release: the restricted pilot's value evaluation required rollback, so
its approved access surfaces remain unchanged and candidate knowledge remains
untrusted.

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
  symlinks that escape the root are rejected. The only writes outside a vault
  are `setup skill --dest`, the evaluation artifacts, and the rollout state
  directory, which go exactly where you point them; a destination that resolves
  inside any vault - or inside an evaluated root, or inside the estate or wiki
  root being promoted - is refused before anything is written.
- Writes are atomic; every mutation of an existing file leaves a backup under
  `.megamind/audit/backups/` and an audit record in `.megamind/audit/log.jsonl`.
- `evolve` is dry-run by default and requires the plan id from the dry run as
  an approval token, so what you apply is exactly what you reviewed. Apply uses
  a durable write-ahead transaction. `--rollback --plan-id` restores the exact
  pre-change compiled tree only while every target still matches that
  transaction, retains the proposal and append-only evidence, and refuses
  foreign content. An evolved page under an existing wiki's declared index is
  linked from that index in the same reviewed transaction, without widening
  the card's routing scope. Creating a new top-level wiki additionally requires
  `--approve-new-wiki`; the apply then registers the wiki with its card and
  index skeletons in the same step. Canonical wiki roots route, capture,
  review, and evolve directly from their authoritative card, scoped to the
  compiled page tree that card's index declares: `evolve` targets it by default
  and refuses the immutable `raw/` layer, and `review` never reports raw pages.
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
  zero-network, and refuses a destination that resolves inside a vault;
  uninstall by deleting the installed directory.

## Honest limitations

Routing is lexical and deterministic by default: token overlap against
routing cards, digests, and indexes with fixed weights. That is a feature
(reproducible, explainable, offline) and a limitation (no synonym matching in
the baseline). `--semantic` adds optional local char-ngram reranking of
already-authorized candidates - still fully offline, still deterministic, and
only ever reordering what the lexical ladder surfaced; embedding-based local
adapters come later behind the same protocol, and cloud embeddings are out of
scope permanently. Confidence scores are calibrated rubric outputs, not truth
guarantees: they never exceed what source quality, corroboration, freshness,
and contradiction state justify, and `unknown` stays unknown. English
stopwords only for now. Preflight routes at the catalog level and never loads
page content from other roots. The rollout proof can verify a host attestation,
but running preflight and consuming or disarming the proof remain host-side
policies that Megamind cannot enforce from inside its local process.

## Documentation

- [AXI output contract](docs/axi.md) - schemas, formats, exit codes
- [Architecture](docs/architecture.md) - modules, ladders, scoring weights
- [Schemas and templates](docs/schemas.md) - registry, cards, pages, proposals
- [Evaluation contract](docs/evaluation.md) - release benchmark, three-arm runs
- [Host rollout contract](docs/rollout.md) - proofs, privacy order, health, rollback
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
