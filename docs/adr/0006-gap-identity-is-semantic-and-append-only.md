# ADR 0006: gap records are keyed by semantic identity, append-only

Status: accepted (Phase 3)

## Context

Autonomous gardening needs a durable memory of what is missing, weak, stale,
or contradictory, and that memory outlives any single session. The failure
modes are all memory failures: the same gap reported twice under two ids so
priority and cooldown split; a resolved or rejected gap silently reopened by an
omitted flag; a crash mid-write leaving a half-written record that makes the
whole store unreadable; and a replayed host event creating a second copy of
work that was already done. Content-derived identity (comparing page bodies)
was rejected because it would make identity depend on the very content the gap
says is absent.

## Decision

A gap's identity is a content hash of its normalized wiki, topic, and kind
alone - never page content - and `gap_id` must equal that identity, so a
record whose fields were edited out of agreement with its id is a typed
`garden_invalid` error rather than a silently accepted duplicate. Records live
in `.megamind/gaps.jsonl` as an append-only snapshot journal: every mutation
appends a whole record, the latest line for an id wins, and earlier lines are
retained as recoverable history. Each append is one atomic rewrite, so a crash
can never leave a torn line. Lifecycle is an explicit validated transition
table, and `gap transition` always requires an explicit `--status`, so no
omitted flag can imply a mutation. `doctor` validates the journal in both root
shapes and reports an unusable one as a finding instead of raising. The field
set, the retained history, and the transition rules are specified in
`docs/schemas.md`.

## Consequences

- Repeated reports of the same gap coalesce deterministically, so priority,
  attempts, and cooldown accumulate on one record instead of fragmenting.
- Replaying a host event is idempotent by construction: `create` on an
  existing identity returns the existing record and appends nothing.
- The journal grows monotonically. That is the intended trade: mutation
  history is what makes crash recovery and audit possible, and only the latest
  snapshot per id is ever authoritative.
- Changing the identity inputs is a breaking change - existing journals would
  re-key and stop coalescing - so wiki, topic, and kind are effectively frozen.
