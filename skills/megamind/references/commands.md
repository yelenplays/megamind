# Command reference

Global flags on every command: `--format toon|json` (default toon), `--root
<path>` (vault root, default current directory), `--no-help-hints`. Stdout is
always exactly one typed document with a `schema_version`; diagnostics go to
stderr. Exit codes: 0 success or definitive no-op, 1 operational failure
(including doctor errors), 2 usage or config validation.

## megamind-axi

No arguments: the local home (`megamind/home/v1`) with executable identity,
root, registered wikis, proposal counts, review and doctor aggregates, and
next actions. On an uninitialized directory it returns `initialized: false`
with init guidance (exit 0). Zero network, always.

## megamind-axi init <target> [--no-starter] [--wiki NAME]

`megamind/init-result/v1` with `created[]`/`skipped[]`. Never overwrites.
Re-running refreshes the generated `ROUTER.md` only while it still carries
the generated-file header. With `--wiki NAME` it scaffolds a canonical
single-wiki root instead of a vault (`layout: canonical-wiki`): `AGENTS.md`,
immutable `raw/`, `wiki/index.md` and `wiki/log.md`, and `.megamind/` state
headed by `wiki-card.json`.

## megamind-axi migrate

Upgrades a v1 registry to schema v2 in place, writing the derived access
posture out explicitly (backup plus audit record).
`megamind/migrate-result/v1` with `status: migrated|already_current`.

## megamind-axi catalog [--estate DIR] [--today D] [--full] [--emit-projection] [--check-projection PATH]

`megamind/catalog/v1`. Read-only fleet catalog over one root (`--root`) or a
directory of roots (`--estate`, discovered one level deep). Each row carries
the card fields, the effective model access, and maintenance aggregates.
Broken, unreachable, stale, and redacted entries are stated explicitly.
`catalog_hash` is a content hash of the rows. `--emit-projection` adds the
byte-stable human-readable projection; `--check-projection` compares a
checked-in projection against the cards and exits 1 on drift or a missing
file.

## megamind-axi preflight <request...> --model-class local|cloud [--estate DIR] [--today D] [--full]

`megamind/preflight-result/v1`. Catalog-level routing for a substantive
request under the declared model class: lexical card evidence only, no page
content, no writes, no network. Access filtering happens before paths are
returned: `none` wikis move to `filtered[]`, pointer wikis expose location
metadata only, digest-only wikis allow only their approved digest. `status` is
`matched`, `ambiguous` (tie; `offers[]` choices, nothing loaded), `no-match`,
`unavailable`, or `privacy-filtered`. `preflight_id` is a deterministic
content hash over the request hash, catalog snapshot, model class, and result
- proof the consultation happened, without storing the raw request.

## megamind-axi adopt <target> [--name N] [--apply --plan-id ID] [--rollback] [--full]

Non-destructive adoption of an existing wiki directory as a canonical root.
Dry run: `megamind/adopt-plan/v1` with `files[]`, `directories[]`, notes about
detected legacy shapes (existing hub pages and surrogate digests are
referenced, never replaced), and a `plan_id`. `--apply --plan-id <id>` creates
exactly the planned new files (`megamind/adopt-result/v1`). `--rollback`
removes exactly what the last apply created, and only while the content is
unchanged since creation. Existing pages are never moved, renamed, or
rewritten.

## megamind-axi config show

## megamind-axi route <query...> [--fields ...]

`megamind/route-result/v1`. Default candidate fields: `path,kind,score,reason`.
Available: `path,kind,score,wiki,privacy,chars,reason,reasons`. `kind` is
`page`, `digest`, `index`, `card`, or `pointer`. No match returns
`matched: false` and `candidates[0]`, exit 0.

## megamind-axi capture [--text T | --file F] [--source S] [--type T] [--today D]

Reads stdin when neither `--text` nor `--file` is given.
`megamind/capture-result/v1` with `status: captured|duplicate`; duplicate
means identical content was already captured (idempotent, exit 0).

## megamind-axi evolve <proposal> [flags]

Dry run by default: `megamind/evolve-plan/v1` with `plan_id`, a diff bounded
to 60 lines (`diff_truncated`, `--full` lifts), and `creates_new_wiki`.
Flags: `--dest <page.md|WikiName>`, `--supersedes <page.md>`,
`--apply --plan-id <id>` (id must match the recomputed plan),
`--approve-new-wiki`, `--today <YYYY-MM-DD>`.
Apply returns `megamind/evolve-result/v1`; it backs up changed files under
`.megamind/audit/backups/` and appends to `.megamind/audit/log.jsonl`.
Applying a new top-level wiki also registers it in the same apply: the
registry entry, card and index skeletons, and regenerated router are part of
the reviewed diff and covered by the `plan_id`.
Re-applying an applied proposal is `status: noop`, exit 0.

## megamind-axi review [--today D] [--full]

`megamind/review-report/v1`: `status: clean|attention`, an `aggregates`
object with counts for every category, and per-category item lists (only
when non-empty, bounded to 20 items unless `--full`). Read-only.

## megamind-axi doctor [--full]

`megamind/doctor-report/v1`: `status`, `errors`, `warnings`, and a
`findings[N]{check,severity,path,message}` table (bounded to 50 unless
`--full`). Exit 1 when any finding is an error.

## megamind-axi config show

`megamind/config/v1`: registry path, budgets, and registered wikis.

## megamind-axi setup skill [--dest DIR]

Without `--dest`: a `megamind/setup-plan/v1` document describing what would
be installed. With `--dest`: copies the skill into `DIR/megamind` without
overwriting (`megamind/setup-result/v1`). Uninstall by deleting that
directory. Setup never makes network calls or edits shell/provider config.

## Errors

`megamind/error/v1` with a stable `code` (`usage_error`, `not_initialized`,
`registry_invalid`, `card_invalid`, `capture_invalid`, `proposal_not_found`,
`plan_mismatch`, `approval_required`, `evolve_invalid`, `adopt_invalid`,
`path_escape`, `frontmatter_invalid`, `io_error`), a sanitized `message`, and
`help[]` with corrective commands.
