"""Deterministic confidence rubrics for routes, claims, and answers.

The three confidence kinds stay separate: route confidence says how well a
wiki or artifact matches a request, claim confidence says how strongly the
evidence supports a single claim, and answer confidence caps a delivered
answer at its weakest materially relied-upon claim. All rubrics are fixed
constants with pinned calibration fixtures (tests/fixtures); changing them is
a behavior change and needs test updates, exactly like the routing weights.

`unknown` is first-class: when there is no evidence to score, the score is
``None`` and renders as ``"unknown"`` in output. It is never fabricated into
0.0 or any other number, and it never meets the reliance floor.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The accepted reliance floor: at or above it a route may load automatically,
# a claim may become active factual knowledge, and an answer may be delivered
# without a warning. Below it, evidence stays an offer, a hypothesis, or raw
# material.
RELIANCE_FLOOR = 0.75

# Below this, route evidence is treated as no-match: nothing is offered or
# loaded, however weakly it scored. At or above it (but below the reliance
# floor) the route offers choices without loading content.
OFFER_FLOOR = 0.25

# Top candidates whose confidences differ by less than this band are
# ambiguous: offer a choice instead of picking one.
AMBIGUITY_BAND = 0.05

# Route confidence blends the strongest single-token signal with the share of
# query tokens that signaled at all.
SIGNAL_WEIGHT = 0.6
COVERAGE_WEIGHT = 0.4

# Strongest-signal fractions by evidence class. Triggers/keywords and index
# labels are declared routing signals, so they carry the full token; free text
# (descriptions, scope, card bodies) carries the least.
SIGNAL_STRENGTH = {
    "trigger": 1.0,
    "index-label": 1.0,
    "name": 0.7,
    "digest": 0.5,
    "index-target": 0.5,
    "text": 0.3,
}

# Claim confidence: base score by source quality, in the plan's authority
# order (current eligible primary evidence, then curated synthesis supported
# by that evidence, then labeled hypotheses/observations, then unsupported
# model priors).
QUALITY_BASE = {
    "primary": 0.9,
    "synthesis": 0.7,
    "hypothesis": 0.5,
    "prior": 0.2,
}
SOURCE_QUALITIES: tuple[str, ...] = tuple(QUALITY_BASE)

# Only primary and synthesis sources corroborate; hypotheses and priors never
# lift a claim. Independent origins beyond the first add a fixed step, capped,
# and unknown origin identities collapse to one origin.
CORROBORATING_QUALITIES = ("primary", "synthesis")
CORROBORATION_STEP = 0.05
CORROBORATION_CAP = 0.10

# Deterministic caps. An unresolved contradiction freezes a claim below the
# reliance floor; stale or undated evidence can never be relied upon either.
CAP_CONTRADICTION = 0.5
CAP_STALE = 0.6
CAP_FRESHNESS_UNKNOWN = 0.7
LIFECYCLE_CAP = {
    "proposed": 0.5,
    "shaky": 0.6,
    "rejected": 0.1,
    "superseded": 0.1,
    "confirmed": 1.0,
    "active": 1.0,
}
CAP_LIFECYCLE_UNKNOWN = 0.7

FRESHNESS_STATES: tuple[str, ...] = ("fresh", "stale", "unknown")

Component = dict[str, str]


@dataclass
class Confidence:
    """A scored or explicitly unknown confidence with its full rationale."""

    score: float | None
    components: list[Component] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return self.score is not None

    @property
    def meets_floor(self) -> bool:
        """Unknown never meets the floor; it is not a disguised 0 or 1."""
        return self.score is not None and self.score >= RELIANCE_FLOOR

    def render(self) -> float | str:
        """The output form: a number, or the literal string ``unknown``."""
        return round(self.score, 4) if self.score is not None else "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "score": self.render(),
            "meets_floor": self.meets_floor,
            "components": self.components,
        }


def route_confidence(token_signals: list[float], token_count: int) -> float:
    """Blend the strongest per-token signal with token coverage.

    ``token_signals`` carries, per query token, the strongest signal class it
    matched (a value from ``SIGNAL_STRENGTH``, or 0.0 for no signal).
    """
    if token_count <= 0:
        return 0.0
    best = max(token_signals, default=0.0)
    coverage = sum(1 for signal in token_signals if signal > 0.0) / token_count
    return round(SIGNAL_WEIGHT * best + COVERAGE_WEIGHT * coverage, 4)


def decide(confidences: list[float]) -> tuple[str, int]:
    """Apply the route thresholds to a set of candidate confidences.

    Returns the decision (``load``, ``offer``, or ``no-match``) and how many
    of the strongest candidates an ``offer`` should present (those inside the
    ambiguity band). Confidence order is established here rather than trusted:
    callers rank candidates by lexical score, and confidence is not monotone in
    score, so a genuine near-tie can sit anywhere in the caller's list. The
    lexical baseline alone feeds this function; semantic reranking may reorder
    candidates but never recomputes these confidences.
    """
    if not confidences:
        return "no-match", 0
    ranked = sorted(confidences, reverse=True)
    top = ranked[0]
    if top < OFFER_FLOOR:
        return "no-match", 0
    band = 1
    while band < len(ranked) and ranked[band] >= top - AMBIGUITY_BAND:
        band += 1
    if top >= RELIANCE_FLOOR and band == 1:
        return "load", 1
    return "offer", band


def authorize(confidences: list[float]) -> tuple[str, list[int]]:
    """Decide, and name exactly which candidates the decision authorizes.

    This is the single reliance-floor gate: no caller may hand out load
    authorization to a candidate the floor did not clear. A ``load`` authorizes
    only candidates that individually reach ``RELIANCE_FLOOR``, so a weak
    candidate riding along behind a strong one is never loadable; an ``offer``
    authorizes the ambiguity band around the strongest candidate; a
    ``no-match`` authorizes nothing. Returned indices point into
    ``confidences`` in the caller's own order, so output ordering stays the
    caller's business.
    """
    decision, count = decide(confidences)
    if decision == "no-match":
        return decision, []
    if decision == "load":
        return decision, [
            index for index, score in enumerate(confidences) if score >= RELIANCE_FLOOR
        ]
    band = sorted(range(len(confidences)), key=lambda index: (-confidences[index], index))[:count]
    return decision, sorted(band)


@dataclass
class Source:
    """One piece of evidence behind a claim.

    ``quality`` is a key of ``QUALITY_BASE``; ``origin`` is display-only;
    ``origin_id`` is the independently derived identity used for
    corroboration.  A missing ``origin_id`` means independence is unknown and
    all such sources share one restrictive bucket. ``eligible`` is False when
    the source is not eligible for the consuming model context, in which case
    it contributes nothing at all.
    """

    quality: str
    origin: str
    eligible: bool = True
    origin_id: str = ""
    correction_status: str = "clean"


def claim_confidence(
    sources: list[Source],
    lifecycle: str = "",
    freshness: str = "unknown",
    contradicted: bool = False,
) -> Confidence:
    """Score one claim from its evidence, lifecycle, freshness, and conflicts."""
    components: list[Component] = []
    eligible = [
        source for source in sources if source.eligible and source.correction_status == "clean"
    ]
    for source in sources:
        if not source.eligible or source.correction_status != "clean":
            detail = (
                f"{source.quality} from {source.origin}: {source.correction_status}, removed"
                if source.correction_status != "clean"
                else f"{source.quality} from {source.origin}: ineligible, ignored"
            )
            components.append(
                {
                    "factor": "source",
                    "effect": "ignored",
                    "detail": detail,
                }
            )
    if not eligible:
        components.append(
            {
                "factor": "evidence",
                "effect": "unknown",
                "detail": "no eligible sources: confidence stays unknown, never fabricated",
            }
        )
        return Confidence(score=None, components=components)

    base_quality = max(eligible, key=lambda source: QUALITY_BASE[source.quality]).quality
    score = QUALITY_BASE[base_quality]
    components.append(
        {
            "factor": "source-quality",
            "effect": f"base {score:.2f}",
            "detail": f"strongest eligible source is {base_quality}",
        }
    )

    # Display URLs are not evidence of independence.  The host bridge must
    # provide a derived origin_id; unknown independence deliberately
    # collapses into one bucket instead of buying corroboration with strings.
    origins = {
        source.origin_id or "__unknown_origin__"
        for source in eligible
        if source.quality in CORROBORATING_QUALITIES
    }
    bonus = min(CORROBORATION_STEP * (len(origins) - 1), CORROBORATION_CAP)
    if bonus > 0:
        score += bonus
        components.append(
            {
                "factor": "corroboration",
                "effect": f"+{bonus:.2f}",
                "detail": (
                    f"{len(origins)} independent origins; sources derived from one "
                    "origin count once"
                ),
            }
        )
    elif len(eligible) > 1:
        components.append(
            {
                "factor": "corroboration",
                "effect": "+0.00",
                "detail": "no independent corroborating origin beyond the first",
            }
        )

    caps: list[tuple[str, float, str]] = []
    if lifecycle in LIFECYCLE_CAP:
        caps.append(("lifecycle", LIFECYCLE_CAP[lifecycle], f"lifecycle is {lifecycle}"))
    else:
        caps.append(("lifecycle", CAP_LIFECYCLE_UNKNOWN, "lifecycle state is unknown"))
    if freshness == "stale":
        caps.append(("freshness", CAP_STALE, "evidence is stale"))
    elif freshness != "fresh":
        caps.append(("freshness", CAP_FRESHNESS_UNKNOWN, "freshness is unknown"))
    if contradicted:
        caps.append(
            ("contradiction", CAP_CONTRADICTION, "unresolved contradiction freezes the claim")
        )
    for factor, cap, detail in caps:
        if score > cap:
            score = cap
            components.append({"factor": factor, "effect": f"cap {cap:.2f}", "detail": detail})
        else:
            components.append(
                {"factor": factor, "effect": f"cap {cap:.2f} not reached", "detail": detail}
            )

    return Confidence(score=round(min(score, 1.0), 4), components=components)


def answer_confidence(claims: list[Confidence]) -> Confidence:
    """An answer never exceeds its weakest materially relied-upon claim.

    An unknown claim makes the whole answer unknown: the weakest link cannot
    be inspected, so no score is fabricated around it.
    """
    components: list[Component] = []
    if not claims:
        components.append(
            {
                "factor": "claims",
                "effect": "unknown",
                "detail": "no relied-upon claims supplied",
            }
        )
        return Confidence(score=None, components=components)
    unknown = [index for index, claim in enumerate(claims) if claim.score is None]
    if unknown:
        components.append(
            {
                "factor": "claims",
                "effect": "unknown",
                "detail": (
                    f"claim(s) {', '.join(str(index + 1) for index in unknown)} are unknown; "
                    "the answer inherits unknown"
                ),
            }
        )
        return Confidence(score=None, components=components)
    scores = [claim.score for claim in claims if claim.score is not None]
    weakest = min(scores)
    components.append(
        {
            "factor": "claims",
            "effect": f"min {weakest:.2f}",
            "detail": (
                f"weakest of {len(scores)} materially relied-upon claim(s): "
                + ", ".join(f"{score:.2f}" for score in scores)
            ),
        }
    )
    return Confidence(score=round(weakest, 4), components=components)
