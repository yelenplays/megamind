from pathlib import Path

import pytest

from megamind.evidence import accept_evidence
from megamind.research import (
    ReplayConflict,
    ResearchDrift,
    ResearchStore,
    make_packet,
    make_plan,
)


def _plan() -> object:
    return make_plan(
        {
            "gap_id": "gap-1",
            "question": "Synthetic question",
            "policy_digest": "policy",
            "card_digest": "card",
            "access_digest": "access",
            "ceilings": {"queries": 2, "retrievals": 2},
        },
        today="2026-01-01",
    )


def test_research_transition_replay_is_noop_and_divergence_refused(tmp_path: Path) -> None:
    plan = _plan()
    store = ResearchStore(tmp_path)
    first = store.start(plan, today="2026-01-01")
    moved = store.transition(
        first.job_id,
        "permission-check",
        plan_id=plan.plan_id,
        gap_id=plan.gap_id,
        attempt_id=first.attempt_id,
        today="2026-01-01",
    )
    assert (
        store.transition(
            first.job_id,
            "permission-check",
            plan_id=plan.plan_id,
            gap_id=plan.gap_id,
            attempt_id=first.attempt_id,
            today="2026-01-01",
        )
        == moved
    )
    with pytest.raises(ReplayConflict):
        store.transition(
            first.job_id,
            "permission-check",
            plan_id=plan.plan_id,
            gap_id=plan.gap_id,
            attempt_id=first.attempt_id,
            reason="different",
        )


def test_research_drift_requires_replan(tmp_path: Path) -> None:
    plan = _plan()
    store = ResearchStore(tmp_path)
    job = store.start(plan)
    with pytest.raises(ResearchDrift):
        store.assert_no_drift(
            job, policy_digest="changed", card_digest="card", access_digest="access"
        )


def test_research_packet_id_and_opaque_resolver() -> None:
    packet = make_packet(
        {
            "job_id": "job",
            "attempt_id": "attempt",
            "claims": ["opaque-claim"],
            "contradictions": [],
            "interpretation": "Bounded interpretation",
            "answerability": {"general_method": "supported"},
        }
    )
    assert packet["schema"] == "megamind/research-packet/v1"
    assert isinstance(packet["packet_id"], str)


def test_retracted_evidence_is_rejected() -> None:
    record = accept_evidence(
        {
            "origin": "synthetic-source",
            "origin_id": "source-a",
            "quality": "primary",
            "correction_status": "retracted",
            "snapshot_sha256": "a" * 64,
            "normalized_sha256": "b" * 64,
            "facts": {
                "retrievable": True,
                "identity_resolved": True,
                "dated": True,
                "attributed": True,
                "publisher_resolved": True,
                "rights_determined": True,
            },
        }
    )
    assert record.decision == "rejected"
