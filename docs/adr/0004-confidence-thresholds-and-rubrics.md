# ADR 0004: route confidence thresholds and separate claim/answer rubrics

Status: accepted (Phase 2)

## Context

Hosts need to know when a route may load automatically and when evidence is
strong enough to rely on. Raw lexical scores (integer weights) cannot answer
that: they scale with query length and signal class, so no fixed cut-off is
meaningful. The plan fixes a 0.75 reliance floor for routes, claims, and
answers, and demands that `unknown` stay first-class instead of being
fabricated into a score.

## Decision

`megamind.confidence` owns all rubrics as fixed constants, pinned by
calibration fixtures (`tests/fixtures/confidence-calibration.json`). Route
confidence normalizes lexical evidence into [0, 1] by blending the strongest
per-token signal class with query-token coverage; fixed thresholds decide the
outcome: at least 0.75 loads automatically, 0.25 to 0.75 (or top candidates
inside the 0.05 ambiguity band) offers choices without loading, below 0.25 is
a quiet no-match. Claim confidence starts from the strongest eligible source
by authority order (primary, synthesis, hypothesis, prior), adds capped
corroboration for independent origins only (sources derived from one origin
count once), and is capped deterministically by lifecycle state, staleness,
unknown freshness, and unresolved contradictions (which freeze a claim below
the floor). Answer confidence is exactly the weakest materially relied-upon
claim. When there is no evidence to score, the score is `unknown`: never 0,
never a guess, and never floor-meeting.

## Consequences

- Every threshold decision is reproducible and self-describing (`thresholds`
  block in the v2 retrieval documents); changing a constant is a behavior
  change that fails the calibration fixtures.
- Contradictory, stale, or undated evidence can never be silently relied
  upon; it stays offer-, hypothesis-, or raw-material-grade until an owner
  resolves it.
- Route, claim, and answer confidence can never blur into one number, so a
  confident route cannot launder a weak claim into a confident answer.
