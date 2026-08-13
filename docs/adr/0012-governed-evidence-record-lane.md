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
being marked resolvable, and an active claim requires resolvable support.  An
empty, zero-length, or out-of-range selector is refused outright rather than
resolving vacuously against any text.
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

A record's identity covers only part of its body, so immutability is scoped to
the identity-bearing fields.  The governed state Megamind itself derives - the
acceptance block, quotation resolution, claim lifecycle and support,
contradiction resolution, job state, candidate status - advances in place, and
any edit to an identity-bearing field of an existing record is refused.  This
is what lets a deferred artifact reach `accepted` once its spans resolve
without ever rewriting a frozen fact.

The frozen facts themselves are never in that derived set, corrections
included.  An artifact's identity is its canonical URL and normalized snapshot,
which a later retraction does not change, so a recheck that could edit
`corrections` in place would silently rewrite history and destroy the record of
what was true at retrieval time.  A recheck therefore appends a content-bound
`correction-notice/v1` superseding the previous notice, and the head of that
per-artifact chain is the posture G9 judges and claim confidence weighs.
Retracted support is removed the moment the notice is recorded, not when the
acceptance block is next re-derived.  A chain with no single head names no
posture, so it resolves to `unknown` - restrictive - and doctor reports it.

Records that reference each other are written only once every member of the set
is proven writable, and every reference is resolved against the store at
admission rather than only reported afterwards.  A reference that names two
records must be coherent across both: a claim's support may only pair a span
with the artifact that span was hash-bound to, because the hash binding would
otherwise stop at the quotation and let a host choose which artifact's
acceptance and correction posture a borrowed span earns.  Committing part of such a set,
or a record naming an identity this vault does not hold, would leave a vault
whose own doctor reports it broken, which is a state no supported command may
reach.  Doctor stays the owner of the same invariant for records that arrive by
other means, so the two never disagree about what a valid vault is.

## Consequences

Absent, malformed, or explicitly `"research": "off"` policy denies fresh
research.  Existing v1 registries and legacy research results remain readable
with restrictive semantics.  Doctor validates evidence and research journals; review surfaces
deferred records and unresolved contradictions.  Full offline replay depends
on host quarantine retention unless a rights-approved source is imported into
`raw/` by a separate owner action.
