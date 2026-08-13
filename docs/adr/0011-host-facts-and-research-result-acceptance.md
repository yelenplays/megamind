# ADR 0011: host facts are evidence inputs, not acceptance verdicts

- Status: accepted
- Date: 2026-08-13

## Context

Megamind is the deterministic, network-free engine. A host may retrieve a
source and report typed facts, but source bytes and source prose are untrusted.
The former `research-result/v1` accepted a caller boolean, which allowed a
retracted source, copied URLs, or instruction-shaped text to enter the durable
proposal path.

## Decision

`research-result/v2` is the only result schema that can create an ingest
proposal. Each accepted source must carry a validated acceptance block binding:

- an independently derived `origin_id`, separate from display `origin`;
- retrieval and publication dates with precision;
- snapshot and normalized snapshot SHA-256 digests;
- license, quotation, and snapshot posture; and
- a host-supplied correction check whose status is clean.

Megamind derives eligibility from those facts. Missing, unknown where a gate
needs certainty, malformed, contradictory, non-clean, or retracted facts are
typed ineligible outcomes. Contradiction is checked across facts as well as
within one, so a publication whose earliest possible day falls after the exact
retrieval date is refused. A retracted source is removed from support rather
than down-weighted. Unknown independence collapses to one corroboration origin.

Both derived facts are stated at the executable boundary rather than inferred
below it: `assess claim --source quality:origin[::origin_id[::status]]` is the
only way a caller declares independence and correction posture, and the origin
string never becomes either one. `megamind.confidence` owns that vocabulary and
the corroboration bucket, so acceptance validation and scoring cannot drift.
Acceptance strings that reach the durable proposal are bounded and redacted at
the same projection boundary as origins and summaries.

v1 remains readable as a restrictive legacy nomination. Its `eligible` field
has no evidence, quality, rights, lifecycle, destination, or future
autonomous-apply authority. Source text cannot set behavior-changing fields.
Exact replay remains idempotent and divergent replay refuses.

## Consequences

The host owns retrieval, correction/retraction checks, and derivation of typed
facts. Megamind performs no network call and keeps the existing confidence
constants. Durable v2 proposals are inspectable and content-bound, while
legacy results cannot silently gain authority.
