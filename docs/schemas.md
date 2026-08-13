# Schemas

Copy-ready templates live in [templates/](../templates/). This page defines
the fields.

## Registry (`.megamind/registry.json`)

```json
{
  "version": 2,
  "budgets": {
    "max_candidates": 5,
    "max_context_chars": 8000,
    "stale_days": 180,
    "micro_wiki_pages": 4,
    "top_level_topics": 3
  },
  "wikis": [
    {
      "name": "ProductWiki",
      "path": "ProductWiki",
      "privacy": "public-reference",
      "description": "What questions this wiki answers.",
      "keywords": ["product", "pricing"],
      "card": "ProductWiki/CARD.md",
      "digest": "ProductWiki/DIGEST.md",
      "index": "ProductWiki/INDEX.md",
      "purpose": "Answers questions about the synthetic product.",
      "answers": ["pricing model", "release process"],
      "does_not_answer": ["brand voice -> BrandingWiki"],
      "scope_boundaries": "Synthetic product facts only.",
      "owners": ["team-product@example.invalid"],
      "sensitivity": "public-reference",
      "model_access": {"local": "full", "cloud": "full"},
      "routing_mode": "full",
      "source_policy": {
        "summary": "Synthetic release notes only.",
        "allowlist": "ProductWiki/SOURCES.md",
        "allowlist_status": "approved"
      },
      "freshness": {"half_life_days": 90, "last_confirmed": "2026-08-01"},
      "examples": ["What is the current pricing model?"],
      "triggers": ["pricing", "release"],
      "negative_triggers": ["payroll"],
      "dependencies": ["BrandingWiki"],
      "context_budget": {"max_candidates": 3, "max_context_chars": 4000},
      "catalog_visibility": "full"
    }
  ]
}
```

Schema v2 is additive: every v1 registry loads unchanged and the new fields
fall back to the defaults below. `megamind-axi migrate` upgrades a v1 file in
place (with backup and audit record); doctor warns about v1 registries and
prints that guidance. A v1 registry that already carries v2 fields is rejected
with the same migration pointer.

Base rules (v1 and v2): paths are root-relative (absolute paths and `..` are
rejected), wiki names are unique, `privacy` is one of `public-reference`,
`company-private`, `personal-local`, `digest-only`, `pointer-only`, and
`max_candidates`, `max_context_chars`, and `stale_days` are positive. `card`,
`digest`, and `index` are optional; routing degrades gracefully without them.

v2 card fields, all optional:

- `purpose`, `answers`, `does_not_answer`, `scope_boundaries`: what the wiki
  covers and what it declines, in the owner's words.
- `owners`: policy metadata; who governs the wiki.
- `sensitivity`: `public-reference`, `company-private`, `collaborative`,
  `personal-local`, or `unclassified`. Empty derives from `privacy`.
- `model_access`: `local` and `cloud`, each `full`, `digest-only`, or `none`.
  An empty axis derives from the privacy class: public-reference defaults to
  cloud `full`, personal-local to cloud `digest-only`, company-private to
  cloud `none` (doctor warns until a `company-private` or `collaborative`
  wiki sets an explicit cloud policy), digest-only to `digest-only` on both
  axes, pointer-only to `none` on both. A `company-private` or `collaborative`
  sensitivity defaults cloud to `none` whatever its privacy class implies.
  Unknown, missing, broken, or unmigrated classifications always derive
  restrictively. An explicit sensitivity is reconciled with the sensitivity
  implied by privacy, and the more restrictive classification wins; doctor
  reports a wider contradiction. Explicit access values that exceed a
  sensitivity or privacy ceiling are likewise clamped down and reported as an
  `access` error. Personal-local privacy always caps cloud access at
  digest-only. A consistently classified company-private card may still set
  an explicit cloud policy, including `full`. Ceilings key on the effective
  sensitivity, so omitting `sensitivity` or spelling a wider one cannot escape
  a clamp.
- `routing_mode`: `full` or `pointer`. Pointer wikis return location metadata
  and zero content. Pointer-only privacy forces pointer mode.
- `source_policy`: a free-text `summary`, an `allowlist` path pointer, and
  the allowlist `allowlist_status` (`approved`, `proposed`, `none`).
- `freshness`: declared expectations only (`half_life_days`,
  `last_confirmed`). Staleness is computed at read time (catalog passes
  `--today`), never stored.
- `examples`, `triggers`, `negative_triggers`, `keywords`: the lexical
  routing signals. Negative triggers let a wiki decline a request explicitly.
- `dependencies`: other wikis this one relies on.
- `context_budget`: optional per-wiki overrides surfaced by the catalog.
- `catalog_visibility`: `full`, `redacted` (name, root, sensitivity, and
  purpose only), or `hidden` (identity and all fields withheld; the projection
  states that a wiki is withheld rather than omitting the row silently).
  Personal wikis default to `redacted`; everything else to `full`.
- `provisional`: additive boolean governance marker. A provisional wiki is
  surfaced by route, catalog, and preflight and may be offered as an explicit
  choice, but it is never an authorized load until confidence coverage and a
  later evaluation pass (see "Governed gardening records" below).

Loading validates types before use: every field must have the type shown
above (a JSON boolean is never accepted as a budget), and unknown fields at
any level are rejected rather than silently ignored, so a typo in a wiki entry
surfaces as a `registry_invalid` error instead of a silently defaulted value.

## Wiki card (`.megamind/wiki-card.json`)

A canonical wiki root carries exactly one authoritative card instead of a
multi-wiki registry. The card is the v2 wiki-entry field set above, at the top
level of a JSON object with `"schema": "megamind/wiki-card/v2"` and
`"version": 2`. The wiki path is the root itself, so `path` is omitted, and
`privacy` may be omitted entirely: an unclassified card stays locally readable
and cloud-restrictive until the owner classifies it. Malformed cards raise
`card_invalid`.

## Preflight result packet (`megamind/preflight-result/v2`)

`preflight-result/v2` carries `context_budget` only on an authorized `matches[]`
entry whose `follow_up` actually hands out a load path. It is the card's exact
numeric `max_candidates` and/or `max_context_chars` override, not a
host-generated estimate. Offers, filtered, redacted, broken-root, unavailable,
and no-match outcomes carry no budget or load path, and neither do pointer
matches or digest-only matches that declare no digest: they are told to load
nothing, so there is nothing for a budget to bound. `allows` does not decide
this on its own - a registry match that declares no card, digest, or index
still receives the executable bounded-ladder follow-up, so its declared budget
is stated even though `allows` is empty.

Each `matches[]` and `offers[]` entry also carries a bounded `evidence`
summary:

```json
{
  "routing_class": "lexical-card",
  "coverage": {"matched_terms": 2, "request_terms": 3, "ratio": 0.6667},
  "signal_counts": {"trigger": 1, "name": 0, "scope": 1},
  "provenance": {
    "source": "registry-card",
    "scope": "declared card metadata only",
    "page_content": false
  },
  "lexical_classes": ["trigger", "scope"],
  "semantic": null
}
```

The summary preserves routing class, coverage, and card provenance without
including raw request-derived tokens, page content, roots, or paths. The
sibling `matches[].reasons` keeps its v2 semantics of up to five literal
lexical reason strings, and confidence and freshness remain authoritative
alongside it. `evidence.semantic` is unchanged, but `evidence.lexical_classes`
contains only deterministic signal-class labels and never repeats those reason
strings, so request-derived tokens live in `reasons` only. There is exactly one
class list: `lexical_classes` is the fired subset of `signal_counts`, in the
fixed `trigger`, `name`, `scope` order.

The schema version stays `preflight-result/v2` and the proof identity is
unchanged: `preflight_id` continues to bind the request hash, catalog hash,
model class, and result. The budget and the lexical summary are already covered
by those inputs - the budget is part of the card and therefore of
`catalog_hash`, and the lexical summary is a pure function of the request,
catalog, and model class - so neither is added to the proof. `evidence.semantic`
is the one field that is not: it reflects the host-selected `--semantic`
backend, which is not a proof input, so a rerank that changes scores without
changing the ranked order keeps the same `preflight_id`. Repeated inputs with
the same backend selection stay byte-stable.

## Preflight selection result (`megamind/preflight-selection-result/v1`)

`select-offer` consumes a complete original JSON `preflight-result/v2` plus the
exact original request, model class, and one selected wiki identity. The result
binds `preflight_id`, `request_hash`, `catalog_hash`, `model_class`, the selected
wiki, current card/root facts, effective access, allows, follow-up, budget,
confidence/evidence, and selection provenance into `selection_id`.
`root_facts_hash` covers current card facts and root-relative resolved paths,
including symlink resolution, without exposing machine-specific absolute paths
or hashing page content.

`selection.status` is `explicit-user-selection`, its basis is
`selected-current-offer`, and `confidence_changed` is always false. `selected`
starts with the exact validated original offer fields and additively carries
only the current card-derived `access`, `routing_mode`, `allows`, `follow_up`,
and, when that follow-up is loadable, `context_budget`. Thus a selected
sub-floor offer remains visibly sub-floor rather than becoming a confidence
match. Digest-only yields one approved digest path. Full yields the same
registry route ladder or canonical index ladder and only the already-declared
card/digest/index allows. Pointer and no-digest outcomes cannot produce this
schema because they fail typed before authorization.

`catalog_visibility` stays what it is everywhere else: a projection control, not
an access control. A `redacted` wiki, the documented default for personal wikis,
is redacted in the rendered catalog and remains selectable on exactly the terms
preflight already routes it on; whether it may be loaded is decided by the
independent access, provisional-trust, routing-mode, follow-up, artifact, and
containment checks. A `hidden` wiki is never projected, never offered, and never
selectable.

The complete packet is mandatory: truncation, malformed lists/evidence,
changed request/catalog/model class, duplicate or unknown identity, any
non-offer insertion, filtered/hidden/broken/absent/provisional/pointer state,
access `none`, missing load artifacts, and traversal or escaping symlinks are
`selection_invalid`. The operation is read-only and deterministic.

## Governed gardening records (Phase 3)

`.megamind/gaps.jsonl` is an append-only snapshot journal. The latest record
for each `gap_id` is authoritative, while earlier lines retain the mutation
history for crash recovery and audit. Every record has `schema: megamind/gap/v1`
and these stable fields:

```json
{
  "schema": "megamind/gap/v1",
  "gap_id": "content-hash",
  "wiki": "ProductWiki",
  "topic": "release cleanup",
  "kind": "missing",
  "status": "open",
  "priority": {"impact": 4, "urgency": 3, "repeat_demand": 2,
               "coverage": 1, "confidence_risk": 4, "score": 30},
  "attempts": [], "cooldown_until": "", "rejection": null,
  "reopened_from": "", "superseded_by": "", "related_topics": [],
  "created": "2026-01-01", "updated": "2026-01-01", "identity": "content-hash"
}
```

The identity hashes normalized wiki, topic, and kind, so repeated reports
coalesce without comparing page bodies. Lifecycle transitions are explicit;
attempts, cooldown, rejection, reopen, and supersession are never discarded.
`gap transition` always requires an explicit `--status`, so an omitted flag can
never reopen a resolved or rejected gap. An omitted `--cooldown-until` keeps the
recorded backoff; only an explicitly supplied value (including an explicit empty
string) replaces or clears it.

A transition to the status a gap already holds is an exact repeat: it appends no
snapshot, keeps the recorded date, and never rewrites a rejection reason, a
supersession target, or a cooldown, so `status: unchanged` comes back instead of
a lifecycle move that did not happen. Only a field that status actually persists
can make a repeat inexact - a different `--reason` on `rejected`, a different
`--superseded-by` on `superseded`, or a different `--cooldown-until` on any
status - and that refuses with `gap_transition_invalid` rather than overwriting
a terminal fact. A flag the status does not persist changes nothing, so
`--reason` on a repeated `open` or `paused` transition stays a no-op instead of
refusing an edit it was never going to make.

Dates are supplied by the host or CLI, never read from the clock, and every
date a record carries - `created`, `updated`, `cooldown_until`, each attempt's
`date`, and the rejection `date` - is parsed as ISO `YYYY-MM-DD` by one owner
before any write, so a malformed date fails typed instead of becoming permanent
journal state or a log heading, and two spellings of one day can never read as
two values. `--today` and `--cooldown-until` both refuse a non-ISO value as a
usage error before any side effect; the empty string stays the documented way to
clear a cooldown. Replay revalidates the same fields, so a hand-edited journal
fails at read rather than at the next mutation. A journal line that is not a
JSON gap object, or that carries a date in any other form, is a typed
`garden_invalid` error, and `doctor` reports it in both root shapes. `gap list`
bounds its rows to 20 with a note and `--full`, and keeps the unbounded
`attempts` history behind `--full`.

`megamind/research-wave/v1` is a plan, not a dispatch instruction. It contains
one direct nomination and bounded first-order nominations; deeper topics appear
in `deferred_nominations` for later reprioritization. A host supplies a typed
capacity fact. Unknown capacity, less than 25 percent measurable applicable
quota reserve, active captain work, a full three-worker fleet, a worker already
on the target wiki, or insufficient capacity through the wave produces a typed
`paused` or `refused` result. Megamind never calls a quota tool or starts a
worker.

`megamind/research-nomination/v1` correlation IDs are stable within a wave.
`megamind/research-result/v1` remains readable as a restrictive legacy
nomination. Its caller-supplied `eligible` flag is not evidence and cannot
create an ingest proposal, infer quality or rights, or authorize a future
apply. A v2 result is the only result that can propose a source. Either
version bounds one result to 20 sources and refuses a longer list. Each v2
source carries an `acceptance` block with required host-supplied typed facts:

```json
{
  "origin_id": "derived-origin-identity",
  "retrieval": {"date": "2026-08-13", "precision": "exact"},
  "publication": {"date": "2026-08", "precision": "month"},
  "snapshot": {"sha256": "<64 hex>", "normalized_sha256": "<64 hex>"},
  "rights": {
    "license": "CC-BY-4.0",
    "quote_policy": "quote-bounded",
    "snapshot_policy": "local-snapshot-allowed"
  },
  "corrections": {
    "status": "clean",
    "checked_at": {"date": "2026-08-13", "precision": "exact"},
    "method": "host-registry",
    "notice_ids": []
  }
}
```

Megamind derives eligibility from these validated facts. Missing, unknown,
malformed, contradictory, non-clean correction, or retracted facts produce a
typed ineligible reason and never become support. That reason names the field
and the vocabulary it violated, never the offending value, and is bounded
before it reaches the document. Contradiction is checked
across facts, not only within one: a `clean` status carrying `notice_ids` is
refused, and so is a publication whose earliest possible day falls after the
exact retrieval date (a coarse `month` or `year` publication is an interval,
so it is ordered by the first day it can denote). The `origin` display string
is separate from `origin_id`; only the latter may corroborate a claim, and
unknown independence collapses to one origin. Rights and correction facts are
owned by the host and are never fetched by Megamind. Instruction-shaped source
text is inert data and cannot set acceptance, quality, destination, or
lifecycle fields. Accepted v2 results create a replay-safe proposal with
schema `megamind/ingest-proposal/v2` and `immutable_raw_required: true`; no
command fetches a URL or writes `raw/`. Correlation remains the idempotency
key: exact replays are no-ops and divergent nomination or evidence facts
refuse. Every host string that reaches the durable proposal - origins,
summaries, and the `origin_id`, `license`, `method`, and `notice_ids`
acceptance strings - crosses one projection boundary: it is bounded (an
over-long acceptance string is a typed refusal, never a silent truncation) and
redacted only where a credential assignment or a local filesystem path is
structurally identified, so a cited origin such as
`https://docs.example.com/home/getting-started` survives intact.

A provisional registry entry carries `provisional: true`. It is created only
when all local qualification inputs pass: accepted domain, repeat demand,
multiple topics, overlap, scope and exclusions, owner, source policy,
privacy/model access, seed topics, and maintenance.

`provision-wiki` only ever extends an existing registry vault. It refuses a
canonical single-wiki root (that root's `wiki-card.json` stays the one
authority), refuses to bootstrap a vault that `init` has not created, and
refuses a v1 registry with an instruction to run `migrate` explicitly first, so
migration notes are never silently skipped. The whole registry plan is
serialized and validated before any directory or file is written, so a rejected
entry cannot leave an orphan scaffold behind. The new wiki's nested
`.megamind/wiki-card.json` is written through the same serializer as every other
canonical card, with paths rooted at the wiki directory, while the registry
entry keeps its vault-relative paths.

Planning is side-effect-free and yields a content-bound `plan_id`; `--apply`
takes that id back as the approval token. The qualification criteria are
required by mode rather than by the parser: planning and `--apply` both refuse
with a typed `usage_error` naming every missing flag before anything is read or
written, while `--rollback --plan-id <id>` is driven by the plan id and the
positional name and path alone. Those positionals are the identity the rollback
is checked against, not decoration: the recorded wiki name must match exactly
and the recorded path must resolve to the same contained target (so `./Wiki`
and `Wiki` are one target, while a traversal or an escaping symlink is a
`path_escape` refusal). A pasted plan id from another transaction therefore
refuses instead of undoing that wiki under this name, and the refusal happens
before any backup is read, any file changes, any audit is appended, or the
record is removed. The result echoes the recorded `wiki` and `path`, so a
response can never agree with a host that believed it was undoing something
else. `--apply` and `--rollback` are mutually exclusive. Because `--apply` re-derives the plan from the criteria and
`--today`, the apply command printed in `help[]` carries both, so every emitted
recovery command runs exactly as printed. The apply is a write-ahead
transaction recorded in `.megamind/audit/provisional-wiki-<plan_id>.json`
(`megamind/provisional-wiki-rollback/v1`), which carries the whole plan, the
prior content of every file it will replace, and a `state` of `pending`,
`applied`, `partial`, or `rolled_back`. The manifest and every required backup are
persisted and flushed to disk before the first target mutation, every target is
written atomically and flushed with its parent directory, and the record only
switches to `applied` after every target has been verified to hold its planned
bytes. The directory half of that flush needs a platform primitive: POSIX
targets get it, Windows exposes none, so durability there is exactly the file
flush and no stronger (`fsops.sync_directory` reports which it did rather than
claiming a guarantee the platform cannot give). The reviewed plan is never
mutated by the apply it authorized, so it keeps hashing to the `plan_id` that
approved it. An apply that a signal or power loss interrupts is therefore
always recoverable from the record alone:

- re-running `--apply --plan-id <id>` with the same wiki and path resumes a
  `pending` transaction to completion, or verifies an `applied` one file by
  file and returns `status: noop`. The replay is reached before the plan is
  recomputed, so the wiki the first apply registered never turns the second
  into a `wiki already exists` failure;
- an `applied` record over a target that is absent or still carries its prior
  content is an incomplete commit, not a success: the same replay finishes it
  and `--rollback` still undoes it, so a `noop` is never reported over content
  that is not on disk;
- a plan id recorded for a different wiki or path, and any file the transaction
  did not itself write, refuse with a typed `garden_invalid` result;
- `--rollback --plan-id <id>` undoes a `pending` or `applied` transaction
  completely, restoring backed-up files and removing created ones, and refuses
  when generated content was edited. It marks the record `rolled_back`, which
  leaves the vault re-plannable: the same criteria produce the same `plan_id`
  and a fresh apply;
- recovery deletes only the exact files the manifest tracks, and prunes only
  directories the transaction is recorded as having created and left empty
  (`created_dirs`, written before the first mutation). Nothing is ever removed
  recursively. A page authored inside the provisional wiki after the apply is
  not the transaction's to delete: it is preserved, named in `preserved`, and
  the result comes back as `status: partial` instead of `rolled_back`.
  `preserved` is a bounded sample of at most 20 entries; `preserved_total`
  carries the real count in the result and in the audit record alike, and a
  note states the truncation explicitly whenever the total exceeds the sample;
- the in-process undo that runs when an apply raises follows the same rule, and
  it owns the record too. It removes the manifest only after a verified
  complete undo. When it had to keep foreign content, or could not restore
  every target it wrote, it marks the record `partial`, appends a
  `provisional-wiki-undo` audit record carrying the same bounded `preserved`
  sample and `preserved_total`, and raises `provision_recovery_required` with
  the original failure as its cause. The surviving record is what makes the
  retry work: `--apply --plan-id <id>` resumes the `partial` transaction
  through the manifest before planning can refuse the directory the preserved
  content keeps alive, and `--rollback --plan-id <id>` closes it instead.

Provisional knowledge is not trusted until confidence coverage and a later
evaluation clear it, and every consumer acts on that: catalog rows carry
`provisional`, preflight matches and offers carry `provisional`, and `route`
emits a `governance[]` sidecar with `path`, `provisional`, `trusted`, and
`disposition` (`load` or `offer`) for every candidate it accounts for (plus
`provisional` as an opt-in `--fields` column, so the default candidate columns
stay stable). A provisional wiki is never an authorized load: in `route` it
leaves the `load` packet and stays a governance-sidecar offer row keyed by its
own path (the result degrades to `offer` when nothing trusted remains), and in
`preflight` it stays an offer with no loadable paths. Both surfaces therefore
keep a provisional candidate visible as an offer while authorizing loads only
for trusted ones. This gate runs after the confidence
thresholds and only narrows them, so it can produce an `offer` at or above the
reliance floor; the additive `governance_downgrade` boolean states that cause
as a typed field, and `notes` names it as a governance decision and never as a
confidence one. Privacy-safe offers and research nominations may still name a
provisional wiki explicitly.

## Evaluation schemas (Phase 4)

Release inputs are immutable by version and safe to publish. A task set is
an object with `schema: megamind/evaluation-task-set/v1`, `frozen: true`, a
`version`, and unique pre-authored task objects. A benchmark query set is
JSONL whose first record is a `megamind/benchmark-query-set/v1` header with
`frozen: true` and a `version`; a file without that header has no provenance
and is refused. Each following record carries an `id`, a `tier`, a `query`,
the `expected_wikis`, and a `model_class`, and may pin `expect_status` and
`expect_loaded` as behavior contracts.

Threshold files are deterministic TOML (or JSON) and are hashed into every
result. A `[binding]` section is mandatory: it names the
`benchmark_version`, `corpus_sha256`, `queries_sha256`, `task_set_version`,
and `task_set_sha256` the gates were preregistered against, every `*_sha256`
is a 64-character hex digest, and a missing, stale, or tampered binding is
refused rather than defaulted. `[release]` must preregister all eight
release gates and `[promotion]` all three promotion gates: unknown keys and
missing keys are both refused, accuracy floors and regression tolerances
must be finite numbers in `[0.0, 1.0]`, and the `*_max` counters must be
non-negative whole numbers. `megamind/benchmark-result/v1` contains
`benchmark_version` (taken from the query-set header, never a literal),
`corpus_sha256`, `queries_sha256`, `thresholds_sha256`, per-tier aggregates,
per-query canonical rows with separate `candidates` and authorized `loaded`
sets, safety counts, and honest local baselines. It has no wall-clock field.
`benchmark-check/v1` reports each gate and failed gate names; a tier with no
queries scores zero so an absent tier fails closed.

`megamind/evaluation-plan/v1` freezes the task-set, rubric, threshold,
model, tools, effort, and execution seed. It assigns opaque arm labels and
distinct output roots, carries an `identity_id` over everything the
commitments commit to, lists `snapshot_commitments` as a sorted set never
tied to a label, and carries `blinding` with the scheme, a keyed assignment
commitment, and a `key_id` fingerprint. It never names a condition beside a
label, never carries the blinding key, and never carries a raw snapshot
digest: the empty no-wiki tree hashes to a key-free public constant, so a
raw digest would identify that condition on its own.
`megamind/evaluation-grader-packet/v1` carries only the blind labels, the
task prompts, the rubric, and the same commitment.
`megamind/evaluation-unblinding-map/v1` is a separate host artifact, written
mode 0600, holding the private `blinding_key`, the label-to-condition
`assignments`, and per-arm `snapshots` (root, digest, and commitment). It is
accepted only when it belongs to the plan by `plan_id` and `identity_id`,
carries the key the plan's `key_id` names, opens the plan's assignment
commitment, re-derives the same assignment, and opens every planned snapshot
commitment, so a substituted, re-keyed, replayed, or edited map is refused.
Host arm output is `megamind/evaluation-arm-output/v1`; it must carry the
plan/task versions, opaque label, session id, a `snapshot_commitment` drawn
from the planned set and no raw `wiki_sha256`, every task exactly once, the
actual non-negative `authorized_context_chars`, provenance, and zero
privacy/model access violations. Validation rejects malformed output, stale
or tampered inputs, missing tasks, duplicate arms, commitments that do not
cover the planned set exactly once, path escapes, leakage, or cross-arm
contamination, and needs no map or key. Scoring returns
`megamind/evaluation-score/v1`, sealing the per-label blind scores as
`blind_scores_sha256` before the map is opened at all: promotion requires
improved target outcomes, no material adjacent regression, provenance within
the preregistered `provenance_regression_max` tolerance, and zero new safety
violations. Failure is `rollback-required` with a non-empty `rollback_ref`;
incomplete or unvalidated input is a typed `unsettled` document naming
`missing_arms`, and is never promoted. `experiment record` appends
`megamind/evaluation-record/v1` to an audit JSONL journal with a
deterministic hash-chain event, bounded safe summary, and rollback
reference. Prompts, answers, canaries, secrets, and sensitive content are
never copied to the event.

## Governed host rollout records (Phase 6)

`megamind/host-rollout-evidence/v1` is a host-supplied object with `host_id`,
`model_class`, and exactly six boolean checks: `mandatory_preflight`,
`privacy_enforcement`, `quiet_no_match`, `task_logging`, `failure_disclosure`,
and `local_only_activation`. Every check must be true. Unknown fields are
refused.

`megamind/rollout-plan/v1` binds one host/wiki target to its effective model
access, sensitivity, deterministic privacy rank, sequence and prior promotion
ids, current card/catalog digests, matched and no-match preflight ids, promoted
evaluation identity and blind-score seal, doctor aggregates, and hashed
approval references. `status` is `ready` only when every typed check passes;
otherwise it is `blocked`. The `plan_id` hashes the full plan body. No raw
request, approval text, root, prompt, answer, or wiki content enters the plan.

`megamind/host-wiki-promotion-proof/v1` is created only from a ready plan under
its exact approval token. It carries `status: promoted`, `loadable: true`, one
host, one wiki, one model class, and exactly the card-derived access. A prior
proof chain must cover sequences `0..N-1` for the same host/model class and be
nondecreasing by privacy rank. Public reference starts earliest; company,
collaborative, personal, and unclassified surfaces sort later; pointer and
`none` postures are never loadable. Unknown sensitivity does not move earlier
because access happens to be bounded.

Rollout state is an explicit directory outside every estate and vault:

```text
transactions/  # durable promote, blocked, and rollback records
proofs/        # immutable promotion proofs
active/        # host-consumable local projections
receipts/      # rollback receipts
outcomes/      # durable non-loadable blocked plans
```

The matched and no-match preflight evidence must both be recomputable against
the current catalog for the declared model class. The no-match result must come
from a substantive request: usable terms, no matches, and a request hash
distinct from the matched one. A termless request proves nothing about host
quietness and fails the `quiet-no-match` check.

Promotion writes `megamind/rollout-transaction/v1` in `pending` state before
the proof or active projection, verifies each exact document, then records
`applied`. Replay resumes pending bytes or returns `noop`; foreign bytes or a
stale token refuse. A blocked apply writes only a non-loadable outcome.

`proofs/` and `receipts/` are append-only and content-addressed; `active/` is
the single mutable projection and only a transaction moves it. A promote may
replace a `rolled-back` projection for its binding, recorded as the
transaction's `previous_active`, so a disarmed binding can be armed again by a
fresh plan with its own evidence and approvals. It may never replace a
`promoted` projection, other bytes, or a binding an unfinished promote
transaction still owns, and replaying a rolled-back plan refuses instead of
re-arming.

`megamind/rollout-health/v1` rechecks the active projection, current card hash,
exact effective access, non-provisional marker, and doctor errors. Any failure
is `status: rollback-required` and `loadable: false`.
`megamind/rollout-rollback-plan/v1` is dry-run and content-bound;
`megamind/rollout-rollback-receipt/v1` disarms the active projection, hashes the
safe reason, and retains the proof and transaction. Rollback is itself pending
before mutation and replay-safe. `megamind/rollout-status/v1` reports bounded
`active`, `rolled_back`, and `blocked` rows with full counts. `active` projects
the live binding files, while `rolled_back` projects the append-only receipt
ledger; `counts.rolled_back` is the receipt count, so re-arming a binding
supersedes its live row without erasing the rollback from the audit view.

These records authorize no provider or external action. The host remains the
only owner of proof consumption, model execution, scheduling, workers, grading,
research, publication, repositories, accounts, collaborators, merges, and
spend.

## Canonical wiki root

`megamind-axi init <path> --wiki <Name>` scaffolds the canonical layout:

```text
AGENTS.md                # domain schema, conventions, workflows
raw/                     # immutable human-curated sources
raw/assets/              # optional source images and files
wiki/index.md            # content-oriented catalog of compiled pages
wiki/log.md              # structured append-only event log
wiki/...                 # AI-maintained compiled pages
.megamind/wiki-card.json # the authoritative card
.megamind/proposals/     # knowledge proposals awaiting approval
.megamind/gaps.jsonl     # durable knowledge-gap records
.megamind/audit/         # mutation records, backups, rollback material
```

The `raw/` layer is immutable to Megamind: it is read and cited, never
modified. `wiki/log.md` events use a stable field shape (date, type, summary,
pages, sources, confidence, outcome, audit reference) and never record
credentials, secrets, or verbatim sensitive prompts. `megamind-axi adopt
<path>` brings an existing wiki directory into this shape non-destructively:
it detects existing index/hub pages and surrogate digests and points the new
card at them instead of replacing anything, and `adopt --rollback` removes
exactly the generated material.

A directory carries one root shape or the other, never both: a registry vault
and a canonical wiki root disagree about which card is authoritative, so
discovery would have to guess. Both `init` and `adopt` refuse to add the
second shape to a root that already carries the first (`init_invalid`,
`adopt_invalid`).


## Frontmatter subset

Megamind reads and writes a deterministic YAML subset: `key: value` scalars
(strings, integers, `true`/`false`), flow lists (`[a, b]`), and block lists of
scalars. Nested maps are not supported; doctor reports files it cannot parse.
Obsidian reads this subset fine.

Writing quotes any scalar that would not survive a round trip bare: embedded
newlines, surrounding whitespace, the empty string, leading structural markers
(`-`, `[`, `#`, ...), and values that would otherwise parse back as a boolean
or an integer. Inside quotes, `\\`, `"`, and the whitespace escapes `\n`,
`\r`, `\t` carry their usual meaning. This is a safety property, not
cosmetics: an untrusted value such as a capture provenance label can never
inject additional frontmatter keys, so it can never flip a proposal's `status`
or a page's `superseded_by`.

## Routing card (`CARD.md`)

Frontmatter: `megamind: routing-card`, `wiki`, `privacy`, `keywords`.
Body: what the wiki answers and does not answer. The card is the first rung of
the retrieval ladder, so its keywords are the strongest routing signal.

## Digest (`DIGEST.md`) and index (`INDEX.md`)

Digest: `megamind: digest` plus a body kept deliberately small; it is what
gets read when no exact page matches, and the only routable content of
`digest-only` wikis. Index: `megamind: index` plus one Markdown link per topic
page with a short hint; the router scores these entries to find exact pages.
Link targets are read percent-decoded, so a page name carrying a space, `(`,
`)`, `#`, or `%` is linked in the encoded form Markdown can carry, and a
generated entry label carries `[` and `]` rewritten as `(` and `)` so the line
stays a readable link.

## Topic page

```
---
title: Pricing model
type: decision            # fact|decision|hypothesis|procedure|example|guidance
status: active            # proposed|confirmed|active|shaky|rejected|superseded
created: 2026-01-10
updated: 2026-01-10
provenance:
  - where this came from
superseded_by: path.md    # required when status is superseded
---
```

## Deterministic research state (`.megamind/research/`)

Slice 1 research uses strict JSON documents: `megamind/research-plan/v1` is
content-addressed and binds the gap, question, capability flags, hard budget
ceilings, policy/card/access digests, and change envelope. The append-only
`research/jobs.jsonl` journal stores `megamind/research-job/v1` transition
facts. `research/packets/<id>.json` and `research/outcomes/<id>.json` are
immutable `megamind/research-packet/v1` and `megamind/research-outcome/v1`
artifacts. IDs are opaque references; packet validation uses narrow resolver
interfaces and never treats a packet as admitted answer evidence. Cancellation
retains artifacts, replaying the same event is a no-op, and divergent replay,
terminal mutation, or policy/card/access drift is refused.

`research packet --input packet.json` compiles a packet to the existing normal
`.megamind/proposals/<id>.md` shape. `evolve` validates the packet reference
inside its existing write-ahead transaction; apply remains one explicit
approval per cycle. Core has no network or host orchestration.

## Proposal (`.megamind/proposals/<id>.md`)

Written by `capture`: `megamind: proposal`, `id` (content hash, equals the
filename), `type`, `status` (`proposed`, `applied`, or `rejected`), `source`,
`captured`, `suggested_destination`, `route_reasons`, and after an apply,
`applied_to` and `applied_on`.

## Top-level wiki proposal

See [templates/top-level-wiki-proposal.md](../templates/top-level-wiki-proposal.md).
It documents why the new wiki should exist and is only ever applied with
`--approve-new-wiki` after human approval.

## Evolution transaction (`.megamind/audit/evolve-<plan-id>.json`)

An approved `evolve --apply` persists `megamind/evolve-rollback/v1` before its
first target mutation. It binds the plan and proposal identities, action,
destination, exact old/new bytes for each controlled path, backup references,
path roles, pre-change/applied controlled-tree SHA-256 values, and a state of
`pending`, `applied`, or `rolled_back`. A replay of `--apply --plan-id` resumes
`pending` work. `--rollback --plan-id` accepts only the proposal identity that
owns the transaction, verifies every path is still at its old or new bytes
before writing, removes only an unchanged file the transaction created,
restores replaced files and proposal status, and retains the manifest. Foreign
files and stale, tampered, or already-rolled-back plan ids are refused.
Canonical roots treat `raw/` as outside the evolution surface.

## Audit records (`.megamind/audit/log.jsonl`)

One JSON object per line: `ts` (UTC ISO), `action` (`init`, `migrate`,
`capture`, `evolve-apply`, `evolve-apply-proposal-status`, `evolve-rollback`,
`router-refresh`, `adopt-apply`, `adopt-rollback`, `gap-transition`,
`research-ingest-proposal`, `provisional-wiki-create`, `provisional-wiki-undo`,
`provisional-wiki-rollback`), and action-specific fields such as `path`,
`proposal_id`, `plan_id`, `gap_id`, `correlation_id`, `preserved`,
`preserved_total`, and `backup`. Backups of every mutated file live in
`.megamind/audit/backups/<name>.<content-hash>.bak`. Adoption additionally
writes `.megamind/audit/adoption-<plan_id>.json`, the content-hashed rollback
record that `adopt --rollback` verifies before removing generated files;
`provision-wiki` writes the equivalent transaction record described under
"Governed gardening records" above.
