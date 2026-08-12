# ADR 0010: Host rollout is local proof activation

Status: accepted

## Decision

Megamind represents Phase 6 rollout as an external local ledger of typed
per-host/per-wiki proofs. It validates current card access, host capabilities,
matching and no-match preflight evidence, evaluation, doctor health, explicit
approval references, and an incremental privacy-order chain. A content-bound
plan must be applied before a proof becomes loadable. The host may consume that
proof, but Megamind never edits host or provider configuration.

Rollout state is isolated from every wiki and estate. Promotion and rollback
use durable write-ahead transactions, exact-byte replay, foreign-content
refusal, and retained receipts. Rollback disarms a binding without deleting its
proof or audit history. A provisional wiki, a `none` or pointer posture, a
failed evaluation, a missing approval, or a broken proof remains unloadable.

The proof records the exact effective access derived by `megamind.access`; no
rollout flag can request a wider surface. Every host, wiki, model class, and
access posture is a separate binding. Privacy ordering is a nondecreasing chain
of prior typed proofs rather than an all-at-once fleet switch.

## Consequences

The interface is provider-neutral and deterministic, and it can prove what was
locally authorized without gaining provider credentials or an external-action
adapter. Hosts continue to own mandatory invocation, workers, scheduling,
model calls, grading, research, budgets, publication, repositories, accounts,
collaborators, and merges.

A promotion proof is evidence, not a remote deployment. Operators must keep the
state directory local, use opaque identifiers when needed, nominate the full
authorized set before assigning sequence numbers, run health before use, and
stop consumption on any drift. A passing evaluation remains necessary but does
not override card governance or explicit approvals.
