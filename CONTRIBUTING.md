# Contributing to Megamind

Thanks for considering a contribution. Megamind values determinism, safety,
and honest claims over feature count.

## Development setup

```sh
git clone https://github.com/yelenplays/megamind
cd megamind
python -m venv .venv && source .venv/bin/activate
pip install -e . pytest ruff mypy
```

## Checks

All three must pass before a pull request is reviewed; CI runs the same
commands:

```sh
pytest          # deterministic test suite
ruff check src tests && ruff format --check src tests
mypy            # strict, configured in pyproject.toml
```

## Ground rules

- **Determinism**: same vault plus same input must always produce the same
  output. No wall-clock dependence in behavior (use `--today` plumbing), no
  randomness, no network in core commands.
- **Safety model**: every write goes through `megamind.fsops` (containment,
  atomic write, backup, audit). Dry-run stays the default for anything that
  mutates a wiki; approval tokens gate applies. Do not weaken these.
- **Zero runtime dependencies**: the core package uses the standard library
  only. Optional integrations belong behind extras (see the roadmap).
- **Tests first**: behavior changes come with tests; bug fixes come with a
  test that fails before the fix. The synthetic examples vault is pinned by
  tests; regenerate it deliberately, never let it drift.
- **Synthetic content only**: examples, tests, fixtures, and docs must never
  contain real personal data, private paths, or company information.
- **Style**: ruff-formatted, 100-column lines, typed throughout. Prefer plain
  dashes over em dashes in prose.

## Pull requests

Keep them focused. Describe what changed and why, note any behavior change in
routing scores or file formats explicitly, and update the docs the change
touches (`docs/`, `skills/`, templates). The packaged skill under
`src/megamind/skill/` is the source of truth; keep `skills/megamind/` an
exact copy (a test enforces this).

New or changed `megamind-axi` surface must honor the AXI contract in
[docs/axi.md](docs/axi.md): one typed document on stdout, TOON and JSON as
renderings of the same object (golden-tested), stable `schema_version`,
aggregates, definitive empty states, truncation with `--full`, `help[]`, and
0/1/2 exit codes.

For product-shaping ideas (new commands, format changes, semantic adapters),
open an issue first so design can happen before code.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do not open public issues for
vulnerabilities.
