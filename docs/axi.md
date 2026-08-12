# The megamind-axi output contract

Megamind's executable boundary is an AXI (agent experience interface), built
against the ten principles at <https://axi.md/>. Agents are the first-class
consumer; humans read the same structured document, not a separate prose
rendering that could drift.

## One document per invocation

Stdout carries exactly one typed document. TOON is the default rendering;
`--format json` renders the exact same object, and golden tests enforce that
the two never diverge. Diagnostics never go to stdout. Every document starts
with a stable `schema_version`:

| schema_version | emitted by |
| --- | --- |
| `megamind/home/v1` | `megamind-axi` (no arguments) |
| `megamind/init-result/v1` | `init` (vault or `--wiki` canonical root) |
| `megamind/route-result/v2` | `route` |
| `megamind/capture-result/v1` | `capture` |
| `megamind/evolve-plan/v1` | `evolve` (dry run) |
| `megamind/evolve-result/v1` | `evolve --apply`, `evolve --rollback` |
| `megamind/review-report/v1` | `review` |
| `megamind/doctor-report/v1` | `doctor` |
| `megamind/catalog/v1` | `catalog` |
| `megamind/preflight-result/v2` | `preflight` |
| `megamind/confidence-report/v1` | `assess claim`, `assess answer` |
| `megamind/adopt-plan/v1` | `adopt` (dry run) |
| `megamind/adopt-result/v1` | `adopt --apply`, `adopt --rollback` |
| `megamind/migrate-result/v1` | `migrate` |
| `megamind/config/v1` | `config show` |
| `megamind/setup-plan/v1`, `megamind/setup-result/v1` | `setup skill` |
| `megamind/gaps-result/v1`, `megamind/gap-result/v1` | `gap` |
| `megamind/gap-transition/v1`, `megamind/gap-attempt/v1` | `gap` mutations |
| `megamind/research-wave/v1` | `research-wave` |
| `megamind/research-result/v1` | `research-result` |
| `megamind/provisional-wiki-result/v1` | `provision-wiki` |
| `megamind/benchmark-result/v1`, `megamind/benchmark-check/v1` | `bench run`, `bench check` |
| `megamind/evaluation-key/v1` | `experiment keygen` |
| `megamind/evaluation-plan/v1`, `megamind/evaluation-grader-packet/v1`, `megamind/evaluation-unblinding-map/v1` | `experiment plan` |
| `megamind/evaluation-validation/v1` | `experiment validate` |
| `megamind/evaluation-score/v1` | `experiment score` |
| `megamind/evaluation-record/v1` | `experiment record` |
| `megamind/rollout-plan/v1`, `megamind/rollout-result/v1` | `rollout promote` |
| `megamind/host-wiki-promotion-proof/v1` | successful `rollout promote --apply` proof |
| `megamind/rollout-health/v1` | `rollout health` |
| `megamind/rollout-rollback-plan/v1`, `megamind/rollout-rollback-receipt/v1` | `rollout rollback` |
| `megamind/rollout-status/v1` | `rollout status` |
| `megamind/error/v1` | any failure |

The v2 retrieval documents are additive over their v1 shapes: every v1 field
keeps its name and meaning, and v2 adds route confidence, thresholds,
semantic-rerank outcome, per-candidate freshness (see below), the
`route-result/v2` `governance[]` sidecar and its `governance_downgrade` flag,
and the `provisional` governance marker on catalog rows and preflight matches
and offers.

Example (`megamind-axi route "pricing"` on the examples vault):

```toon
schema_version: megamind/route-result/v2
query: pricing
matched: true
decision: load
confidence: 1.0
thresholds:
  reliance_floor: 0.75
  offer_floor: 0.25
  ambiguity_band: 0.05
semantic:
  status: disabled
  backend: none
  reason: semantic reranking not enabled
candidates[1]{path,kind,score,reason}:
  ProductWiki/topics/pricing-v2.md,page,7,"keyword match: pricing"
governance[1]{path,provisional,trusted,disposition}:
  ProductWiki/topics/pricing-v2.md,false,true,load
governance_downgrade: false
context_chars: 446
max_context_chars: 8000
max_candidates: 5
notes[0]:
help[2]:
  Open `ProductWiki/topics/pricing-v2.md` first; it scored highest
  "Run `megamind-axi route pricing --fields path,kind,score,confidence,reasons` for detail"
```

## Route confidence, thresholds, and the semantic layer

Route, claim, and answer confidence are three separate kinds; the shared
reliance floor is 0.75. Route confidence is a deterministic blend of the
strongest per-token routing signal (declared triggers/keywords and index
labels count full, free text least) and query-token coverage; the rubric
constants live in `megamind.confidence` and are pinned by calibration
fixtures. `route` and `preflight` apply fixed thresholds:

- at or above 0.75 (`reliance_floor`) the route may load automatically
  (`decision: load`, preflight `status: matched`); the floor is per candidate,
  so a load carries only candidates that reach it themselves - weaker rows are
  omitted from a route packet and demoted to preflight offers;
- from 0.25 (`offer_floor`) up to 0.75, or whenever the top candidates sit
  inside a 0.05 `ambiguity_band`, the route offers choices without loading
  (`decision: offer`, preflight `status: ambiguous`, no `allows` paths or
  follow-up commands on offers);
- below 0.25 the evidence is dropped and the result is a definitive no-match
  that stays quiet.

Confidence is necessary for a load, not sufficient. One governance gate runs
after the thresholds and can only narrow them: a wiki marked `provisional`
(see `docs/schemas.md`) is never an authorized load or active content match
until confidence coverage and a later evaluation pass, however confident the
match is. So `confidence >= 0.75` alone does not imply a loadable result: a
provisional candidate that cleared the floor leaves the `load` packet and
becomes an offer, and when every candidate that cleared it is provisional the
whole result degrades to `decision: offer` / `status: ambiguous` with the top
confidence still at or above the floor and no `ambiguity_band` involved.
The cause is a typed field, not prose to parse: `governance_downgrade` is
`true` exactly when the governance gate, not a threshold, produced the offer.
`notes` says the same thing in words - a governance note says "governance gate,
not a confidence threshold" or "governance downgrade, not a confidence
downgrade", and never reuses the reliance-floor or ambiguity-band wording.

`route-result/v2` therefore carries a `governance[]` sidecar, emitted on every
route regardless of `--fields`, keyed by each candidate's own root-relative
`path`, with a `disposition` of `load` or `offer`:

```text
governance[2]{path,provisional,trusted,disposition}:
  ProductWiki/topics/release-process.md,false,true,load
  ReleaseWiki/INDEX.md,true,false,offer
```

Rows cover the emitted packet plus every provisional candidate a `load`
withheld from it, so a consumer reading structured fields alone still sees
which wikis were offered rather than loaded, exactly as `preflight` reports
them; `candidates[]` keeps carrying only paths the host may open. Withheld
offer rows are bounded by `max_candidates`, with a note naming anything
omitted. It is a sidecar rather than a candidate column so the default
candidate field set stays exactly what `route-result/v1` and `v2` already
promised; `provisional` is additionally available as an opt-in `--fields`
column. Catalog rows and preflight matches and offers carry `provisional`
directly, with the same meaning.

The emitted `thresholds` block makes every decision self-describing.
`unknown` is a first-class confidence value (no evidence to score): it is
emitted as the literal string, never as a fabricated number, and it never
meets the floor.

`--semantic` opts into the local char-ngram reranker on both `route` and
`preflight`. It only reorders candidates the lexical baseline already
surfaced and model-access filtering already authorized; it never changes
membership, thresholds, budgets, or access, and it runs fully offline. The
`semantic` block is typed: `disabled` (default), `ok`, `unavailable`, or
`error`, with a `reason` whenever it is not `ok`; every non-`ok` state
returns the untouched lexical order. Candidates additionally carry
`freshness` (`updated`, `age_days`, `stale`), computed only when `--today` is
passed and otherwise explicitly unknown.

`assess claim` scores one claim from `--source quality:origin` evidence
(`primary`, `synthesis`, `hypothesis`, `prior`; `--ineligible-source` counts
for nothing), `--lifecycle`, `--freshness`, and `--contradicted`; sources
derived from one origin count once, unresolved contradictions freeze the
claim below the floor, and stale or undated evidence can never reach it.
`assess answer --claim SCORE|unknown ...` caps an answer at its weakest
materially relied-upon claim. Both emit `megamind/confidence-report/v1` with
the full component rationale.

## The ten principles, applied

1. **Token-efficient output**: TOON by default; tables for candidate and
   finding lists; wiki page bodies are never dumped into output.
2. **Minimal default schemas**: route candidates default to
   `path,kind,score,reason`; `--fields` opts into
   `wiki,privacy,chars,confidence,freshness,semantic_score,provisional,reasons`.
   Doctor findings carry exactly `check,severity,path,message`.
3. **Content truncation**: evolve diffs are bounded to 60 lines
   (`diff_truncated`, `diff_lines_total`, `--full`), doctor findings to 50, and
   every other item list - review sections, catalog wikis, preflight matches
   and filtered entries, adopt file lists, gap journals, rollout status rows -
   to 20, each with an explicit note and `--full` to lift it. `gap list`
   additionally keeps the unbounded per-gap `attempts` history behind `--full`,
   reporting only `attempt_count` in a default row, and states the journal size
   in `total`. Notes themselves stay bounded: a note that reports dropped
   candidates names at most 5 of them and then states how many more there were,
   so a wide index cannot inflate the packet through `notes`.
4. **Pre-computed aggregates**: home returns proposal/review/doctor counts;
   review returns an `aggregates` object; route returns context-budget
   accounting; doctor returns error/warning counts.
5. **Definitive empty states**: `matched: false` with `candidates[0]`,
   `status: clean`, `status: healthy`, `status: duplicate`, and
   `status: noop` are structured successes, exit 0.
6. **Structured errors and exits**: `megamind/error/v1` with a stable `code`
   and sanitized message; exit 0 success/no-op, 1 operational failure
   (including doctor errors), 2 usage/config validation; no interactive
   prompts; unknown flags are rejected before any mutation.
7. **Explicit, local, zero-network integration**: `setup skill --dest DIR`
   installs the Agent Skill only where you point it; uninstall by deleting
   that directory. No hooks are installed implicitly; nothing touches shell
   or provider config; no command ever makes a network call.
8. **Content first**: no arguments returns the live local home: executable
   identity, one-sentence purpose, root, wikis, proposal/review/doctor
   aggregates, next actions. Never an argparse dump, never a mutation.
9. **Contextual disclosure**: every result, error, and no-op carries a small
   `help[]` of executable next commands with runtime values filled in (for
   example the exact `--apply --plan-id ...` command after a dry run).
   `--no-help-hints` suppresses it.
10. **Consistent help**: `--help` on every command is short and
    example-driven.

`evolve --apply` is a durable transaction. Its result additively carries
`pre_change_tree_sha256`, `applied_tree_sha256`, and `restored_tree_sha256`
over each controlled path but the proposal, plus `rolled_back` beside `applied`.
Replaying an interrupted apply resumes from the write-ahead record.
`evolve --rollback --plan-id` verifies every target before writing, refuses
foreign content or a stale/tampered token, restores the proposal and exact
pre-change controlled-tree hash, and returns `restored_tree_sha256` while
retaining append-only audit and transaction evidence. Apply, recovery, and
rollback are three states of one transaction and render one stable
`evolve-result/v1` key set: a field a state does not describe carries its
definitive empty value rather than disappearing. At a canonical wiki root,
`route`, `capture`, `review`, and `evolve` use the authoritative card directly;
`route` honors its card context budget, `evolve` defaults to and accepts only
destinations inside the card's compiled page tree (the directory of its
declared index) and rejects `raw/`, and `review` reports only compiled pages.

## Error codes

`usage_error`, `not_initialized`, `registry_invalid`, `card_invalid`,
`capture_invalid`, `proposal_not_found`, `plan_mismatch`, `approval_required`,
`evolve_invalid`, `adopt_invalid`, `init_invalid`, `path_escape`,
`frontmatter_invalid`, `io_error`, `garden_invalid`, `gap_not_found`,
`gap_transition_invalid`, `provision_recovery_required`, `evaluation_invalid`,
`rollout_invalid`. Malformed vault content and filesystem failures are reported
as `frontmatter_invalid` and `io_error` documents with exit 1; malformed frozen
evaluation inputs are `evaluation_invalid`; malformed, unsafe, or stale rollout
evidence is `rollout_invalid`; no invocation ever ends in a traceback.
`provision_recovery_required` is the one failure that deliberately leaves
durable state: an apply that could not fully undo itself keeps its transaction
record so the retry or the explicit rollback stays available. Messages never
include machine-specific absolute paths from inside the vault model; registry
paths are always root-relative.

## Evaluation contract

`bench run` invokes the public `preflight` and `route` commands over the
checked-in synthetic release fixture and emits tier-specific canonical
metrics, context accounting, safety counts, and honest local baselines. A
query set without a registered `megamind/benchmark-query-set/v1` header is
refused, and the emitted `benchmark_version` is the version that header
declares. Thresholds are preregistered against the exact corpus, query-set,
and task-set digests they gate, and `bench run`, `bench check`, and
`experiment plan` all refuse a missing, stale, or tampered binding. A route
candidate is only counted as loaded once preflight authorizes it for the
declared model class, for every model class.

Every gate is validated for key, type, finiteness, and range before anything
is scored, checked, or written, so a malformed threshold file is a typed
`evaluation_invalid` document rather than a coercion traceback. The measured
invocations inherit the caller's `PYTHONHASHSEED` instead of pinning it, and
`--repeat` reruns under a different explicit seed and requires a
byte-identical document.

`bench check` applies the versioned machine-readable gates; a tier with no
queries scores zero rather than crashing, so an absent tier fails closed.
`experiment keygen` writes the host's private 256-bit blinding key, creating
the file mode 0600 from its first syscall, refusing to overwrite an existing
key, and leaving no partial file behind on failure; it is the single command
that draws on OS entropy, and everything downstream stays deterministic once
the frozen key exists. `experiment plan` freezes a task-set digest,
prompt/model/tools/effort inputs, rubric, thresholds, execution seed, and
three isolated wiki snapshots, and emits three separate artifacts under that
key - a machine-generated 256-bit value supplied in its own mode-0600 file:
the plan, a grader packet carrying only blind identities, the rubric, and a
keyed commitment, and a host-only unblinding map holding the key, the
assignment, and the snapshot roots. Neither public artifact carries inputs
sufficient to derive the label-to-condition mapping, and no artifact outside
the map carries a raw snapshot digest, so the constant empty-tree hash
cannot identify the no-wiki arm. The host supplies opaque-label arm outputs;
`experiment validate` needs no map or key and rejects malformed, incomplete,
cross-arm, contaminated, or wrong-provenance outputs. `experiment score`
seals the blind per-label scores before it opens the unblinding artifact at
all, accepts the map only if it opens the plan's commitments under the key
the plan names, then applies only the frozen rubric and promotion rules. An
incomplete arm set is sealed the same way and returns a typed `unsettled`
document, never a traceback and never a promotion; the map is then opened
only to derive the roots an `--out` destination must stay outside of, so
with no destination to protect an unreadable map still yields `unsettled`,
while with one an unvalidated map is refused rather than written around. No
assignment ever reaches the result. `experiment record` appends a bounded
safe audit event with a non-empty rollback reference when needed. Every
evaluation output destination is refused when it resolves inside an
evaluated root - fixture, snapshot, arm output tree, or audit root - or
inside any vault, and every check runs before anything is written so a
refusal leaves nothing partially written. None of these commands invokes a
model, worker, network, account, or external service.

## Governed host rollout contract

`rollout promote` consumes a current card, matched and no-match
`preflight-result/v2` documents, typed host capability evidence, a promoted
`evaluation-score/v1`, doctor health, separate governance/access approval
references, and a complete nondecreasing prior-proof chain. Dry run emits
`rollout-plan/v1`; apply requires its exact `plan_id`. Only a ready plan creates
a loadable `host-wiki-promotion-proof/v1`. A blocked plan can be recorded but
stays `loadable: false`.

The proof binds one host, one wiki, one model class, the effective access from
`megamind.access`, the current card and catalog digests, sequence, prior proofs,
and privacy rank. It stores approval hashes, preflight ids, evaluation ids, and
doctor aggregates, never raw requests or filesystem roots. Provisional,
`none`, pointer, failed-evaluation, unhealthy, unapproved, or sequence-breaking
targets fail closed.

State is an explicit local directory outside every estate and vault. Promotion
and rollback are durable write-ahead transactions with exact replay and
foreign-content refusal. `rollout health` makes card/access/trust/doctor drift a
typed `rollback-required`, and `rollout rollback` disarms the local binding
while retaining the proof and a typed receipt. No command edits a card, wiki,
host configuration, provider, account, collaborator, repository, publication,
merge, or billing state. See [rollout.md](rollout.md).

## Testing the contract

`tests/test_cli.py` golden-tests TOON and JSON as renderings of the same
object, exit behavior, the no-args home, empty states, truncation, `--fields`,
`help[]` presence, and module execution outside the source tree.
`tests/test_skillpack.py` additionally asserts that the error codes and exit
semantics documented here, in the standalone skill's
`references/commands.md`, and raised by the package itself all agree. CI
additionally builds the wheel, installs it into a clean environment, and runs
`megamind-axi` from a scratch directory.

## Federation surfaces

`catalog` and `preflight` are read-only commands over one or more wiki roots
(`--root` for a single root, `--estate` for a directory of roots). Both are
catalog-level only: they read registry entries and wiki cards, never page
content. `catalog` aggregates every eligible card into a deterministic
projection (explicit broken/unreachable/stale/redacted entries, stable
ordering, a content-hashed `catalog_hash`, and a byte-stable human-readable
projection via `--emit-projection`, drift-checked with `--check-projection`).
`preflight` routes a substantive request under the host's declared model class
(`local` or `cloud`): wikis the class may not receive are filtered before any
path is returned, pointer wikis expose location metadata only, digest-only
wikis allow only their approved digest, and the deterministic `preflight_id`
binds the request hash, catalog snapshot, model class, and result so a host
can prove preflight ran without storing the raw request. An authorized match
whose follow-up hands out a load path also carries the card's numeric
`context_budget` exactly as declared; offers, filtered, withheld, broken,
pointer, and other outcomes told to load nothing never carry a budget. Match
and offer entries carry a bounded, privacy-safe `evidence` summary of the
routing class, numeric coverage, per-class signal counts, and card-only
provenance; it never includes request-derived tokens or page content, and
`reasons`, confidence, freshness, and semantic fallback semantics are
unchanged. [schemas.md](schemas.md) owns the packet's field-level shape and
its v2 compatibility notes. The route-confidence thresholds above decide the
status: a confident match carries per-match confidence, freshness, and this
evidence summary; below-floor matches become offers that expose no loadable
paths. Statuses are definitive: `matched`, `ambiguous`, `no-match`,
`unavailable`, and `privacy-filtered` are all structured successes with
exit 0.
