# ADR 0001: access classifications default restrictively and clamp downward

Status: accepted (Phase 1)

## Context

Wiki cards declare who may see a wiki and what a local or cloud model context
may receive. Cards can be missing fields, stale after a schema migration,
hand-edited into contradiction, or outright malformed. The failure mode that
matters is silent overexposure: a private wiki handed to a cloud model because
a field was absent or mistyped.

## Decision

Every unset access axis derives from the legacy privacy class; anything
unknown, unclassified, broken, or unmigrated resolves to the most restrictive
honest value (cloud `none`; personal wikis never broader than cloud
`digest-only`; public reference may default to cloud `full`). Explicit card
values that contradict a sensitivity or privacy ceiling are clamped downward
at read time and reported as doctor errors, instead of failing to load. The
clamp lives in one module (`megamind.access`); no host, router, or later
semantic layer may widen it.

## Consequences

- A v1 registry keeps working and becomes safer without edits; doctor nudges
  the owner toward `migrate` and explicit policies.
- A contradictory card still routes, but only at the restrictive level, and
  the contradiction is visible in doctor and the catalog.
- Load-time validation stays structural; policy problems never make a whole
  registry unreadable, which is what lets the catalog report them explicitly.
- Widening access always requires a deliberate card edit; that is the point,
  and it is hard to reverse casually.
