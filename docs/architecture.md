# Architecture

Megamind is a small, dependency-free Python package behind the `megamind-axi`
executable. Every command runs locally, deterministically, and without network
access. Internally everything is plain typed Python objects; the CLI builds
one typed document per invocation and renders it as TOON or JSON only at the
output boundary (see [axi.md](axi.md)).

## Modules

| Module | Responsibility |
| --- | --- |
| `megamind.models` | Knowledge types, lifecycle states, privacy classes, frontmatter parse/serialize (deterministic YAML subset) |
| `megamind.fsops` | Path containment, atomic writes, backups, audit log. Every write goes through here |
| `megamind.registry` | `.megamind/registry.json` load/save/validate; generated `ROUTER.md` projection |
| `megamind.links` | Markdown link and Obsidian wikilink extraction and resolution |
| `megamind.routing` | The deterministic retrieval ladder |
| `megamind.capture` | Proposal-first capture with dedupe and provenance |
| `megamind.evolve` | Evolution plans, unified diffs, approval-gated apply |
| `megamind.review` | Read-only gardening report |
| `megamind.doctor` | Read-only integrity validation |
| `megamind.scaffold` | Non-destructive `init` |
| `megamind.toon` | TOON encoder; the output boundary renders typed dicts |
| `megamind.skillpack` | Packaged Agent Skill source for `setup skill` |
| `megamind.cli` | The `megamind-axi` AXI boundary: typed documents, TOON/JSON, exits |

```
        capture ─────► .megamind/proposals/ ────► evolve (dry-run diff)
           │                                          │  approval token
route ◄── registry ◄── ROUTER.md (generated)          ▼
   │        ▲                                    wiki pages (atomic write,
   ▼        │                                    backup + audit record)
 cards/digests/indexes/pages ◄─── review / doctor (read-only)
```

## The retrieval ladder

`route` scores each registered wiki by token overlap between the query and the
wiki's keywords (weight 3), name (2), description (1), and routing card body
(1). For the top wikis it adds digest matches (1) and scores domain index
entries (label 2, target path 1). Entries that match become exact page
candidates; if none match, the smallest useful artifact (digest, else index,
else card, else path pointer) is returned instead, so the ladder degrades
gracefully rather than guessing.

Candidates are sorted by score with alphabetical tie-breaking, then cut by
`max_candidates` and `max_context_chars` from the registry budgets. Privacy
classes shape the result: `digest-only` wikis never expose pages,
`pointer-only` wikis never expose content. Tokenization is lowercase word
extraction with an English stopword list and naive plural stripping. All
weights are constants in `routing.py`; changing them is a behavior change and
needs test updates.

## The evolution ladder

Knowledge earns structure: an idea enters as a proposal; an approved proposal
becomes or extends a topic page; a cluster of related pages (flagged by
`review`) becomes a micro-wiki folder with its own index; a micro-wiki that
keeps growing can be proposed as a top-level wiki, which always requires
explicit human approval (`--approve-new-wiki`).

`evolve` computes a plan (create, merge, or supersede) and hashes it into a
`plan_id`. Applying requires that exact id, so the applied change is exactly
the reviewed diff; if the vault changed in between, the id no longer matches
and the apply is refused. Merges embed an idempotency marker
(`<!-- megamind:proposal:<id> -->`), so re-planning an already-merged proposal
yields a no-op. Supersession marks the old page `superseded` with a
`superseded_by` pointer instead of deleting anything.

## Safety model

- `fsops.resolve_contained` resolves symlinks first and rejects any path that
  leaves the root: traversal (`..`), absolute paths, and symlinks pointing
  outside all fail closed.
- Writes are atomic (temp file + `os.replace`). Mutations of existing files
  first copy the old content to `.megamind/audit/backups/<name>.<hash>.bak`.
- Every mutating action appends a JSON line to `.megamind/audit/log.jsonl`.
- The registry stores only root-relative paths, so vaults stay portable and
  never leak machine-specific locations.
- `doctor` re-checks the invariants: containment, unsafe symlinks, router
  consistency, metadata validity, link integrity, proposal hygiene.

## Determinism

Commands avoid wall-clock dependence where it matters: proposal ids and plan
ids are content hashes, and `capture`, `evolve`, `review`, and the home view
accept `--today` for reproducible date handling in tests and benchmarks. The
only non-deterministic output is audit timestamps.
