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

## megamind-axi init <target> [--no-starter]

`megamind/init-result/v1` with `created[]`/`skipped[]`. Never overwrites.
Re-running refreshes the generated `ROUTER.md` only while it still carries
the generated-file header.

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
`registry_invalid`, `capture_invalid`, `proposal_not_found`, `plan_mismatch`,
`approval_required`, `evolve_invalid`, `path_escape`, `frontmatter_invalid`,
`io_error`), a sanitized `message`, and `help[]` with corrective commands.
