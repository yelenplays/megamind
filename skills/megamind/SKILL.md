---
name: megamind
description: Route questions to the right Markdown wiki pages and garden knowledge safely with the megamind-axi executable. Use when the user asks where knowledge lives in their vault or wiki, wants to capture a note or decision from a conversation, asks to update or supersede outdated wiki pages, wants duplicates, stale pages, or dead links found, wants gardening of knowledge gaps (recording a missing, weak, stale, or contradictory gap, planning a research wave over one, or filing a host research result), asks about provisional wikis or wants a qualified new local wiki provisioned, asks to validate a Megamind vault, or needs to plan, prove, health-check, or roll back a governed host/wiki rollout. Works locally on Markdown and Obsidian vaults with no embeddings or network.
---

# Megamind

Drive everything through `megamind-axi`. Every invocation prints exactly one
typed TOON document on stdout (add `--format json` for JSON of the same
object); each document carries a `help[]` of ready-to-run next commands, so
follow those over guessing flags. Running `megamind-axi` with no arguments
shows the vault home: wikis, proposal counts, review and doctor aggregates.

Megamind never publishes automatically: capture writes proposal drafts, and
changes to existing knowledge require the plan id from a dry run as an
approval token.

## Find knowledge

Run `megamind-axi route "<question>"`. The `decision` field is authoritative:
`load` means route confidence reached the 0.75 reliance floor, so open the
returned candidate paths, best first; `offer` means weaker or ambiguous
evidence, or a confident match that is only `provisional`, so present the
candidates as choices and load nothing until one is picked; `no-match` is
definitive, so say so instead of guessing. The `governance[]` sidecar names
which candidates are provisional and whether each is `load` or `offer`,
including provisional wikis withheld from a load, and `notes` states whether a
downgrade was a governance or a confidence decision. Candidates
of kind `pointer` must be opened manually and never quoted; kind `digest`
exposes only summary content. `--semantic` reranks the surfaced candidates
locally when phrasing is indirect; check the `semantic.status` field and fall
back to the lexical order whenever it is not `ok`.

## Score confidence

Route, claim, and answer confidence are separate, and 0.75 is the reliance
floor for all three. Use `megamind-axi assess claim --source
<quality>:<origin>[::<origin_id>[::<status>]] [--lifecycle active]
[--freshness fresh] [--contradicted]` to score one claim from its evidence
(only a declared `origin_id` corroborates and sources sharing one count once,
an omitted identity is unknown independence and never corroborates, a
correction status other than `clean` removes the source, and contradictions
and stale or undated evidence stay below the floor), and
`megamind-axi assess answer --claim <score|unknown> ...` to cap an answer at
its weakest relied-upon claim. `unknown` is a definitive state, never a
number to work around.

## Capture knowledge

When a conversation produces a durable fact, decision, hypothesis, procedure,
example, or guidance:

```sh
megamind-axi capture --text "<the knowledge>" --type <type> --source "<origin>"
```

This writes a private proposal draft with provenance and a suggested
destination. It never touches wiki pages, so capture liberally; identical
content dedupes (`status: duplicate` is a success, not an error).

## Evolve knowledge (requires approval)

1. `megamind-axi evolve <proposal-id>` prints the plan with a bounded diff
   and a `plan_id`. Nothing changes. Use `--full` if `diff_truncated: true`.
2. Show the human the diff. Only after they approve, run the apply command
   from `help[]` (it carries `--apply --plan-id <plan_id>`).
3. To replace an outdated page instead of appending, add
   `--supersedes <old-page.md>`; the old page is marked superseded, not deleted.
4. Apply persists a durable transaction. If validation requires undoing it, run
   `megamind-axi evolve <proposal-id> --rollback --plan-id <plan-id>`; rollback
   refuses foreign content and retains the proposal and audit evidence.
5. If `creates_new_wiki: true`, stop and ask the human explicitly; only then
   add `--approve-new-wiki`.

Never edit `.megamind/registry.json` or apply plans without an explicit human
go-ahead. At a canonical wiki root, these commands use the authoritative card
directly and evolve may write only compiled content, never `raw/`.

## Maintain the vault

- `megamind-axi review` reports open proposals, duplicates, stale pages, dead
  links, and promotion candidates; `status: clean` means nothing needs work.
- `megamind-axi doctor` validates the vault; exit 1 means real errors and the
  `findings` table names each offending file. Run it after any approved change.
- `megamind-axi migrate` upgrades a v1 registry to schema v2 (access-policy
  card fields with restrictive defaults) when doctor suggests it.

## Work across wikis (catalog and preflight)

Wikis can live in separate roots; each root's card is authoritative and the
fleet catalog is a generated projection of those cards.

- `megamind-axi catalog --estate <dir>` lists every wiki with scope, owners,
  sensitivity, effective model access, and content-free maintenance state.
  Broken or redacted entries are stated explicitly, never silently omitted.
- `megamind-axi preflight "<request>" --estate <dir> --model-class local|cloud`
  routes a substantive request at the catalog level. It returns card-level
  matches and exact follow-up commands only when route confidence reaches the
  reliance floor; below it you get `ambiguous` offers (no loadable paths) or
  a quiet `no-match`. It never reads or returns compiled page content, never
  writes anything, and honors each card's access policy. A `none` wiki appears
  under `filtered`, a pointer wiki yields location metadata only, and
  `preflight_id` is the proof the consultation ran. Treat `no-match` as
  definitive: stay quiet about wikis instead of guessing.
- When the user explicitly chooses one current `offers[]` wiki, use the
  complete original JSON packet and exact original request with
  `megamind-axi select-offer <Wiki> --request "<request>" --preflight-result
  <file> --model-class local|cloud` against the same root or estate. Follow the
  returned selected load path only on `status: authorized`. Never substitute a
  rephrased request, handcraft a path, or use selection to override filtered,
  provisional, pointer, broken, stale, or model-incompatible knowledge.
- `megamind-axi adopt <dir>` brings an existing wiki directory under Megamind
  without touching its pages: dry run first, apply with the `plan_id` only
  after human approval, and `--rollback` removes exactly what apply created.

## Track gaps and host research (never dispatch work)

Megamind stores gardening facts and emits plans; the host owns dispatch,
research, model choice, quotas, and cost.

- `megamind-axi gap create --wiki <W> --topic "<t>" --kind <k>` records a
  durable gap for `missing`, `weak`, `stale`, or `contradictory` coverage;
  `gap list`, `gap transition <id> --status <s>`, and `gap attempt <id>
  --outcome "<o>"` keep its lifecycle. `transition` always needs an explicit
  `--status`; repeating the status a gap already holds is a no-op, and
  `gap list` shows 20 rows without attempt histories until `--full`.
- `megamind-axi research-wave <gap-id>` plans one hop from the capacity facts
  you pass in. A `paused` or `refused` status is final: report it, never work
  around it, and never launch a worker or call a quota tool on its behalf.
- `megamind-axi research-result --nomination-json <j> --result-json <j>` turns
  a `megamind/research-result/v2` document into an immutable-source ingest
  proposal. Each source needs an `acceptance` block of typed host facts you
  derived yourself (`origin_id`, dated `retrieval`/`publication`, snapshot
  digests, rights, and a clean correction check); a `v1` result and its
  `eligible` flag are legacy input only and can never propose a source.
  Megamind fetches nothing and never writes `raw/`.
- `megamind-axi provision-wiki <Name> <path> ...` creates a local wiki only
  when every qualification criterion is supplied. Without `--apply` it only
  plans and returns a `plan_id`; show that plan to the human and apply it with
  `--apply --plan-id <id>`. Re-running the same apply is a no-op, and
  `--rollback --plan-id <id>` undoes it. The new wiki stays `provisional`:
  offer it, never load it, until it is evaluated.

## Govern host rollout with local proofs

`megamind-axi rollout` records evidence, not a deployment. For one host/wiki
binding, `rollout promote` requires current card access, matched and no-match
preflights (the no-match one from a real request with usable terms that still
matched nothing), typed host enforcement checks, a promoted value evaluation,
doctor health, separate governance/access approval references, and the complete
nondecreasing prior-proof chain. Plan first; only re-run with `--apply
--plan-id <id>` when every typed check passed. Never override a blocked plan.

A healthy proof authorizes only the exact card-derived model access and changes
no host configuration. Run `rollout health` before consumption and after card
or wiki changes. On drift or failure, stop consumption and use plan-first
`rollout rollback`; it disarms the local binding and retains a typed receipt.
Re-arming that binding later needs a fresh plan with its own evidence and
approvals, never a replay of the rolled-back one. Provisional, `none`, pointer,
failed-evaluation, or unapproved targets remain unloadable. The host still owns
preflight invocation, workers, scheduling, models, grading, research,
publication, repositories, accounts, collaborators, merges, and spending.

## References

- [commands.md](references/commands.md) - full flag reference, schemas, exits
- [concepts.md](references/concepts.md) - types, lifecycle, privacy classes
