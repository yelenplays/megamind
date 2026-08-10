# Roadmap

Megamind grows in deliberate steps. Determinism and the proposal-first safety
model are non-negotiable at every stage.

## v0.1 (shipped): deterministic foundation

- Retrieval ladder (card -> digest -> index -> pages) with context budgets
- Proposal-first capture with dedupe and provenance
- Approval-gated evolve with merge and supersession
- Review and doctor, synthetic examples vault, Agent Skill
- Known limitations: lexical English-only routing, no nested-map frontmatter

## v0.2 (shipped): federation foundation

- Registry and wiki-card schema v2: purpose, scope boundaries, owners,
  sensitivity, local/cloud model access, routing mode, source policy,
  freshness expectations, examples, triggers, negative triggers, dependencies,
  context budgets, and catalog visibility, all with restrictive defaults; v1
  registries load unchanged and `migrate` upgrades them in place
- Canonical wiki roots (`init --wiki`): the Karpathy layout with an immutable
  `raw/` layer, a compiled `wiki/` layer, and an authoritative
  `.megamind/wiki-card.json`
- Non-destructive adoption (`adopt`): dry-run plan, approval-gated apply that
  only ever adds sidecar/scaffold files, surrogate index/digest detection, and
  content-verified rollback
- Fleet catalog (`catalog`): a deterministic, read-only, drift-checked
  projection over separate wiki roots with explicit broken, unreachable,
  stale, and redacted entries
- Model-access-aware preflight (`preflight`): catalog-level routing under a
  declared local/cloud model class with privacy filtering before any path is
  returned, definitive matched/ambiguous/no-match/unavailable/privacy-filtered
  states, and a content-hashed proof identity
- Repairs: route candidate paths are canonical root-relative paths, and an
  approved new top-level wiki is registered with its card/index skeletons in
  the same apply (doctor flags unregistered wiki-shaped directories)
- Known limitations: lexical card evidence only, no semantic reranking,
  preflight is host-invoked (no mandatory host enforcement yet)

## v0.3 (this release): retrieval and confidence

- Route confidence with explicit deterministic thresholds: a fixed 0.75
  reliance floor, a 0.25 no-match floor, and a 0.05 ambiguity band, applied
  in both `route` and `preflight`; confident matches load automatically,
  sub-floor matches offer choices without loading, and weak evidence stays a
  quiet no-match
- Claim and answer confidence rubrics (`megamind.confidence`, exposed as
  `megamind-axi assess claim|answer`): source quality by authority order,
  corroboration by independent origin only, freshness and lifecycle caps,
  contradictions frozen below the floor, `unknown` first-class and never
  fabricated; pinned by calibration fixtures
- Optional local semantic reranking (`--semantic`): a char-ngram backend
  behind a small protocol that only reorders already-authorized candidates,
  never widens access, never touches the network, and returns a typed
  disabled/ok/unavailable/error outcome with the lexical order intact on any
  failure
- Evidence packets carry provenance and freshness: per-candidate route
  confidence, lexical/card/semantic evidence, and `updated`/`age_days`/`stale`
  (computed only with `--today`), all inside the existing context budgets
- `route-result/v2` and `preflight-result/v2` documents (additive over v1)
- Known limitations: lexical English-only matching stays the default and the
  baseline; the semantic backend is similarity reranking, not embeddings;
  preflight is host-invoked (no mandatory host enforcement yet)

## Later / undecided

- Benchmark suite (bench-mini): a frozen synthetic corpus with a tiered query
  set (exact, near, paraphrase, ambiguous, no-match, privacy), canary-string
  leak detection, context-cost accounting, and honest baselines (full-vault
  stuffing, grep-style search) with per-tier reporting and hard safety gates,
  runnable in CI without any hosted API
- Pluggable local embedding adapters behind the `megamind.semantic` protocol
  (the deterministic ladder stays the source of truth and the offline
  default; adapters only re-order already-authorized candidates and are
  always optional)
- Micro-wiki promotion as a first-class evolve action (generate the index,
  update the parent index, all through the normal plan/apply gate)
- Multilingual stopword lists and configurable tokenization
- Durable gap records and nomination workflows
- Structured `wiki/log.md` event emission from Megamind commands
- Watch mode for continuous capture suggestions
- Editor integrations beyond the Agent Skill
- Host-side mandatory preflight enforcement and per-host promotion

Contributions toward any of these are welcome; open an issue first for
anything that changes the safety model. Anything that would make routing
non-reproducible or writes non-auditable is out of scope permanently.
