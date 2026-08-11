# ADR 0008: provisional governance narrows routing after the thresholds

Status: accepted (Phase 3)

## Context

A provisional wiki is knowledge Megamind created for itself: seeded from a
qualification check, not yet covered by confidence scoring, and not yet cleared
by an evaluation pass. Its card can still score well - the seed keywords were
chosen to match the demand that justified it - so lexical evidence alone would
happily hand a host an auto-load packet built entirely from unevaluated
material. Two tempting shortcuts were rejected: expressing provisional status
as an access restriction (it is a trust decision, not a privacy one, and
`megamind.access` must stay the single authority for access), and expressing it
as a confidence penalty (which would corrupt the calibrated rubrics of ADR 0004
and lie about the evidence).

## Decision

Provisional status is a governance gate that runs after the confidence
thresholds and may only narrow them. In `route`, provisional candidates are
removed from a `load` packet; if nothing trusted remains, the decision degrades
to `offer` and `governance_downgrade` is set. In `preflight`, a provisional
wiki stays an offer with no loadable path. Consequently an `offer` can be
returned at or above the 0.75 reliance floor - the one case where the decision
is not a threshold decision - so `notes` names it as a governance downgrade and
never as a confidence one, and a `governance[]` sidecar states `provisional`
and `trusted` for every emitted candidate regardless of `--fields`. The gate
never widens anything: it cannot promote a filtered or sub-floor wiki, and it
runs before semantic reranking can reorder the packet. The projected fields are
specified in `docs/schemas.md`.

## Consequences

- Autonomy can create knowledge without that knowledge silently becoming
  trusted; promotion out of `provisional` stays a deliberate card edit.
- Consumers must not infer trust from confidence alone. The sidecar exists so
  no host has to re-derive the posture, and it is emitted for every candidate,
  not only withheld ones.
- `route` and `preflight` now have a decision path where confidence and
  decision disagree; the notes carry the explanation and tests pin both the
  wording's cause and the fact that the load path stays empty.
- Access derivation is untouched, so ADR 0001 keeps its single authority and
  the two mechanisms cannot be confused for one another.
