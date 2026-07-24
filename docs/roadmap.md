# Roadmap

Megamind grows in deliberate steps. Determinism and the proposal-first safety
model are non-negotiable at every stage.

## v0.1 (this release): deterministic foundation

- Retrieval ladder (card -> digest -> index -> pages) with context budgets
- Proposal-first capture with dedupe and provenance
- Approval-gated evolve with merge and supersession
- Review and doctor, synthetic examples vault, Agent Skill
- Known limitations: lexical English-only routing, no nested-map frontmatter,
  single-vault registry, manual registry editing for adopting existing wikis

## v0.2: adoption and ergonomics

- `megamind-axi adopt <path>`: assisted, non-destructive registration of existing
  wikis (card/digest/index generation proposals, never rewrites). Planned
  adapters: legacy non-frontmatter metadata blocks, surrogate digests (an
  existing overview/memory page acting as the digest), virtual digest
  fallback for wikis without one, hub/MOC-style index pages, and
  dirty-git-aware adoption (read-only routing works, writes wait for opt-in)
- Micro-wiki promotion as a first-class evolve action (generate the index,
  update the parent index, all through the normal plan/apply gate)
- Explicit ambiguity handling: when top candidates tie across wikis, return
  an ambiguity offer instead of loading, tuned jointly with the no-match
  floor (the two are the same dial)
- Multilingual stopword lists and configurable tokenization
- Benchmark suite (bench-mini): a frozen synthetic corpus with a tiered query
  set (exact, near, paraphrase, ambiguous, no-match, privacy), canary-string
  leak detection, context-cost accounting, and honest baselines (full-vault
  stuffing, grep-style search) with per-tier reporting and hard safety gates,
  runnable in CI without any hosted API

## v0.3: optional semantic adapters

- Pluggable adapter interface for embedding-based re-ranking on top of the
  deterministic candidate set (deterministic ladder stays the source of truth
  and the offline default; adapters only re-order and are always optional)
- Local-first adapters preferred; nothing in core ever requires a hosted API

## Later / undecided

- Watch mode for continuous capture suggestions
- Editor integrations beyond the Agent Skill
- Multi-vault federation with pointer-only cross-vault routing

Contributions toward any of these are welcome; open an issue first for
anything that changes the safety model. Anything that would make routing
non-reproducible or writes non-auditable is out of scope permanently.
