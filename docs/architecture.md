# Architecture

Megamind is a small, dependency-free Python package behind the `megamind-axi`
executable. Every command runs locally and without network access, and every
command but `experiment keygen` is deterministic (see
[Determinism](#determinism) for that one exception). Internally everything is
plain typed Python objects; the CLI builds one typed document per invocation
and renders it as TOON or JSON only at the output boundary (see
[axi.md](axi.md)).

## Modules

| Module | Responsibility |
| --- | --- |
| `megamind.models` | Knowledge types, lifecycle states, privacy and access classes, frontmatter parse/serialize (deterministic YAML subset) |
| `megamind.fsops` | Path containment, atomic writes, backups, audit log, owner-only secret files. Every write goes through here |
| `megamind.registry` | `.megamind/registry.json` (schema v1/v2) load/save/validate/migrate; generated `ROUTER.md` projection |
| `megamind.access` | Model-access policy: derives and clamps the binding local/cloud access, routing mode, and catalog visibility for every card |
| `megamind.card` | The standalone `.megamind/wiki-card.json` of a canonical wiki root |
| `megamind.links` | Markdown link and Obsidian wikilink extraction, target encoding/decoding, and resolution |
| `megamind.routing` | The deterministic retrieval ladder |
| `megamind.confidence` | Route/claim/answer confidence rubrics, the 0.75 reliance floor, and the route thresholds |
| `megamind.semantic` | Optional local semantic reranking behind the lexical baseline, with typed fallbacks |
| `megamind.capture` | Proposal-first capture with dedupe and provenance |
| `megamind.evolve` | Evolution plans, unified diffs, approval-gated apply, new-wiki registration |
| `megamind.review` | Read-only gardening report |
| `megamind.doctor` | Read-only integrity validation |
| `megamind.scaffold` | Non-destructive `init`: registry vaults and canonical wiki roots |
| `megamind.adopt` | Non-destructive adoption of existing wiki directories, with rollback |
| `megamind.catalog` | The generated read-only fleet catalog and its drift-checked projection |
| `megamind.preflight` | Catalog-level, model-access-aware routing for substantive requests |
| `megamind.selection` | Evidence-bound explicit choice of one current loadable preflight offer or eligible existing wiki, with no confidence or access widening |
| `megamind.gardening` | Durable gaps, one-hop host plans, research bridge, safe event log, provisional local-wiki qualification |
| `megamind.policy` | Strict per-wiki research-policy/v1 parsing with restrictive absent defaults |
| `megamind.evidence` | Frozen evidence records, gate validation, hash-bound quotations, claims, contradictions |
| `megamind.research` | Network-free plan/job/packet/outcome receipt spine; never dispatches or fetches |
| `megamind.evaluation` | Frozen release benchmark over the public CLI, and the host-executed three-arm plan/validate/score/record contract. No model, worker, or network code |
| `megamind.rollout` | Provider-neutral per-host/per-wiki promotion proofs, privacy-order chains, local write-ahead activation, health, and rollback receipts. No host or provider adapter |
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
lowercase word extraction with an English-plus-German stopword list and plural
stripping: a final `s` is removed from tokens longer than three characters,
except for the exact conversational token `pros`, which must not become the
unrelated trigger `pro`. This preserves ordinary plural normalization such as
`logos` -> `logo` and `names` -> `name`. Before extraction the text is NFC
normalized and German umlauts fold to their transliterations (`ä` -> `ae`,
`ö` -> `oe`, `ü` -> `ue`, `ß` -> `ss`), so a card written either way meets a
query typed either way on one token. All weights are constants in
`routing.py`; changing them is a behavior change and needs test updates.

## Confidence and thresholds

`megamind.confidence` owns the three separate confidence kinds and the shared
0.75 reliance floor. Route confidence blends the strongest per-token signal
(triggers/keywords and index labels count 1.0, names 0.7, digests and index
targets 0.5, free text 0.3) with query-token coverage (weights 0.6/0.4), and
fixed thresholds decide the outcome: at least 0.75 loads automatically, 0.25
to 0.75 (or top candidates within the 0.05 ambiguity band) offers choices
without loading, and below 0.25 is a quiet no-match. In `preflight` only, a
candidate whose only evidence is free text (no trigger/keyword or name
signal) must also reach the 0.35 text-only offer floor
(`TEXT_ONLY_OFFER_FLOOR`, reported as `text_only_offer_floor` in the
preflight `thresholds` block) before it may be offered: text-only confidence
is 0.18 + 0.4 * coverage, so a short request that mostly matches a card's
text stays offerable while one stray shared word in a longer request can no
longer summon an offer picker. Also in `preflight` only, a sole candidate
with no rival above the offer floor loads at or above the 0.6 solo reliance
floor (`SOLO_RELIANCE_FLOOR`, reported as `solo_reliance_floor` in the
preflight `thresholds` block) even below 0.75, and only when its evidence is
corroborated: at least `SOLO_MIN_SIGNAL_TOKENS` (2) distinct signaling
tokens, or a matched name token, because naming a wiki is self-corroborating.
A provisional sole candidate never opts in - governance forbids its load -
and the in-vault route ladder deliberately does not opt in at all. The floor
is otherwise applied per
candidate, not just to the leader: `confidence.authorize` names exactly which
rows a decision covers, so a `load` never hands out authorization to a weaker
row riding behind a strong one - `route` omits it from the packet with a note
and `preflight` demotes it to an offer with no loadable paths. Claim confidence scores
one claim from its eligible sources by authority order (primary 0.9,
synthesis 0.7, hypothesis 0.5, prior 0.2), removes every source whose
host-supplied correction status is not `clean` instead of down-weighting it,
adds capped corroboration for derived `origin_id` values only (unknown
independence collapses to one origin), and applies deterministic caps:
unresolved contradictions (0.5), staleness (0.6), unknown freshness or
lifecycle (0.7), lifecycle state (proposed 0.5, shaky 0.6,
rejected/superseded 0.1). Answer confidence is the weakest materially
relied-upon claim. `unknown` is first-class everywhere: no evidence means no
number, and unknown never meets the floor. Every constant is pinned by
`tests/fixtures/confidence-calibration.json`; `megamind-axi assess
claim|answer` exposes the rubrics as typed documents.

## Semantic reranking

`--semantic` on `route` and `preflight` enables the local char-ngram
backend. It blends into the *ordering* only (0.4 semantic, 0.6 normalized
lexical), over candidates the lexical ladder already surfaced and access
filtering already authorized, using only text each candidate may already
expose (pointer candidates contribute none). Thresholds, membership, budgets,
and access never move. The layer fails typed: `disabled`, `ok`,
`unavailable`, or `error` with a reason, and every non-`ok` state returns the
untouched lexical order. The backend is a small protocol, so a future local
embedding adapter can plug in under the same constraints; cloud embeddings
and network access remain out of scope permanently.

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
yields a no-op. When the destination belongs to an existing wiki with a
present declared index, the plan also adds a missing relative Markdown link to
that page without changing card scope; when that wiki declares no index or the
declared index is missing, the plan notes that the page will not be
index-routable rather than inventing one. Apply first persists a durable
transaction over the exact old and new page and index bytes.
`evolve --rollback --plan-id` restores that pre-change compiled tree, keeps
the proposal and transaction evidence, and refuses before writing when any
target contains foreign content. Interrupted applies resume from the same
transaction. Supersession marks the old page `superseded` with a
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
without touching their content. The local `route`, `capture`, `review`, and
`evolve` surfaces adapt that one card into the same internal routing interface;
for `route`, its declared context budget replaces the registry defaults. The
card is rooted at `.`, but its compiled page tree is the directory of its
declared index: `wiki/` for a scaffolded root, the root itself for a wiki
adopted around a legacy hub page. `card.compiled_page_dir` derives that the
same way routing resolves index entries, so `review` and `evolve` reach exactly
the pages `route` can offer. Evolution in this shape defaults to and accepts
only compiled destinations, rejecting every `raw/` destination or supersession
target, and `review` reports only compiled pages; `raw/`, `AGENTS.md`, and any
hidden entry at any depth (`.megamind/` state included) are never pages under a
canonical root, whatever its card declares.

## Access policy

Every card resolves to an effective posture in `megamind.access`: sensitivity
(who may ever see it), per-axis model access (what a local or cloud context
may receive: `full`, `digest-only`, or `none`), routing mode, and catalog
visibility. Unset axes derive from the privacy class; unknown, unclassified,
broken, or unmigrated classifications derive restrictively (cloud `none`).
An explicit sensitivity is reconciled with the classification implied by
privacy, and the more restrictive classification wins. Explicit values that
contradict a sensitivity or privacy ceiling are clamped down and reported as
doctor `access` errors; the restrictive value always wins, and no host or later
routing layer can widen the decision. Personal-local privacy independently caps
cloud access at digest-only. A consistently classified company-private card may
still set an explicit cloud policy, including full access. Ceilings key on the
effective sensitivity, so leaving `sensitivity` unset or spelling a wider one
is never a way to escape a clamp.

## The fleet catalog and preflight

`catalog` aggregates the cards of separate roots (`--estate` discovery) into
one generated, read-only projection: stable ordering, a content-hashed
`catalog_hash`, explicit broken/unreachable/stale/redacted entries, and a
byte-stable human-readable rendering that `--check-projection` drift-checks
against the cards. Cards stay authoritative; the catalog is always a
projection and redaction happens at the projection boundary. Rows sort by
root and name, except wikis their card withholds entirely: those sort last,
ordered by a hash of their identity, so a withheld row's position leaks no
ranking. Page content is never read; page doctor and staleness maintenance
remain on the explicit `doctor` and `review` surfaces rather than the catalog
or preflight hot path.

`preflight` routes a substantive request at the catalog level under the host's
declared model class. Lexical card evidence only by default (triggers/keywords,
name, scope text; negative triggers decline explicitly); `--semantic` reranks
the authorized matches on the same card fields. Access filtering happens
before any path is returned and before any reranking, so a semantic pass can
never resurrect an ineligible wiki. The route-confidence thresholds decide
the result: a confident match states `matched` and gives exact per-wiki
follow-up commands with per-match confidence, freshness, the exact card
`context_budget`, and a bounded lexical/semantic evidence summary. That
summary reports routing class, numeric coverage, per-class signal counts,
and card-only provenance without request-derived tokens or page content;
`evidence.lexical_classes` is the fired subset of those counts and contains
only deterministic signal-class labels, while `reasons` keeps its literal
lexical strings. Only an authorized match whose follow-up hands out a load
path carries the budget; sub-floor or banded matches state `ambiguous` and
offer the same evidence as choices that carry neither a loadable path nor a
budget. `no-match` stays quiet; `unavailable` and `privacy-filtered` are
explicit. A deterministic `preflight_id` content hash binds the request hash,
catalog snapshot, model class, and result. Preflight never mutates a wiki,
never writes a host record, never calls a model, and never touches the
network; whether and when a host runs preflight is the host's own policy.

Explicit selection is a narrow module and pair of AXI commands, not a threshold
exception. `select-offer` accepts one exact original request/model
identity, its complete recorded preflight packet, and one wiki name. It
recomputes preflight against the current catalog, validates the complete packet
and proof identity, then re-runs effective access, visibility, provisional
trust, routing mode, root existence, artifact existence, and symlink-resolved
containment. Only an identity occurring exactly once in the validated original
`offers[]` can become authorized. The selected entry keeps its original
confidence, evidence, freshness, and reasons. User authority picks among
eligible offers; it does not create routing evidence or trust.

The selection module owns these invariants behind `select_offer` and
`select_existing` interfaces, so hosts never assemble paths. `select-existing`
derives and atomically consumes an exact-request, one-time picker list from the
current complete catalog. It delegates access derivation to
`megamind.access` through the current catalog and delegates the existing
follow-up ladder and budgets to preflight helpers.

`select-offer` derives its deterministic `selection_id` from the original
request/catalog/model/preflight identity, the selected offer, current card/root
facts, exact access and paths, budget, evidence, and provenance. Root facts are
hashed as card facts plus root-relative resolved paths, never absolute machine
paths or page bytes. `select-existing` instead issues its opaque one-time
`selection_id` before a choice, binding the request and complete catalog hashes,
model class, owner/session identity hashes, current date, home identity, and
the emitted eligible subset. Authorization then validates the chosen wiki
against those bound current facts before it exposes a reader surface.

## Governed autonomous gardening

Phase 3 keeps autonomy inside a local, approval-aware sandbox. `gardening.py`
only consumes host-supplied capacity and research facts. It emits bounded
nominations and typed pause/refusal outcomes; it has no scheduler, worker
launcher, quota client, network adapter, or provider dependency. A wave is one
hop: the direct gap and first-order related topics are planned together, while
deeper topics are deferred nominations.

Gap records use semantic identity hashes and an append-only snapshot journal.
Transitions are validated, replayable, and linked to audit and safe log events.
Research results are acceptance-filtered and become immutable-source ingest
proposals only through `research-result/v2`. Its acceptance block binds the
host-supplied derived `origin_id`, retrieval/publication dates with precision,
snapshot digests, rights/quotation posture, and correction status. A v1
result remains readable only as a restrictive legacy nomination: caller
`eligible` labels, source prose, quality, destination, and lifecycle never
become authority. Missing, unknown, malformed, contradictory, or retracted
facts are typed ineligible outcomes rather than down-weighted evidence, so
an unsupported claim stays unknown; the claim rubric above owns how a
derived `origin_id` corroborates and what a correction status does. Raw
sources are never written by Megamind. Provisional wiki creation
validates every qualification input and the complete registry plan before any
byte is written, refuses a canonical wiki root and a v1 registry rather than
creating an ambiguous root shape or migrating one silently, writes the canonical
structure and restrictive routing registration together, and marks trust false
until confidence coverage and evaluation pass. `provisional` is read back by
every consumer: catalog, route, and preflight surface it, and none of them ever
turns a provisional wiki into an authorized load.

## Evaluation

`megamind.evaluation` measures the shipped interfaces rather than its own
internals: the release benchmark shells out to the real `megamind-axi preflight`
and `route` for every frozen query and refuses to score an invocation that
exited non-zero or answered with an unexpected schema. Routing accuracy is
measured on what the ladder surfaced; context, canary, and access metrics are
measured only on what the declared model class actually authorizes, which the
`access` module re-derives independently as a cross-check. Thresholds are
preregistered against the corpus, query-set, and task-set digests they gate.

The three-arm contract is planning and arithmetic only. The host executes the
arms; Megamind freezes the inputs, blinds the conditions behind opaque labels
under the host's private blinding key, validates the returned outputs, seals
the blind scores before it unblinds, and appends a bounded safe audit event.
See `docs/evaluation.md` and ADR 0009.

## Governed host rollout

`megamind.rollout` consumes those existing contracts rather than creating a
control plane. One plan binds one host, wiki, model class, current effective
access, card/catalog identity, matched and no-match preflight proofs, host
capabilities, passing value evaluation, doctor health, separate approval
references, and a complete nondecreasing privacy-order proof chain. The access
module remains authoritative: rollout can record its result but cannot widen
it. Provisional, pointer, `none`, failed, unhealthy, unapproved, or stale
bindings remain unloadable.

Promotion writes only to an explicit local rollout state directory outside all
estates and vaults. A durable transaction precedes the immutable proof and
active projection; replay verifies exact bytes and resumes. Health checks card,
access, trust, and doctor drift. Rollback disarms the projection and retains its
proof, transaction, and typed receipt. There is no provider-specific adapter,
remote operation, host configuration edit, worker, scheduler, model, grader,
research client, account operation, merge, publication, or billing path. The
host owns consumption of the proof and every external action. See
`docs/rollout.md` and ADR 0010.

## Safety model

- `fsops.resolve_contained` resolves symlinks first and rejects any path that
  leaves the root: traversal (`..`), absolute paths, and symlinks pointing
  outside all fail closed.
- Writes are atomic (temp file + `os.replace`). Mutations of existing files
  first copy the old content to `.megamind/audit/backups/<name>.<hash>.bak`.
- Every mutating action inside a vault appends a JSON line to
  `.megamind/audit/log.jsonl`. Containment, backup, and audit are vault
  policies layered on top of the bare atomic-write primitive, so `setup skill`,
  which refuses destinations resolving inside a vault and writes only to an
  external destination, gets atomicity only. The evaluation surfaces use the
  same primitive, but only after refusing any destination that resolves inside
  an evaluated root or inside any vault. The blinding key is the exception:
  `fsops.create_private_file` creates it owner-only from its first syscall and
  refuses to replace an existing key, because a secret must never exist under
  broader permissions, not even between a write and a `chmod`.
- The registry stores only root-relative paths, so vaults stay portable and
  never leak machine-specific locations.
- Rollout state is external to every vault and estate. Proofs contain card and
  evidence identities but no roots or raw requests; approval references and
  rollback reasons are hashed. Promotion and rollback use durable local
  write-ahead transactions and never mutate a wiki or host configuration.
- `doctor` re-checks the invariants: containment, unsafe symlinks, router
  consistency, metadata validity, link integrity, proposal hygiene, registry
  schema version, access-policy contradictions, gap-journal integrity, and
  wiki-shaped directories that were never registered. It reports on both root
  shapes: a root that carries a card and no registry is validated as a
  canonical root (required directories, a named card, and the same gap
  journal) instead of failing as an uninitialized registry.

## Determinism

Commands avoid wall-clock dependence where it matters: proposal ids and plan
ids are content hashes, and `capture`, `evolve`, `route`, `review`, `catalog`,
`preflight`, `gap`, `research-wave`, `provision-wiki`, `rollout promote`,
`rollout rollback`, and the home view accept `--today` for reproducible date
handling in tests and benchmarks. `route`, `catalog`, `preflight`, and the
gardening and rollout surfaces go further and read no clock at all: without
`--today` freshness is simply reported as unknown rather than computed, and a
gap record, wave id, provisioning plan, promotion proof, or rollback receipt
keeps an empty date rather than inventing one. Semantic reranking is equally
deterministic: the char-ngram backend is a pure function of its inputs and
rerank ties keep the lexical order. The only non-deterministic outputs are audit
timestamps and `experiment keygen`, the one command that draws on OS entropy: a
blinding key must be unpredictable or the published commitment is enumerable.
Planning, validation, and scoring stay fully deterministic once that frozen key
exists.
