# Roadmap

Megamind grows in deliberate steps. Determinism and the proposal-first safety
model are non-negotiable at every stage.

## v0.1 (shipped): deterministic foundation

- Retrieval ladder (card -> digest -> index -> pages) with context budgets
- Proposal-first capture with dedupe and provenance
- Approval-gated evolve with merge and supersession
- Review and doctor, synthetic examples vault, Agent Skill
- Known limitations: lexical English-only routing, no nested-map frontmatter

## v0.2 (this release): federation foundation

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

## v0.3: retrieval depth and ergonomics

- Ambiguity band and no-match floor tuning for the per-wiki ladder, evaluated
  against the benchmark suite
- Micro-wiki promotion as a first-class evolve action (generate the index,
  update the parent index, all through the normal plan/apply gate)
- Multilingual stopword lists and configurable tokenization
- Benchmark suite (bench-mini): a frozen synthetic corpus with a tiered query
  set (exact, near, paraphrase, ambiguous, no-match, privacy), canary-string
  leak detection, context-cost accounting, and honest baselines (full-vault
  stuffing, grep-style search) with per-tier reporting and hard safety gates,
  runnable in CI without any hosted API
- Pluggable adapter interface for local embedding-based re-ranking on top of
  the deterministic candidate set (the deterministic ladder stays the source
  of truth and the offline default; adapters only re-order already-authorized
  candidates and are always optional)

## Later / undecided

- Durable gap records and nomination workflows
- Structured `wiki/log.md` event emission from Megamind commands
- Watch mode for continuous capture suggestions
- Editor integrations beyond the Agent Skill
- Host-side mandatory preflight enforcement and per-host promotion

Contributions toward any of these are welcome; open an issue first for
anything that changes the safety model. Anything that would make routing
non-reproducible or writes non-auditable is out of scope permanently.
