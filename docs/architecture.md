# Architecture

Megamind is a small, dependency-free Python package behind the `megamind-axi`
executable. Every command runs locally, deterministically, and without network
access. Internally everything is plain typed Python objects; the CLI builds
one typed document per invocation and renders it as TOON or JSON only at the
output boundary (see [axi.md](axi.md)).

## Modules

| Module | Responsibility |
| --- | --- |
| `megamind.models` | Knowledge types, lifecycle states, privacy and access classes, frontmatter parse/serialize (deterministic YAML subset) |
| `megamind.fsops` | Path containment, atomic writes, backups, audit log. Every write goes through here |
| `megamind.registry` | `.megamind/registry.json` (schema v1/v2) load/save/validate/migrate; generated `ROUTER.md` projection |
| `megamind.access` | Model-access policy: derives and clamps the binding local/cloud access, routing mode, and catalog visibility for every card |
| `megamind.card` | The standalone `.megamind/wiki-card.json` of a canonical wiki root |
| `megamind.links` | Markdown link and Obsidian wikilink extraction and resolution |
| `megamind.routing` | The deterministic retrieval ladder |
| `megamind.capture` | Proposal-first capture with dedupe and provenance |
| `megamind.evolve` | Evolution plans, unified diffs, approval-gated apply, new-wiki registration |
| `megamind.review` | Read-only gardening report |
| `megamind.doctor` | Read-only integrity validation |
| `megamind.scaffold` | Non-destructive `init`: registry vaults and canonical wiki roots |
| `megamind.adopt` | Non-destructive adoption of existing wiki directories, with rollback |
| `megamind.catalog` | The generated read-only fleet catalog and its drift-checked projection |
| `megamind.preflight` | Catalog-level, model-access-aware routing for substantive requests |
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
`max_candidates` and `max_context_chars` from the registry budgets. Candidate
paths are always canonical root-relative paths: index links that climb out of
their directory with `..` resolve to the same page and are emitted in the
resolved form. Every
artifact whose content is handed back counts against the budget in characters,
pages and digests alike, so the reported `context_chars` never exceeds
`max_context_chars`; each artifact dropped for budget adds a
`context budget reached: omitted <path>` note. The highest scoring candidate is
always returned even when it alone exceeds the budget, so a real match never
degrades into a silent empty result. Privacy classes shape the result:
`digest-only` wikis never expose pages, `pointer-only` wikis never expose
content and therefore cost nothing against the budget. Tokenization is
lowercase word extraction with an English stopword list and naive plural
stripping. All weights are constants in `routing.py`; changing them is a
behavior change and needs test updates.

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
`superseded_by` pointer instead of deleting anything. An approved new
top-level wiki is registered in the same apply: the registry entry, the card
and index skeletons, and the regenerated router are plan changes covered by
the `plan_id`, so the wiki is immediately visible to route, review, catalog,
and doctor (which also warns about wiki-shaped directories that were never
registered). New wikis start with the restrictive company-private posture
until an owner sets an explicit access policy.

## Two root shapes, one card schema

A **registry vault** holds many wikis as subdirectories of one root; their
cards are the wiki entries of `.megamind/registry.json` (schema v2; v1 loads
with restrictive derived defaults). A **canonical wiki root** is a single wiki
in the Karpathy layout: human-curated immutable `raw/`, the AI-maintained
compiled `wiki/` layer with its content-oriented `index.md` and append-only
`log.md`, and `.megamind/` state headed by the authoritative
`wiki-card.json`. Both shapes validate against the same v2 field set; `init
--wiki` scaffolds new canonical roots and `adopt` onboards existing ones
without touching their content.

## Access policy

Every card resolves to an effective posture in `megamind.access`: sensitivity
(who may ever see it), per-axis model access (what a local or cloud context
may receive: `full`, `digest-only`, or `none`), routing mode, and catalog
visibility. Unset axes derive from the privacy class; unknown, unclassified,
broken, or unmigrated classifications derive restrictively (cloud `none`).
Explicit values that contradict a sensitivity or privacy ceiling are clamped
down and reported as doctor `access` errors; the restrictive value always
wins, and no host or later routing layer can widen the decision. Ceilings key
on the derived sensitivity, never the raw field, so leaving `sensitivity`
unset is never a way to escape a clamp.

## The fleet catalog and preflight

`catalog` aggregates the cards of separate roots (`--estate` discovery) into
one generated, read-only projection: stable ordering, a content-hashed
`catalog_hash`, explicit broken/unreachable/stale/redacted entries, and a
byte-stable human-readable rendering that `--check-projection` drift-checks
against the cards. Cards stay authoritative; the catalog is always a
projection and redaction happens at the projection boundary. Rows sort by
root and name, except wikis their card withholds entirely: those sort last,
ordered by a hash of their identity, so a withheld row's position leaks no
ranking. Page content is never read.

`preflight` routes a substantive request at the catalog level under the host's
declared model class. Lexical card evidence only (triggers/keywords, name,
scope text; negative triggers decline explicitly). Access filtering happens
before any path is returned; the result states `matched`, `ambiguous`,
`no-match`, `unavailable`, or `privacy-filtered`, gives exact per-wiki
follow-up commands where allowed, and carries a deterministic `preflight_id`
content hash over the request hash, catalog snapshot, model class, and result.
Preflight never mutates a wiki, never writes a host record, never calls a
model, and never touches the network; whether and when a host runs preflight
is the host's own policy.

## Safety model

- `fsops.resolve_contained` resolves symlinks first and rejects any path that
  leaves the root: traversal (`..`), absolute paths, and symlinks pointing
  outside all fail closed.
- Writes are atomic (temp file + `os.replace`). Mutations of existing files
  first copy the old content to `.megamind/audit/backups/<name>.<hash>.bak`.
- Every mutating action inside a vault appends a JSON line to
  `.megamind/audit/log.jsonl`. Containment, backup, and audit are vault
  policies layered on top of the bare atomic-write primitive, so `setup skill`,
  which writes into a destination outside any vault, gets atomicity only.
- The registry stores only root-relative paths, so vaults stay portable and
  never leak machine-specific locations.
- `doctor` re-checks the invariants: containment, unsafe symlinks, router
  consistency, metadata validity, link integrity, proposal hygiene.

## Determinism

Commands avoid wall-clock dependence where it matters: proposal ids and plan
ids are content hashes, and `capture`, `evolve`, `review`, and the home view
accept `--today` for reproducible date handling in tests and benchmarks. The
only non-deterministic output is audit timestamps.
