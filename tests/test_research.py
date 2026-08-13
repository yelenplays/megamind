from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import build_vault
from megamind.cli import main
from megamind.evidence import accept_evidence
from megamind.registry import ResearchPolicy, load_registry, save_registry
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
            "wiki": "SynthWiki",
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


def test_research_plan_requires_a_wiki_binding() -> None:
    with pytest.raises(Exception, match="wiki must be a non-empty string"):
        make_plan(
            {
                "gap_id": "gap-1",
                "question": "Synthetic question",
                "policy_digest": "policy",
                "card_digest": "card",
                "access_digest": "access",
                "ceilings": {"queries": 1},
            }
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


def _research_vault(tmp_path: Path, *, enabled: bool = True) -> Path:
    root = build_vault(tmp_path)
    registry = load_registry(root)
    entry = registry.wiki_by_name("ProductWiki")
    assert entry is not None
    entry.research_policy = ResearchPolicy(
        enabled=enabled,
        claim_types=["general"],
        research_mode="approval" if enabled else "off",
        max_sources_per_cycle=2,
        digest="synthetic-policy-digest",
    )
    save_registry(root, registry)
    return root


def _run(capsys: pytest.CaptureFixture[str], root: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    code = main(["--format", "json", "--root", str(root), *argv])
    return code, json.loads(capsys.readouterr().out)


def _write(root: Path, name: str, payload: dict[str, Any]) -> str:
    path = root / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


PLAN_INPUT: dict[str, Any] = {
    "wiki": "ProductWiki",
    "gap_id": "gap-release-cadence",
    "question": "How often does the synthetic product ship?",
    "required_claims": ["release cadence"],
    "ceilings": {"queries": 2, "retrievals": 2},
    "privacy_class": "public-reference",
}

EVIDENCE_INPUT: dict[str, Any] = {
    "origin": "synthetic-source",
    "origin_id": "source-a",
    "quality": "primary",
    "correction_status": "clean",
    "snapshot_sha256": "c" * 64,
    "normalized_sha256": "d" * 64,
    "facts": {
        "retrievable": True,
        "identity_resolved": True,
        "dated": True,
        "attributed": True,
        "publisher_resolved": True,
        "rights_determined": True,
    },
}


def _lane_to_extracting(
    capsys: pytest.CaptureFixture[str], root: Path
) -> tuple[dict[str, Any], str]:
    code, doc = _run(
        capsys,
        root,
        "research",
        "permission-check",
        "--input",
        _write(root, "plan.json", {**PLAN_INPUT, "policy_authorized": True}),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    assert doc["status"] == "planned"
    job = doc["job"]

    code, doc = _run(
        capsys,
        root,
        "research",
        "record-discovery",
        "--input",
        _write(
            root,
            "discovery.json",
            {
                "job_id": job["job_id"],
                "attempt_id": job["attempt_id"],
                "candidates": ["candidate-a"],
                "usage": {"queries": 1},
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    assert doc["status"] == "retrieving"

    code, doc = _run(
        capsys,
        root,
        "research",
        "record-artifact",
        "--input",
        _write(root, "artifact.json", EVIDENCE_INPUT),
    )
    assert code == 0, doc
    assert doc["status"] == "accepted"
    evidence_id = doc["evidence"]["evidence_id"]

    code, doc = _run(
        capsys,
        root,
        "research",
        "record-claims",
        "--input",
        _write(
            root,
            "claims.json",
            {
                "job_id": job["job_id"],
                "attempt_id": job["attempt_id"],
                "claims": [
                    {
                        "claim_key": "release-cadence",
                        "statement": "The synthetic product ships weekly.",
                        "supported_by": [evidence_id],
                    }
                ],
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    assert doc["status"] == "extracting"
    return job, str(doc["claims"][0]["claim_id"])


def _lane_to_packet_ready(
    capsys: pytest.CaptureFixture[str], root: Path
) -> tuple[dict[str, Any], str]:
    job, claim_id = _lane_to_extracting(capsys, root)
    code, doc = _run(
        capsys, root, "research", "reconcile", "--job-id", job["job_id"], "--today", "2026-03-01"
    )
    assert code == 0, doc
    assert doc["status"] == "packet-ready"
    return job, claim_id


def test_research_lane_reaches_a_proposal_through_the_transition_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    job, claim_id = _lane_to_packet_ready(capsys, root)

    packet_input = {
        "job_id": job["job_id"],
        "attempt_id": job["attempt_id"],
        "claims": [claim_id],
        "interpretation": "The synthetic product ships on a weekly train.",
        "answerability": {"general_method": "supported"},
    }
    code, doc = _run(
        capsys,
        root,
        "research",
        "packet",
        "--input",
        _write(root, "packet.json", packet_input),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    assert doc["job"]["state"] == "change-proposed"
    assert (root / doc["proposal"]).is_file()

    # Exact replay of the whole packet step is a no-op, not a second proposal.
    replayed_code, replayed = _run(
        capsys,
        root,
        "research",
        "packet",
        "--input",
        _write(root, "packet.json", packet_input),
        "--today",
        "2026-03-01",
    )
    assert replayed_code == 0
    assert replayed["job"] == doc["job"]
    assert replayed["proposal_id"] == doc["proposal_id"]


def _artifacts(root: Path, directory: str) -> set[str]:
    path = root / ".megamind" / "research" / directory
    return {item.name for item in path.glob("*.json")} if path.is_dir() else set()


def test_packet_refuses_before_reconcile_without_freezing_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    job, claim_id = _lane_to_extracting(capsys, root)
    code, doc = _run(
        capsys,
        root,
        "research",
        "packet",
        "--input",
        _write(
            root,
            "packet.json",
            {
                "job_id": job["job_id"],
                "attempt_id": job["attempt_id"],
                "claims": [claim_id],
                "interpretation": "Synthesis that skipped reconciliation.",
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 1
    assert doc["code"] == "research_transition_invalid"
    assert _artifacts(root, "packets") == set()
    assert list((root / ".megamind" / "proposals").glob("*.md")) == []


def test_record_claims_replay_is_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    job, _claim_id = _lane_to_extracting(capsys, root)
    journal = root / ".megamind" / "research" / "jobs.jsonl"
    before = journal.read_text(encoding="utf-8")
    code, doc = _run(
        capsys,
        root,
        "research",
        "record-claims",
        "--input",
        str(root / "claims.json"),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    assert doc["status"] == "extracting"
    assert journal.read_text(encoding="utf-8") == before
    assert job["job_id"] == doc["job"]["job_id"]


def test_record_claims_refuses_a_forbidden_state_without_freezing_claims(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    job, _claim_id = _lane_to_packet_ready(capsys, root)
    before = _artifacts(root, "claims")
    code, doc = _run(
        capsys,
        root,
        "research",
        "record-claims",
        "--input",
        _write(
            root,
            "late-claims.json",
            {
                "job_id": job["job_id"],
                "attempt_id": job["attempt_id"],
                "claims": [
                    {
                        "claim_key": "late-claim",
                        "statement": "A claim recorded after reconciliation.",
                        "supported_by": [],
                    }
                ],
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 1
    assert doc["code"] == "research_transition_invalid"
    assert _artifacts(root, "claims") == before


def test_packet_refuses_an_unfrozen_claim_reference(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    job, _claim_id = _lane_to_packet_ready(capsys, root)
    code, doc = _run(
        capsys,
        root,
        "research",
        "packet",
        "--input",
        _write(
            root,
            "packet.json",
            {
                "job_id": job["job_id"],
                "attempt_id": job["attempt_id"],
                "claims": ["fabricated-claim"],
                "interpretation": "Unsupported synthesis.",
            },
        ),
    )
    assert code == 1
    assert doc["code"] == "research_invalid"


def test_permission_check_denies_when_the_card_does_not_authorize_research(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path, enabled=False)
    code, doc = _run(
        capsys,
        root,
        "research",
        "permission-check",
        "--input",
        _write(root, "plan.json", {**PLAN_INPUT, "policy_authorized": True}),
        "--today",
        "2026-03-01",
    )
    assert code == 0
    assert doc["status"] == "policy-denied"
    assert doc["authority"]["authorized"] is False


def test_permission_check_replay_is_a_no_op(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    argv = (
        "research",
        "permission-check",
        "--input",
        _write(root, "plan.json", {**PLAN_INPUT, "policy_authorized": True}),
        "--today",
        "2026-03-01",
    )
    first_code, first = _run(capsys, root, *argv)
    second_code, second = _run(capsys, root, *argv)
    assert (first_code, second_code) == (0, 0)
    assert second["job"] == first["job"]


def test_card_digest_drift_refuses_a_submitted_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    code, doc = _run(
        capsys,
        root,
        "research",
        "plan",
        "--input",
        _write(root, "plan.json", {**PLAN_INPUT, "card_digest": "host-asserted-digest"}),
        "--today",
        "2026-03-01",
    )
    assert code == 1
    assert doc["code"] == "research_replan_required"


def test_host_receipt_can_withhold_but_never_widen_a_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    code, doc = _run(
        capsys,
        root,
        "research",
        "permission-check",
        "--input",
        _write(root, "plan.json", {**PLAN_INPUT, "policy_authorized": False}),
        "--today",
        "2026-03-01",
    )
    assert code == 0
    assert doc["status"] == "policy-denied"
    assert doc["authority"]["authorized"] is True


def test_discovery_over_the_plan_ceiling_is_a_terminal_budget_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    code, doc = _run(
        capsys,
        root,
        "research",
        "permission-check",
        "--input",
        _write(root, "plan.json", {**PLAN_INPUT, "policy_authorized": True}),
        "--today",
        "2026-03-01",
    )
    assert code == 0
    job = doc["job"]
    code, doc = _run(
        capsys,
        root,
        "research",
        "record-discovery",
        "--input",
        _write(
            root,
            "discovery.json",
            {
                "job_id": job["job_id"],
                "attempt_id": job["attempt_id"],
                "candidates": ["candidate-a"],
                "usage": {"queries": 99},
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    assert doc["status"] == "budget-exhausted"
    assert doc["budget"]["over"] == ["queries"]


def test_claims_refuse_evidence_this_lane_never_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    code, doc = _run(
        capsys,
        root,
        "research",
        "permission-check",
        "--input",
        _write(root, "plan.json", {**PLAN_INPUT, "policy_authorized": True}),
        "--today",
        "2026-03-01",
    )
    job = doc["job"]
    _run(
        capsys,
        root,
        "research",
        "record-discovery",
        "--input",
        _write(
            root,
            "discovery.json",
            {
                "job_id": job["job_id"],
                "attempt_id": job["attempt_id"],
                "candidates": ["candidate-a"],
                "usage": {"queries": 1},
            },
        ),
        "--today",
        "2026-03-01",
    )
    code, doc = _run(
        capsys,
        root,
        "research",
        "record-claims",
        "--input",
        _write(
            root,
            "claims.json",
            {
                "job_id": job["job_id"],
                "attempt_id": job["attempt_id"],
                "claims": [
                    {
                        "claim_key": "release-cadence",
                        "statement": "Unsupported statement.",
                        "supported_by": ["never-frozen"],
                    }
                ],
            },
        ),
    )
    assert code == 1
    assert doc["code"] == "evidence_acceptance_invalid"


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
