# Project agent memory

Megamind: provider-neutral, agent-native knowledge gardener for Markdown
wikis. Deterministic retrieval ladder, proposal-first evolution, local-only.
The executable boundary is the `megamind-axi` AXI (TOON-default typed
documents). Authoritative docs: `README.md` (product), `docs/axi.md` (output
contract), `docs/architecture.md` (design and scoring weights),
`docs/schemas.md` (file formats), `docs/roadmap.md` (scope).

## Working here

- Checks that must stay green: `pytest`, `ruff check src tests`,
  `ruff format --check src tests`, `mypy` (strict; config in `pyproject.toml`).
  CI (`.github/workflows/ci.yml`) runs these on Python 3.10-3.14 plus an
  installed-wheel smoke test executed outside the source tree.
- Core is stdlib-only by design; do not add runtime dependencies.
- The AXI contract in `docs/axi.md` is product contract: exactly one typed
  document on stdout, TOON and JSON as renderings of the same object
  (golden-tested in `tests/test_cli.py`), stable `schema_version`, definitive
  empty states, truncation with `--full`, `help[]`, exit codes 0/1/2.
  Diagnostics go to stderr only.
- Every filesystem write must go through `megamind.fsops` (containment,
  atomic write, backup, audit). Dry-run defaults and approval tokens
  (`plan_id`, `--approve-new-wiki`) are product contract, not polish.
- Determinism is product contract: no wall-clock behavior (use the `--today`
  plumbing), no randomness, no network in any command. Routing weights are
  constants in `src/megamind/routing.py`; changing them changes behavior and
  needs test updates.
- The packaged skill under `src/megamind/skill/` is the source of truth;
  `skills/megamind/` must be an exact copy (`tests/test_skillpack.py`
  enforces it).
- `examples/vault/` is generated with Megamind's own APIs and pinned by
  `tests/test_examples.py`; regenerate it deliberately, never hand-drift it.
- All repo content (tests, examples, docs, fixtures) must stay fully
  synthetic: no real personal/company data, no private machine paths.
- Prose style: plain dashes, no em dashes.
- The project is independent and unaffiliated with DreamWorks Animation; keep
  the README disclaimer intact and never add franchise references.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in
this project. Do not repeat what the codebase already shows; point to the
authoritative file or command instead. Prefer rewriting or pruning existing
entries over appending new ones. When updating this file, preserve this bar
for all agents and keep entries concise.
