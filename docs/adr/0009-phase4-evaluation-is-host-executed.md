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
Blinding is a three-artifact split under a host-supplied private key - a
machine-generated 256-bit value in its own mode-0600 file, because the
published commitment over a six-permutation space makes a guessable key
worthless. The plan and the grader packet carry blind labels and keyed
commitments but never the key, a condition beside a label, or any raw snapshot
digest; the unblinding map lives with the host execution record. Raw digests
are replaced by opaque per-arm commitments precisely because the empty no-wiki
tree hashes to a key-free constant that would otherwise name that condition in
every artifact carrying it, including the arm outputs a grader reads.
Conditions are assigned by an HMAC of the frozen public identity under that
key, so identical public inputs and the same key reproduce the assignment while
the public artifacts hold nothing sufficient to derive it. Scoring seals the
blind per-label scores before it opens the map at all, and then accepts the map
only if it opens every commitment under the key the plan names.

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
