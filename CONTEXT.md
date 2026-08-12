# Context: Megamind domain language

The settled vocabulary for the federation foundation, the retrieval and
confidence layer above it, the governed gardening layer above that, and the
evaluation surfaces that measure them. Each term means exactly this anywhere
it appears in the project; the avoid lines name the common misreadings. This
file covers only what the current release implements.

**Knowledge engine**:
Megamind's role: the deterministic layer that owns wiki cataloging, routing,
proposals, and validation, without commissioning or scheduling work.
_Avoid_: agent controller, research daemon

**AI host**:
The agent environment (any Agent Skills-compatible host or orchestrator) that
invokes Megamind and owns orchestration, worker dispatch, budgets, and
approval authority.
_Avoid_: Megamind runtime

**Registry vault**:
One root holding many wikis as subdirectories, with `.megamind/registry.json`
as the multi-wiki card authority.
_Avoid_: fleet, mega-root

**Canonical wiki root**:
A single top-level wiki in the canonical layout: `AGENTS.md`, immutable
`raw/`, compiled `wiki/`, and `.megamind/` state with the authoritative card.
_Avoid_: subdirectory wiki, vault entry

**Wiki card**:
The authoritative per-wiki record of scope, exclusions, ownership, privacy,
model access, source policy, freshness, and routing signals; the registry
entry or `wiki-card.json` the catalog projects from.
_Avoid_: catalog row, hand-maintained router copy

**Wiki catalog**:
The generated projection of all eligible wiki cards that lets a host discover
and route across separate roots without combining their content or governance.
_Avoid_: mega-vault, source of truth

**Raw source**:
A human-curated, immutable input document or asset under `raw/` that the AI
may read and cite but never modify.
_Avoid_: wiki page, proposal

**Compiled wiki**:
The AI-maintained, interlinked Markdown synthesis under `wiki/`, navigable
through `index.md` and the append-only `log.md`.
_Avoid_: raw corpus, retrieval index

**Wiki schema**:
The `AGENTS.md` contract that defines a wiki's domain-specific page
conventions and its ingestion, query, maintenance, and source-handling
workflows.
_Avoid_: fleet catalog, routing card

**Model access**:
A per-wiki policy declaring whether a local or cloud model context may receive
full content, only an approved digest, or no content; unset or broken
classifications always resolve restrictively.
_Avoid_: privacy label, host preference

**Substantive request**:
A request that asks an AI to research, analyze, decide, write, design, plan,
or perform software work and therefore warrants wiki preflight.
_Avoid_: every message, every turn

**Wiki preflight**:
The host-side act of consulting the wiki catalog before answering or starting
a substantive request, with a proof identity showing the consultation
happened. Megamind supplies the typed result; running it is the host's choice.
_Avoid_: optional skill trigger, full-catalog prompt injection

**Route confidence**:
The deterministic score in [0, 1] for how well a wiki or artifact matches a
request, blended from the strongest per-token routing signal and token
coverage. Separate from claim confidence (evidence support for one claim) and
answer confidence (capped at the weakest relied-upon claim).
_Avoid_: probability of truth, model self-assessment

**Reliance floor**:
The fixed 0.75 threshold: at or above it a route may load automatically, a
claim may become active factual knowledge, an answer may be delivered without
a warning. Below it evidence stays an offer, hypothesis, or raw material; an
`unknown` confidence never meets it.
_Avoid_: tunable preference, guarantee of correctness

**Semantic rerank**:
The optional local similarity pass (`--semantic`) that reorders only the
candidates the lexical baseline already surfaced and access filtering already
authorized, with a typed disabled/ok/unavailable/error outcome.
_Avoid_: retrieval layer, access decision, cloud embeddings

**Knowledge gap**:
A durable record that one wiki's coverage of one topic is missing, weak,
stale, or contradictory, identified by normalized wiki, topic, and kind rather
than by page text, and carrying its own lifecycle.
_Avoid_: ticket, TODO page

**Research wave**:
One deterministic hop of planned work for a gap: the direct gap plus bounded
first-order topics, with deeper topics deferred. Megamind plans it from
host-supplied capacity facts and never dispatches it.
_Avoid_: job queue, crawl, scheduled run

**Nomination**:
A typed, correlation-stable suggestion that a host may research one gap topic.
Never a dispatch, a worker, or a budget commitment.
_Avoid_: task assignment, work order

**Provisional wiki**:
A locally created wiki that passed every qualification criterion but is not
trusted active knowledge yet: it is surfaced and may be offered, never an
authorized load, until confidence coverage and an evaluation pass clear it.
_Avoid_: draft wiki, private wiki

**Release benchmark**:
The frozen offline measurement of the shipped `route` and `preflight`
interfaces over a synthetic corpus: routing tiers, authorized context, and
safety counts against preregistered thresholds. Evidence about retrieval,
never a claim about model quality.
_Avoid_: model benchmark, leaderboard score

**Arm**:
One condition of a three-arm evaluation - no-wiki, current-wiki, or
updated-wiki - with its own isolated snapshot, session, cache, and output
root. The host executes it; Megamind only freezes, validates, and scores it.
_Avoid_: variant rollout, experiment run

**Blind label**:
The opaque arm identity every artifact outside the host's unblinding map uses
in place of a condition. Conditions are assigned to labels by a keyed
permutation under the host's private blinding key.
_Avoid_: condition name, random tag

**Promotion outcome**:
The typed verdict of a scored evaluation: `promoted` only for improved target
outcomes with no material adjacent regression, preserved provenance, and zero
new safety violations; otherwise `rollback-required`, or `unsettled` when the
evidence is incomplete. Evidence, not authorization to publish or dispatch.
_Avoid_: deployment, approval, release gate

**Host/wiki binding**:
One host's authorization to consume one wiki's exact card-derived access
surface under one model class, represented by a local typed proof after host,
preflight, evaluation, health, governance, access, and privacy-order checks.
_Avoid_: provider integration, fleet-wide enablement

**Promotion proof**:
The content-bound, loadable local artifact for a host/wiki binding. It records
safe evidence identities and cannot widen card access or change host
configuration; health drift or rollback makes the binding unloadable while the
proof remains retained.
_Avoid_: deployment credential, remote feature flag

**Privacy-order chain**:
The sequence-complete prior promotion proofs for one host/model class, ordered
by nondecreasing restrictive privacy rank so rollout proceeds one authorized
binding at a time.
_Avoid_: claim that every estate wiki was nominated, automatic governance
