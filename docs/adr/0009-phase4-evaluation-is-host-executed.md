# ADR 0009: Phase 4 evaluation is host-executed and provider-neutral

Status: accepted

## Decision

Megamind owns frozen synthetic release benchmarking and the deterministic plan,
validation, scoring, and audit contract for three-arm value evaluations. The
host owns model execution and supplies outputs for opaque no-wiki,
current-wiki, and updated-wiki arms. Megamind never invokes a model, worker,
network, account, or external service.

All inputs are versioned and digested before scoring. Arm snapshots, sessions,
caches, writable output roots, and labels are isolated. Validation fails closed
on tampering, missing provenance, malformed context accounting, leakage,
privacy/access violations, or cross-arm contamination. Promotion is a typed
result requiring target improvement, no material adjacent regression, preserved
provenance, and zero new safety violations. Failed results require rollback;
incomplete results are unsettled. Audit events are append-only and contain only
bounded safe summaries.

## Consequences

The benchmark measures the real public CLI and publishes per-tier synthetic
evidence plus honest local baselines. It does not claim model quality or
replace a host's execution and grading controls. Provisional wikis remain
untrusted until a complete pass and a separate governed card change.
