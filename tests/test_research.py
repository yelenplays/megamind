from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import build_vault
from megamind.cli import main
from megamind.evidence import (
    EvidenceAcceptanceError,
    accept_evidence,
    claim_confidence_from_records,
    make_resolved_claim,
)
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


class _StubFacts:
    def __init__(self, claims: dict[str, float | str], unresolved: tuple[str, ...] = ()) -> None:
        self.claims = claims
        self.unresolved = unresolved

    def resolve(self, kind: str, identifier: str) -> bool:
        if kind == "claim":
            return identifier in self.claims
        return identifier in self.unresolved

    def claim_confidence(self, identifier: str) -> float | str:
        return self.claims[identifier]

    def contradiction_is_unresolved(self, identifier: str) -> bool:
        return identifier in self.unresolved


def test_research_packet_id_and_opaque_resolver() -> None:
    packet = make_packet(
        {
            "job_id": "job",
            "attempt_id": "attempt",
            "claims": ["opaque-claim"],
            "contradictions": [],
            "interpretation": "Bounded interpretation",
        },
        _StubFacts({"opaque-claim": 0.8}),
    )
    assert packet["schema"] == "megamind/research-packet/v1"
    assert isinstance(packet["packet_id"], str)
    assert packet["confidence"] == 0.8
    assert packet["answerability"]["verdict"] == "supported"


def test_packet_confidence_never_exceeds_its_weakest_claim() -> None:
    packet = make_packet(
        {
            "job_id": "job",
            "attempt_id": "attempt",
            "claims": ["strong", "weak"],
            "interpretation": "Bounded interpretation",
        },
        _StubFacts({"strong": 0.9, "weak": 0.5}),
    )
    assert packet["confidence"] == 0.5
    assert packet["answerability"]["verdict"] == "insufficient"
    assert packet["answerability"]["below_floor_claims"] == 1


def test_packet_confidence_is_unknown_when_any_claim_is_unknown() -> None:
    packet = make_packet(
        {
            "job_id": "job",
            "attempt_id": "attempt",
            "claims": ["strong", "unsupported"],
            "interpretation": "Bounded interpretation",
        },
        _StubFacts({"strong": 0.9, "unsupported": "unknown"}),
    )
    assert packet["confidence"] == "unknown"
    assert packet["answerability"] == {
        "verdict": "insufficient",
        "claims": 2,
        "unknown_claims": 1,
        "below_floor_claims": 0,
        "unresolved_contradictions": 0,
        "reliance_floor": 0.75,
    }


def test_packet_answerability_is_contradicted_by_an_unresolved_contradiction() -> None:
    packet = make_packet(
        {
            "job_id": "job",
            "attempt_id": "attempt",
            "claims": ["strong"],
            "contradictions": ["open"],
            "interpretation": "Bounded interpretation",
        },
        _StubFacts({"strong": 0.9}, unresolved=("open",)),
    )
    assert packet["answerability"]["verdict"] == "contradicted"


def test_packet_references_without_a_resolver_are_refused() -> None:
    with pytest.raises(Exception, match="require a resolver"):
        make_packet(
            {
                "job_id": "job",
                "attempt_id": "attempt",
                "claims": ["opaque-claim"],
                "interpretation": "Bounded interpretation",
            }
        )


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
    # The packet reports the frozen claim's confidence, not a receipt value.
    assert doc["packet"]["confidence"] == _frozen_claim(root, claim_id)["confidence"]
    assert doc["packet"]["answerability"]["claims"] == 1

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


def _frozen_claim(root: Path, claim_id: str) -> dict[str, Any]:
    path = root / ".megamind" / "research" / "claims" / f"{claim_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_forged_lifecycle_cannot_lift_the_emitted_packet_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    _job, honest_claim_id = _lane_to_packet_ready(capsys, root)
    honest = _frozen_claim(root, honest_claim_id)

    forged_root = _research_vault(tmp_path / "forged")
    code, doc = _run(
        capsys,
        forged_root,
        "research",
        "permission-check",
        "--input",
        _write(forged_root, "plan.json", {**PLAN_INPUT, "policy_authorized": True}),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    forged_job = doc["job"]
    _run(
        capsys,
        forged_root,
        "research",
        "record-discovery",
        "--input",
        _write(
            forged_root,
            "discovery.json",
            {
                "job_id": forged_job["job_id"],
                "attempt_id": forged_job["attempt_id"],
                "candidates": ["candidate-a"],
                "usage": {"queries": 1},
            },
        ),
        "--today",
        "2026-03-01",
    )
    code, doc = _run(
        capsys,
        forged_root,
        "research",
        "record-artifact",
        "--input",
        _write(forged_root, "artifact.json", EVIDENCE_INPUT),
    )
    assert code == 0, doc
    evidence_id = doc["evidence"]["evidence_id"]
    code, doc = _run(
        capsys,
        forged_root,
        "research",
        "record-claims",
        "--input",
        _write(
            forged_root,
            "claims.json",
            {
                "job_id": forged_job["job_id"],
                "attempt_id": forged_job["attempt_id"],
                "claims": [
                    {
                        "claim_key": "release-cadence",
                        "statement": "The synthetic product ships weekly.",
                        "supported_by": [evidence_id],
                        "lifecycle": "active",
                    }
                ],
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    forged = doc["claims"][0]
    # The receipt asked for `active`; the frozen claim keeps the derived
    # lifecycle, identifier and confidence of the honest run.
    assert forged["lifecycle"] == honest["lifecycle"] == "proposed"
    assert forged["claim_id"] == honest_claim_id
    assert forged["confidence"] == honest["confidence"]

    _run(
        capsys,
        forged_root,
        "research",
        "reconcile",
        "--job-id",
        forged_job["job_id"],
        "--today",
        "2026-03-01",
    )
    code, doc = _run(
        capsys,
        forged_root,
        "research",
        "packet",
        "--input",
        _write(
            forged_root,
            "packet.json",
            {
                "job_id": forged_job["job_id"],
                "attempt_id": forged_job["attempt_id"],
                "claims": [forged["claim_id"]],
                "interpretation": "The synthetic product ships on a weekly train.",
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    assert doc["packet"]["answerability"]["verdict"] == "insufficient"
    assert doc["packet"]["confidence"] == honest["confidence"]


def test_terminal_outcome_freezes_and_packet_id_is_part_of_its_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    job, claim_id = _lane_to_packet_ready(capsys, root)
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
                "interpretation": "The synthetic product ships on a weekly train.",
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    packet_id = str(doc["packet"]["packet_id"])

    answered = {
        "job_id": job["job_id"],
        "attempt_id": job["attempt_id"],
        "state": "answered",
        "plan_id": job["plan_id"],
        "packet_id": packet_id,
        "reason": "synthesis accepted",
    }
    code, doc = _run(
        capsys, root, "research", "outcome", "--input", _write(root, "outcome.json", answered)
    )
    assert code == 0, doc
    answered_id = str(doc["outcome"]["outcome_id"])
    frozen = json.loads(
        (root / ".megamind" / "research" / "outcomes" / f"{answered_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert frozen["schema"] == "megamind/research-outcome/v1"
    assert frozen["packet_id"] == packet_id

    # packet_id is outcome content, not the outcome's identity field: the same
    # terminal state naming no packet freezes as a distinct artifact.
    code, doc = _run(
        capsys,
        root,
        "research",
        "outcome",
        "--input",
        _write(root, "outcome-detached.json", {**answered, "packet_id": ""}),
    )
    assert code == 0, doc
    detached_id = str(doc["outcome"]["outcome_id"])
    assert detached_id != answered_id

    # A packet-free terminal outcome freezes on its own.
    code, doc = _run(
        capsys,
        root,
        "research",
        "outcome",
        "--input",
        _write(
            root,
            "outcome-cancelled.json",
            {**answered, "state": "cancelled", "packet_id": "", "reason": "cancelled"},
        ),
    )
    assert code == 0, doc
    assert _artifacts(root, "outcomes") == {
        f"{answered_id}.json",
        f"{detached_id}.json",
        f"{doc['outcome']['outcome_id']}.json",
    }


def test_research_status_lists_durable_jobs_without_a_job_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    job, _claim_id = _lane_to_extracting(capsys, root)

    code, doc = _run(capsys, root, "research", "status")
    assert code == 0, doc
    assert [entry["job_id"] for entry in doc["jobs"]] == [job["job_id"]]
    assert doc["count"] == 1

    # The per-job form still filters, and the actions that act on one attempt
    # still refuse without it.
    code, doc = _run(capsys, root, "research", "status", "--job-id", "no-such-job")
    assert code == 0, doc
    assert doc["count"] == 0
    for action in ("cancel", "resume", "reconcile"):
        code, doc = _run(capsys, root, "research", action)
        assert code == 2, doc
        assert doc["code"] == "usage_error"


def test_packet_refuses_a_forged_host_confidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _research_vault(tmp_path)
    job, claim_id = _lane_to_packet_ready(capsys, root)
    assert _frozen_claim(root, claim_id)["confidence"] != 0.99
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
                "interpretation": "Synthesis with a minted score.",
                "confidence": 0.99,
                "answerability": {"general_method": "supported"},
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 1
    assert doc["code"] == "research_invalid"
    assert _artifacts(root, "packets") == set()
    assert list((root / ".megamind" / "proposals").glob("*.md")) == []


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


def test_packet_over_an_unsupported_claim_stays_unknown(
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
    assert code == 0, doc
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
                        "claim_key": "unsupported",
                        "statement": "A claim with no admitted support.",
                        "supported_by": [],
                    }
                ],
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    claim_id = doc["claims"][0]["claim_id"]
    assert doc["claims"][0]["confidence"] == "unknown"
    _run(capsys, root, "research", "reconcile", "--job-id", job["job_id"], "--today", "2026-03-01")
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
                "interpretation": "Synthesis over an unsupported claim.",
            },
        ),
        "--today",
        "2026-03-01",
    )
    assert code == 0, doc
    assert doc["packet"]["confidence"] == "unknown"
    assert doc["packet"]["answerability"]["verdict"] == "insufficient"
    assert doc["packet"]["answerability"]["unknown_claims"] == 1


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


def _accepted_record(origin_id: str = "source-a") -> Any:
    return accept_evidence({**EVIDENCE_INPUT, "origin_id": origin_id})


def test_claim_lifecycle_is_derived_and_a_forged_one_changes_nothing() -> None:
    record = _accepted_record()
    records = {record.evidence_id: record}
    body = {
        "claim_key": "release-cadence",
        "statement": "The synthetic product ships weekly.",
        "supported_by": [record.evidence_id],
    }
    honest = make_resolved_claim(body, records)
    forged = make_resolved_claim({**body, "lifecycle": "active"}, records)
    assert honest.lifecycle == "proposed"
    assert forged.lifecycle == "proposed"
    assert forged.claim_id == honest.claim_id
    assert forged.confidence == honest.confidence


def test_claim_lifecycle_observation_may_only_narrow() -> None:
    record = _accepted_record()
    records = {record.evidence_id: record}
    body = {
        "claim_key": "release-cadence",
        "statement": "The synthetic product ships weekly.",
        "supported_by": [record.evidence_id],
    }
    narrowed = make_resolved_claim({**body, "lifecycle": "rejected"}, records)
    assert narrowed.lifecycle == "rejected"
    assert narrowed.confidence < make_resolved_claim(body, records).confidence


def test_unknown_claim_lifecycle_is_refused_rather_than_scored() -> None:
    record = _accepted_record()
    with pytest.raises(EvidenceAcceptanceError, match="lifecycle must be one of"):
        make_resolved_claim(
            {
                "claim_key": "release-cadence",
                "statement": "The synthetic product ships weekly.",
                "supported_by": [record.evidence_id],
                "lifecycle": "blessed",
            },
            {record.evidence_id: record},
        )


def test_a_weaker_lifecycle_observation_cannot_widen_an_extracted_claim() -> None:
    record = _accepted_record()
    records = {record.evidence_id: record}
    body = {
        "claim_key": "release-cadence",
        "statement": "The synthetic product ships weekly.",
        "supported_by": [record.evidence_id],
    }
    # `shaky` caps higher than the derived `proposed`, so it is a widening.
    assert make_resolved_claim({**body, "lifecycle": "shaky"}, records).lifecycle == "proposed"


def test_an_unresolved_contradiction_lowers_the_claim_score() -> None:
    records = [_accepted_record()]
    assert claim_confidence_from_records(
        records, lifecycle="active", contradicted=True
    ) < claim_confidence_from_records(records, lifecycle="active")


def test_claim_confidence_never_assumes_freshness() -> None:
    records = [_accepted_record()]
    unobserved = claim_confidence_from_records(records, lifecycle="active")
    assert unobserved == claim_confidence_from_records(
        records, lifecycle="active", freshness="unknown"
    )
    assert unobserved < claim_confidence_from_records(
        records, lifecycle="active", freshness="fresh"
    )
    assert (
        claim_confidence_from_records(records, lifecycle="active", freshness="stale") < unobserved
    )
    with pytest.raises(EvidenceAcceptanceError, match="freshness must be one of"):
        claim_confidence_from_records(records, freshness="recent")


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
    with pytest.raises(EvidenceAcceptanceError, match="unresolved evidence reference"):
        make_resolved_claim(
            {
                "claim_key": "release-cadence",
                "statement": "A claim resting on a retracted source.",
                "supported_by": [record.evidence_id],
            },
            {record.evidence_id: record},
        )
