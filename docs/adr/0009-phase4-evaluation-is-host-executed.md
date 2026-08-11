# ADR 0009: Phase 4 evaluation is host-executed and provider-neutral

Status: accepted

## Decision

Megamind owns frozen synthetic release benchmarking and the deterministic plan,
validation, scoring, and audit contract for three-arm value evaluations. The
host owns model execution and supplies outputs for opaque no-wiki,
current-wiki, and updated-wiki arms. Megamind never invokes a model, worker,
network, account, or external service.

All inputs are versioned and digested before scoring, and every threshold file
is preregistered against the exact corpus, query-set, and task-set digests it
gates, so a missing, stale, or tampered identity is refused rather than scored.
Arm snapshots, sessions, caches, writable output roots, and labels are isolated.
Blinding is a three-artifact split under a host-supplied private key: the plan
and the grader packet carry blind labels and a keyed commitment but never the
key, a condition beside a label, or a snapshot digest tied to one, and the
unblinding map lives with the host execution record. Conditions are assigned by
an HMAC of the frozen public identity under that key, so identical public
inputs and the same key reproduce the assignment while the public artifacts
hold nothing sufficient to derive it. Scoring seals the blind per-label scores
before it reads the map, and accepts the map only if it opens the commitment.

Validation fails closed on tampering, missing provenance, malformed context
accounting, leakage, privacy/access violations, or cross-arm contamination.
Promotion is a typed result requiring target improvement, no material adjacent
regression, provenance within the preregistered tolerance, and zero new safety
violations. Failed results require rollback; incomplete results are unsettled
typed documents, never tracebacks and never promotions. Audit events are
append-only and contain only bounded safe summaries. Evaluation evidence is
written outside every evaluated root and outside every vault.

## Consequences

The benchmark measures the real public CLI and publishes per-tier synthetic
evidence plus honest local baselines. Routing accuracy and authorized context
are reported as separate quantities: a candidate the ladder surfaced is only
counted as loaded once preflight authorizes it for the declared model class, so
the canary, context-budget, and model-access numbers describe what a model
could actually have received rather than what routing mentioned. Canary pages
in the fixture are deliberately unindexed, which is what lets the safety gates
fail rather than pass by construction, and what makes the baseline contrast
real. The benchmark does not claim model quality or replace a host's execution
and grading controls. Provisional wikis remain untrusted until a complete pass
and a separate governed card change.
