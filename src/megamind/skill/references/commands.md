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
headed by `wiki-card.json`. A directory is one shape or the other: adding the
second shape to a root that already carries the first is refused with
`init_invalid`.

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

## megamind-axi preflight <request...> --model-class local|cloud [--estate DIR] [--today D] [--full] [--semantic]

`megamind/preflight-result/v2`. Catalog-level routing for a substantive
request under the declared model class: lexical card evidence by default, no
page content, no writes, no network. Access filtering happens before paths
are returned and before any reranking: `none` wikis move to `filtered[]`,
pointer wikis expose location metadata only, digest-only wikis allow only
their approved digest. Fixed route-confidence thresholds decide `status`:
`matched` at or above the 0.75 reliance floor, `ambiguous` below it or inside
the 0.05 ambiguity band (`offers[]` choices, nothing loaded), `no-match`
under the 0.25 floor, plus `unavailable` and `privacy-filtered`. Matches
carry per-match `confidence`, `freshness`, `reasons`, and a privacy-safe
`evidence` summary (routing class, numeric coverage, per-class signal counts,
and declared card provenance; `evidence.lexical_classes` is the fired subset
of those counts and contains only signal-class labels, with no raw
request-derived tokens or page content); `offers[]` entries carry the same
`reasons` and evidence but no path, follow-up, or budget. An authorized match
whose follow-up hands out a load path also carries the card's exact numeric
`context_budget` override; offers, filtered, withheld, broken, pointer, and
other outcomes told to load nothing carry no budget. `preflight_id` is a
deterministic content hash over the request hash, catalog snapshot, model
class, and result - proof the consultation happened, without storing the raw
request. `--semantic` reranks the authorized matches locally; the `semantic`
block states `disabled`, `ok`, `unavailable`, or `error` and any non-`ok`
state keeps the lexical order.

## megamind-axi adopt <target> [--name N] [--apply --plan-id ID] [--rollback] [--full]

Non-destructive adoption of an existing wiki directory as a canonical root.
Dry run: `megamind/adopt-plan/v1` with `files[]`, `directories[]`, notes about
detected legacy shapes (existing hub pages and surrogate digests are
referenced, never replaced), and a `plan_id`. `--apply --plan-id <id>` creates
exactly the planned new files (`megamind/adopt-result/v1`). `--rollback`
removes exactly what the last apply created, and only while the content is
unchanged since creation. Existing pages are never moved, renamed, or
rewritten.

## megamind-axi route <query...> [--fields ...] [--today D] [--semantic]

`megamind/route-result/v2`. Default candidate fields: `path,kind,score,reason`.
Available: `path,kind,score,wiki,privacy,chars,confidence,freshness,
semantic_score,provisional,reason,reasons`. `kind` is `page`, `digest`,
`index`, `card`, or `pointer`. `decision` applies the route-confidence
thresholds: `load` at or above 0.75, `offer` below it or inside the ambiguity
band, `no-match` under 0.25 (weak candidates are dropped with a note).
Confidence is necessary but not sufficient: the `governance[]` sidecar
(`path`, `provisional`, `trusted`, `disposition`; always present) marks
provisional wikis, which are never an authorized load and can degrade a
confident result to `offer` on their own. It covers the emitted packet plus
every provisional candidate a `load` withheld, which stays an `offer` row
rather than disappearing from the response. The `governance_downgrade` boolean
states whether a downgrade was a governance or a confidence decision as a typed
field, and `notes` says the same in words. No match returns
`matched: false` and `candidates[0]`, exit 0. `--today` makes per-candidate
`freshness` (`updated`, `age_days`, `stale`) reproducible; without it those
stay explicitly unknown. `--semantic` enables local char-ngram reranking of
the surfaced candidates only.

## megamind-axi assess claim|answer [flags]

`megamind/confidence-report/v1`: deterministic confidence against the 0.75
reliance floor, with the full `components[]` rationale. `assess claim` takes
`--source <quality>:<origin>` (`primary|synthesis|hypothesis|prior`, repeat
per source), `--ineligible-source <quality>:<origin>` (counts for nothing),
`--lifecycle <state|unknown>`, `--freshness fresh|stale|unknown`, and
`--contradicted`. Sources derived from one origin count once; unresolved
contradictions freeze the claim below the floor; stale or undated evidence
can never reach it; no eligible evidence yields `score: unknown`, never a
fabricated number. `assess answer` takes `--claim <score|unknown>` per
materially relied-upon claim and returns the weakest (unknown propagates).
Both exit 0; `meets_floor` states the reliance verdict.

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
`--full`). Exit 1 when any finding is an error. Both root shapes report: a
canonical wiki root with a card and no registry is validated as a canonical
root (required directories, a named card, the gap journal) instead of failing
as an uninitialized registry.

## megamind-axi config show

`megamind/config/v1`: registry path, budgets, and registered wikis.

## megamind-axi gap list|create|transition|attempt [--full]

Durable, append-only gap records for missing, weak, stale, and contradictory
coverage. `create` deduplicates by normalized semantic identity. Transitions
and attempts are machine-readable and retain cooldown, rejection, reopen, and
supersession history. `transition` requires an explicit `--status`, so no
omitted flag can reopen a gap. An omitted `--cooldown-until` keeps the recorded
backoff; pass it explicitly to change or clear it. Transitioning to the status
a gap already holds is an idempotent `status: unchanged` no-op; only a field
that status persists can make it inexact, so a repeat refuses on a different
`--reason` for `rejected`, a different `--superseded-by` for `superseded`, or a
different `--cooldown-until`, and ignores a flag the status never stores.
`--today` and `--cooldown-until` must both be ISO `YYYY-MM-DD` (or, for the
cooldown, an explicit empty string to clear it); they are validated before any
write, and replay revalidates every date already in the journal. `list` returns
at most 20 rows with `total` and a truncation note, and reports `attempt_count`
only - `--full` lifts both bounds.

## megamind-axi research-wave GAP_ID [capacity flags]

Plans a deterministic one-hop wave from host-supplied capacity facts. It emits
at most the direct gap and first-order nominations and never dispatches a
worker. Unknown capacity, active captain work, a full fleet, a target-wiki
worker, insufficient wave capacity, or less than 25 percent quota reserve is a
typed pause/refusal.

## megamind-axi research-result --nomination-json JSON --result-json JSON

Validates a host round-trip by stable correlation id and turns eligible
sources into an immutable-source ingest proposal. Results are bounded and
idempotent; a result with no eligible source is rejected before any proposal,
audit, or log write, and a replay that changes any part of the nomination
identity - wiki, topic, or the eligible sources - refuses.
Megamind performs no network or external action and never writes `raw/`.

## megamind-axi provision-wiki NAME PATH [criteria flags]

Creates a local provisional wiki only when every domain, demand, scope, owner,
policy, seed, and maintenance criterion passes. The criteria flags are required
by mode, not by the parser: a plan and an `--apply` both refuse with
`usage_error` naming whichever is missing, before any write, while
`--rollback --plan-id <id>` takes the positional name and path and nothing
else. Those positionals are checked against the manifest before anything is
touched: a plan id recorded for another wiki or path refuses with zero
mutation, and the result echoes the recorded `wiki` and `path`. `--apply` and
`--rollback` are mutually exclusive. Every command printed
in `help[]` runs verbatim, so an apply hint carries the criteria and `--today`
it needs to re-derive the same `plan_id`. It extends an existing registry
vault only: it refuses a canonical single-wiki root, refuses to bootstrap a
vault `init` has not created, and refuses a v1 registry until `migrate` has run
explicitly. Without `--apply` it plans with no side effects and returns a
content-bound `plan_id`; `--apply --plan-id <id>` applies exactly that plan as
a write-ahead transaction whose manifest and backups are flushed before the
first file changes and whose targets are all flushed and verified before the
record commits. Re-running the same apply is a verified `status: noop`, an
interrupted apply resumes from the manifest (including one whose targets did
not all reach disk), and `--rollback --plan-id <id>` undoes it completely; a
plan id from other arguments, stale targets, or edited generated content all
refuse. Rollback removes only manifest-tracked files and empties only
directories the transaction created: a page written into the wiki after the
apply is preserved, listed in a bounded `preserved` sample beside the full
`preserved_total`, and returns `status: partial`. When an apply fails and its
own undo has to keep such content, the transaction record survives as
`partial` and the command exits `provision_recovery_required`, so the retry or
the explicit `--rollback` is a deliberate choice rather than a lost record. It registers the card immediately with
`provisional: true`; provisional knowledge is not trusted, so route, catalog,
and preflight surface it and only ever offer it, never load it. No remote
repository, collaborators, account action, publication, merge, or spend is
possible.

## megamind-axi bench run|check

`bench run --fixtures DIR --queries FILE --thresholds FILE [--out FILE]` invokes the real public `preflight` and `route` interfaces over the frozen synthetic release fixture. It emits `megamind/benchmark-result/v1` with separate exact, near, paraphrase, ambiguous, no-match, and privacy metrics, authorized context accounting, canary/access safety counts, determinism, and local grep/full-vault baselines. `--repeat` requires byte-identical reruns. `bench check --results FILE --thresholds FILE` emits `megamind/benchmark-check/v1` and exits 1 when a frozen gate fails. No model, network, or external service is used.

## megamind-axi experiment plan|validate|score|record

`experiment plan` freezes a versioned task set, rubric, thresholds, model/provider identifier, tools, effort, seed, and distinct no-wiki/current-wiki/updated-wiki snapshot roots. It creates opaque deterministic arm labels and isolated arm output roots. The host executes identical tasks and supplies outputs; Megamind never invokes a worker or model. `validate --plan PLAN --outputs FILE...` rejects missing tasks, duplicate arms, changed snapshot digests, malformed context accounting, prompt/canary leakage, model-access or privacy violations, and cross-arm contamination. `score` applies only the frozen rubric and promotion gates, returning `promoted` or `rollback-required`; incomplete inputs are invalid/unsettled rather than silently promoted. `record --score FILE --audit-root DIR` appends a bounded safe hash-chained event with a rollback reference and no prompt, answer, secret, or sensitive-content copy.

## megamind-axi setup skill [--dest DIR]

Without `--dest`: a `megamind/setup-plan/v1` document describing what would
be installed. With `--dest`: copies the skill into `DIR/megamind` without
overwriting (`megamind/setup-result/v1`). Uninstall by deleting that
directory. Setup never makes network calls or edits shell/provider config.

## Errors

`megamind/error/v1` with a stable `code` (`usage_error`, `not_initialized`,
`registry_invalid`, `card_invalid`, `capture_invalid`, `proposal_not_found`,
`plan_mismatch`, `approval_required`, `evolve_invalid`, `adopt_invalid`,
`init_invalid`, `path_escape`, `frontmatter_invalid`, `io_error`,
`garden_invalid`, `gap_not_found`, `gap_transition_invalid`,
`provision_recovery_required`, `evaluation_invalid`), a sanitized `message`, and `help[]` with
corrective commands.
