"""Confidence rubrics: thresholds, calibration fixtures, unknown-first-class."""

from __future__ import annotations

import json
from pathlib import Path

from megamind.confidence import (
    AMBIGUITY_BAND,
    OFFER_FLOOR,
    RELIANCE_FLOOR,
    SOLO_RELIANCE_FLOOR,
    Confidence,
    Source,
    answer_confidence,
    authorize,
    claim_confidence,
    decide,
    route_confidence,
)

FIXTURE = Path(__file__).parent / "fixtures" / "confidence-calibration.json"


def _calibration_cases() -> list[dict[str, object]]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]  # type: ignore[no-any-return]


def test_calibration_fixture_pins_the_claim_rubric() -> None:
    cases = _calibration_cases()
    assert len(cases) >= 20  # the rubric's load-bearing cases are all pinned
    for case in cases:
        sources = [
            Source(
                quality=str(source["quality"]),
                origin=str(source["origin"]),
                eligible=bool(source.get("eligible", True)),
                origin_id=str(source.get("origin_id", "")),
            )
            for source in case["sources"]  # type: ignore[index]
        ]
        result = claim_confidence(
            sources,
            lifecycle=str(case["lifecycle"]),
            freshness=str(case["freshness"]),
            contradicted=bool(case["contradicted"]),
        )
        expected = case["expected_score"]
        assert result.score == expected, f"{case['name']}: expected {expected}, got {result.score}"
        if expected is None:
            assert not result.known
            assert result.render() == "unknown"
        else:
            assert result.meets_floor == (float(str(expected)) >= RELIANCE_FLOOR)  # type: ignore[arg-type]


def test_calibration_components_explain_every_case() -> None:
    for case in _calibration_cases():
        sources = [
            Source(
                str(s["quality"]),
                str(s["origin"]),
                bool(s.get("eligible", True)),
                str(s.get("origin_id", "")),
            )  # type: ignore[index]
            for s in case["sources"]  # type: ignore[index]
        ]
        result = claim_confidence(sources)
        assert result.components, case["name"]
        for component in result.components:
            assert set(component) == {"factor", "effect", "detail"}


# --- unknown is first-class ---------------------------------------------------


def test_unknown_is_never_fabricated_into_a_score() -> None:
    unknown = claim_confidence([])
    assert unknown.score is None
    assert unknown.render() == "unknown"
    assert unknown.meets_floor is False
    assert unknown.to_dict()["score"] == "unknown"
    # unknown is not 0.0 in disguise: it fails the floor for a different reason
    zero = Confidence(score=0.0)
    assert zero.render() == 0.0
    assert zero.meets_floor is False


def test_answer_inherits_unknown_from_any_relied_claim() -> None:
    answer = answer_confidence([Confidence(score=0.95), Confidence(score=None)])
    assert answer.score is None
    assert answer.render() == "unknown"
    assert answer_confidence([]).score is None


# --- claim rubric -------------------------------------------------------------


def test_claim_sources_from_one_origin_count_once() -> None:
    one_origin = claim_confidence(
        [Source("primary", "notes"), Source("primary", "notes")],
        freshness="fresh",
        lifecycle="active",
    )
    two_origins = claim_confidence(
        [
            Source("primary", "notes", origin_id="notes-origin"),
            Source("primary", "changelog", origin_id="changelog-origin"),
        ],
        freshness="fresh",
        lifecycle="active",
    )
    assert one_origin.score == 0.9
    assert two_origins.score == 0.95


def test_unresolved_contradiction_stays_below_the_floor() -> None:
    contradicted = claim_confidence(
        [Source("primary", "a"), Source("primary", "b")],
        lifecycle="active",
        freshness="fresh",
        contradicted=True,
    )
    assert contradicted.score is not None and contradicted.score < RELIANCE_FLOOR
    assert not contradicted.meets_floor


def test_stale_and_undated_evidence_never_reaches_the_floor() -> None:
    stale = claim_confidence([Source("primary", "a")], lifecycle="active", freshness="stale")
    undated = claim_confidence([Source("primary", "a")], lifecycle="active", freshness="unknown")
    assert stale.score is not None and stale.score < RELIANCE_FLOOR
    assert undated.score is not None and undated.score < RELIANCE_FLOOR


def test_unknown_origin_independence_never_awards_corroboration() -> None:
    result = claim_confidence(
        [
            Source("primary", "https://source.example/A"),
            Source("primary", "https://source.example/B"),
            Source("primary", "https://blog.example/repost-of-A"),
        ],
        lifecycle="active",
        freshness="fresh",
    )
    assert result.score == 0.9
    assert result.meets_floor is True
    assert not any(
        component["factor"] == "corroboration" and component["effect"] != "+0.00"
        for component in result.components
    )


def test_retracted_support_is_removed_before_confidence_scoring() -> None:
    result = claim_confidence(
        [
            Source(
                "primary",
                "doi:10.1056/NEJMoa2007621",
                origin_id="doi",
                correction_status="retracted",
            )
        ],
        lifecycle="active",
        freshness="fresh",
    )
    assert result.score is None
    assert not result.meets_floor


# --- answer rubric ------------------------------------------------------------


def test_answer_cannot_exceed_the_weakest_relied_claim() -> None:
    answer = answer_confidence(
        [Confidence(score=0.95), Confidence(score=0.6), Confidence(score=0.8)]
    )
    assert answer.score == 0.6
    assert answer.meets_floor is False


def test_answer_meets_floor_only_when_every_claim_does() -> None:
    answer = answer_confidence([Confidence(score=0.9), Confidence(score=0.75)])
    assert answer.score == 0.75
    assert answer.meets_floor is True


# --- route confidence and thresholds ------------------------------------------


def test_route_confidence_blends_signal_and_coverage() -> None:
    assert route_confidence([1.0], 1) == 1.0
    assert route_confidence([1.0, 0.0], 2) == 0.8
    assert route_confidence([0.3], 1) == 0.58
    assert route_confidence([0.0], 1) == 0.0
    assert route_confidence([], 0) == 0.0


def test_decide_applies_the_reliance_floor() -> None:
    assert decide([RELIANCE_FLOOR]) == ("load", 1)
    assert decide([0.9, 0.4]) == ("load", 1)
    assert decide([RELIANCE_FLOOR - 0.01]) == ("offer", 1)
    assert decide([OFFER_FLOOR]) == ("offer", 1)
    assert decide([OFFER_FLOOR - 0.01]) == ("no-match", 0)
    assert decide([]) == ("no-match", 0)


def test_decide_ambiguity_band_offers_instead_of_loading() -> None:
    decision, count = decide([0.9, 0.9 - AMBIGUITY_BAND + 0.001, 0.4])
    assert decision == "offer"
    assert count == 2
    assert decide([0.9, 0.9 - AMBIGUITY_BAND - 0.001]) == ("load", 1)


def test_decide_ranks_confidences_instead_of_trusting_caller_order() -> None:
    """Callers rank by lexical score, and confidence is not monotone in score."""
    # a genuine tie sits behind a weaker row: it must still block the auto-load
    assert decide([1.0, 0.82, 1.0]) == ("offer", 2)
    assert decide([0.4, 0.9]) == ("load", 1)
    assert decide([0.1, 0.2]) == ("no-match", 0)


def test_decide_solo_floor_loads_a_sole_candidate() -> None:
    """A sole candidate above the solo floor loads: there is no choice to offer."""
    assert decide([SOLO_RELIANCE_FLOOR], SOLO_RELIANCE_FLOOR) == ("load", 1)
    assert decide([0.67], SOLO_RELIANCE_FLOOR) == ("load", 1)
    # below the solo floor a sole candidate stays an offer
    assert decide([SOLO_RELIANCE_FLOOR - 0.01], SOLO_RELIANCE_FLOOR) == ("offer", 1)
    # any rival above the offer floor keeps the choice with the user
    assert decide([0.67, OFFER_FLOOR], SOLO_RELIANCE_FLOOR) == ("offer", 1)
    # without the opt-in the reliance floor alone decides, as before
    assert decide([0.67]) == ("offer", 1)


def test_authorize_solo_load_covers_exactly_the_sole_candidate() -> None:
    decision, covered = authorize([0.67], SOLO_RELIANCE_FLOOR)
    assert decision == "load"
    assert covered == [0]
    # the reliance floor still owns multi-candidate authorization
    decision, covered = authorize([0.95, 0.3], SOLO_RELIANCE_FLOOR)
    assert decision == "load"
    assert covered == [0]


# --- reliance-floor authorization ---------------------------------------------


def test_authorize_load_covers_only_candidates_that_reach_the_floor() -> None:
    decision, covered = authorize([0.95, 0.3, 0.8])
    assert decision == "load"
    assert covered == [0, 2]  # input order, and the 0.3 row is never loadable


def test_authorize_offer_covers_the_ambiguity_band_in_input_order() -> None:
    decision, covered = authorize([0.9, 0.4, 0.88])
    assert decision == "offer"
    assert covered == [0, 2]


def test_authorize_no_match_covers_nothing() -> None:
    assert authorize([OFFER_FLOOR - 0.01, 0.1]) == ("no-match", [])
    assert authorize([]) == ("no-match", [])


def test_authorize_never_widens_beyond_the_floor() -> None:
    for confidences in ([0.99, 0.74999], [0.8, 0.0], [1.0, 0.25, 0.5]):
        decision, covered = authorize(confidences)
        if decision == "load":
            assert all(confidences[index] >= RELIANCE_FLOOR for index in covered)
