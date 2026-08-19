# ADR 0015: a solo reliance floor for sole corroborated preflight candidates

Status: accepted (amends ADR 0004)

## Context

ADR 0004 fixed the route thresholds: load at or above the 0.75 reliance
floor, offer below it. The live pilot showed what that costs at the estate
level: sole correct candidates scored 0.61-0.67 on ordinary conversational
requests - route confidence blends signal strength with query-token
coverage, and conversational phrasing dilutes coverage - so preflight parked
them behind a one-option offer picker that the captain confirmed every time,
even for requests naming the wiki outright. A picker that presents no
genuine choice is pure friction, and it does not scale to a many-wiki
estate. Simply lowering the load threshold for sole candidates was rejected:
one stray trigger token alone already scores 0.6, and a single shared word
in a long request must keep staying quiet, so plain confidence cannot be the
sole criterion.

## Decision

`decide()` and `authorize()` in `megamind.confidence` gain an opt-in
`solo_floor` parameter, the one exception to the reliance floor: when the
caller passes it and exactly one candidate is in play with no rival above
the offer floor, that sole candidate loads at or above
`SOLO_RELIANCE_FLOOR` (0.6). The corroboration requirement guarding the
opt-in belongs to the caller. `preflight` is the only caller that opts in,
and only for corroborated evidence: at least `SOLO_MIN_SIGNAL_TOKENS` (2)
distinct signaling tokens, or a matched name token, because naming a wiki is
self-corroborating. A provisional sole candidate never opts in - governance
(ADR 0008) forbids its load, so a solo load would only downgrade back into
the picker this floor exists to dissolve. A text-only candidate cannot reach
the floor by construction: text-only confidence tops out at 0.58. The
in-vault route ladder deliberately does not opt in.

## Consequences

- A preflight `matched` result can now carry `confidence.meets_floor: false`;
  a note names the solo reliance floor as the cause, so the decision stays
  self-describing.
- The preflight `thresholds` block emits `solo_reliance_floor`, making the
  exception part of the reproducible threshold contract; the constants live
  in `megamind.confidence` and changing them is a behavior change that needs
  test updates.
- A below-floor load is Megamind's own decision, not a host inference: hosts
  read it from the typed result and never re-derive it from raw confidence.
- Genuine multi-candidate ambiguity still offers, sub-floor evidence still
  stays quiet, and the reliance floor's meaning for claims and answers is
  untouched.
