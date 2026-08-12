# Governed host rollout

Phase 6 adds one provider-neutral interface for promoting a wiki surface to one
AI host: `megamind-axi rollout`. A promotion is a local typed proof. It is not a
deployment, host configuration edit, model call, worker launch, publication,
repository operation, account or collaborator change, merge, or purchase.
The host decides whether to consume a healthy proof and remains responsible for
preflight enforcement, scheduling, models, grading, research, and all external
actions.

## Evidence bundle

`rollout promote` validates one host/wiki binding from:

- a current authoritative card and its effective access from `megamind.access`;
- one matching `megamind/preflight-result/v2` and one definitive no-match result
  produced against the current catalog, for the exact model class. The no-match
  request must be a substantive one, with usable terms and a request hash
  distinct from the matched request: an empty or termless request is quiet about
  nothing and is refused as a negative control;
- a `megamind/host-rollout-evidence/v1` attestation that the host enforces
  mandatory preflight, privacy, quiet no-match behavior, task logging, failure
  disclosure, and local-only activation;
- a complete `megamind/evaluation-score/v1` with `status: promoted`, passed
  frozen gates, and zero privacy or model-access violations;
- a zero-error doctor check;
- separate non-empty governance and access approval references, stored only as
  hashes in the proof;
- a complete prior-proof chain for the declared sequence.

The command derives the access surface from the card. It cannot request a wider
one. A cloud `digest-only` card remains digest-only, a full card remains bounded
by its declared routing ladder and context budget, and `none`, pointer,
provisional, contradictory, unclassified, or otherwise unloadable postures fail
closed. An approval reference does not override a failed check.

The host evidence file is:

```json
{
  "schema": "megamind/host-rollout-evidence/v1",
  "host_id": "synthetic-host-a",
  "model_class": "cloud",
  "checks": {
    "mandatory_preflight": true,
    "privacy_enforcement": true,
    "quiet_no_match": true,
    "task_logging": true,
    "failure_disclosure": true,
    "local_only_activation": true
  }
}
```

Unknown fields are refused. Use an opaque, local host identifier if the host's
real identity is sensitive.

## Incremental privacy order

Promote exactly one host/wiki binding per plan. The proof carries a
deterministic `privacy_rank`, `sequence`, and the prior promotion ids. Every
prior proof must belong to the same host and model class, cover exactly
sequences `0..N-1`, remain loadable, and have a rank no higher than the target.
This enforces a nondecreasing proof chain. Operators must nominate the complete
authorized set before execution and assign lower-risk cards first; absent
lower-risk tiers are an operator governance fact, not something Megamind can
infer.

The ranks are restrictive: public reference is first; bounded digest access is
later; company and collaborative material later still; personal or
unclassified material is last among loadable surfaces. Pointer and `none`
postures are never loadable. Unknown sensitivity never becomes an early tier
merely because its current access is bounded.

Each new host, wiki, model class, sensitivity, or access posture needs its own
plan and approvals. A proof for one binding cannot authorize another.

## Plan, apply, health, rollback

A promotion is dry-run by default:

```sh
megamind-axi rollout promote \
  --state-root <external-local-state> --estate <estate> \
  --wiki-root <root> --wiki <name> --host-id <opaque-host> \
  --model-class local|cloud --host-evidence <file> \
  --preflight-evidence <matched.json> \
  --no-match-evidence <no-match.json> \
  --evaluation-evidence <score.json> \
  --governance-approval <reference> --access-approval <reference> \
  --sequence <n> [--prior-proof <proof.json> ...] --today YYYY-MM-DD
```

A ready plan returns a content-bound `plan_id`. Re-run the exact command with
`--apply --plan-id <id>`. A blocked plan cannot produce a loadable proof; an
apply of that exact blocked plan records a durable non-loadable outcome for
operator accounting.

Rollout state must be outside every estate and wiki root. Apply first durably
writes a transaction carrying the exact proof, then writes the immutable proof
and active projection, verifies their bytes, and marks the transaction applied.
A replay resumes a pending transaction or returns `noop` for the exact applied
bytes. Foreign content, stale plan ids, or a different active promotion refuse.
No wiki file is changed.

Before use and after any card or wiki change, run:

```sh
megamind-axi rollout health --state-root <state> \
  --wiki-root <root> --promotion-id <id>
```

Health requires the active binding, exact card digest, unchanged effective
access, non-provisional governance, and a zero-error doctor result. Any drift is
`rollback-required` and `loadable: false`.

Rollback is also plan-first:

```sh
megamind-axi rollout rollback --state-root <state> \
  --promotion-id <id> --reason "<safe summary>" --today YYYY-MM-DD
# then repeat with --apply --plan-id <id>
```

Rollback durably disarms the active binding and writes a typed receipt. It
retains the promotion proof and both transactions, copies only a hash of the
reason, changes no wiki or host configuration, and is interruption-resumable.

`rollout status` reports promoted, rolled-back, and blocked outcomes from this
local state. `active` is the live projection, one row per binding, so it answers
only what is armed right now. `rolled_back` projects the retained receipt ledger
and `counts.rolled_back` is taken from there, not from the live projection, so
every rollback stays visible even after its binding is armed again by a later
plan.

A rolled-back binding is disarmed, not retired. Promoting the same host and wiki
again requires a fresh plan with its own current evidence and its own separate
governance and access approvals; that plan supersedes the disarmed projection
while both the earlier proof and its receipt stay on disk. Replaying the
rolled-back plan itself does not re-arm anything: it refuses with a typed error
naming the rollback. A promotion that is still armed must be rolled back before
it can be replaced, and an unfinished promote transaction for the same binding
must be replayed or resolved before a different plan can take it over.

## What Phase 6 actually rolled out

The reusable interface, proof schemas, transaction recovery, health checks,
rollback receipts, privacy ordering, operator documentation, and synthetic
public-CLI tests are shipped. No real host/wiki binding was promoted by this
repository change. The preceding restricted pilot's value evaluation ended in
`rollback-required`, so its existing approved access surfaces remain exactly as
they were and its isolated candidate knowledge remains untrusted. No private
root, prompt, arm output, identity, key, or pilot artifact was copied into this
repository or into rollout state.

Everything beyond that already-approved restricted scope remains not onboarded.
A future nomination is blocked until its own card governance, model access,
host evidence, preflight/no-match proofs, passing evaluation, doctor health,
and separate approvals exist. This is a deliberate limitation, not a partial
fleet claim.
