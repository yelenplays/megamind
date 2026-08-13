# ADR 0012: governed evidence-record lane

- Status: accepted
- Date: 2026-08-13

## Context

The host may discover and retrieve external material, but Megamind is a
network-free deterministic engine.  A caller-supplied eligibility label is not
an evidence decision: it can admit retracted material, copied origins, or
instruction-shaped source text.  Evidence must also remain usable after the
retrieval turn without placing full source bodies in a wiki.

## Decision

Slice 1 adds a restrictive per-wiki `megamind/research-policy/v1` and strict
validators for `evidence-record/v1`, `quotation/v1`, `claim/v1`, and
`contradiction/v1`.  Unknown fields and unknown gate verdicts are refused;
unknown is never pass.  Acceptance is derived from frozen typed facts and
common/source-class gates, never from fetched prose or a model label.

Evidence records bind canonical identity, a derived `origin_id`, dates,
rights, snapshot hashes, corrections, and a typed acceptance block.  A
retracted source is removed from support.  A blocked source is deferred and
visible.  Injection scans are advisory; capability isolation is the control.
Full bytes stay in host quarantine, while hashes, gate results, and permitted
quotations are content-addressed under `.megamind/evidence/` through `fsops`.

Quotations use Web Annotation-style exact and positional selectors against the
normalized snapshot hash.  They are re-resolved against frozen text before
being marked resolvable, and an active claim requires resolvable support.
Claims retain qualifiers and source references.  Contradictions retain both
claims and resolve only through typed scope, supersession, retraction, or
wiki-policy precedence; unresolved conflicts are visible and capped by the
existing confidence rubric.  No confidence constant or routing weight
changes.

The host supplies job, discovery, retrieval, and extraction receipts.  The
local research job spine stores plans, jobs, packets, and terminal outcomes,
but never dispatches, schedules, fetches, or publishes.  Packets are cited
synthesis and are not answer context.  The existing proposal/evolve boundary
remains the only mutation path.

## Consequences

Absent or malformed research policy denies fresh research.  Existing v1
registries and legacy research results remain readable with restrictive
semantics.  Doctor validates evidence and research journals; review surfaces
deferred records and unresolved contradictions.  Full offline replay depends
on host quarantine retention unless a rights-approved source is imported into
`raw/` by a separate owner action.
