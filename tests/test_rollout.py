"""Phase 6 governed host rollout, exercised through the public AXI."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from conftest import build_vault
from megamind import rollout as rollout_module
from megamind.registry import load_registry, save_registry
from megamind.rollout import RolloutInputs, apply_plan, apply_rollback, build_plan, plan_rollback
from test_cli import run_json, run_toon

HOST_ID = "synthetic-host-a"
TODAY = "2026-10-01"


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path


def _evaluation(status: str = "promoted") -> dict[str, Any]:
    passed = status == "promoted"
    return {
        "schema_version": "megamind/evaluation-score/v1",
        "plan_id": "synthetic-evaluation-v1",
        "status": status,
        "blind_scores_sha256": "synthetic-blind-seal",
        "gates": {"promotion": {}, "passed": passed},
        "rollback_ref": "" if passed else "evaluation:synthetic-evaluation-v1",
        "summary": {
            "prompt_leak": False,
            "canary_leak": False,
            "privacy_violations": 0,
            "model_access_violations": 0,
        },
    }


def _host(model_class: str = "cloud") -> dict[str, Any]:
    return {
        "schema": "megamind/host-rollout-evidence/v1",
        "host_id": HOST_ID,
        "model_class": model_class,
        "checks": {
            "mandatory_preflight": True,
            "privacy_enforcement": True,
            "quiet_no_match": True,
            "task_logging": True,
            "failure_disclosure": True,
            "local_only_activation": True,
        },
    }


def _evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    vault: Path,
    *,
    query: str = "release process",
    evaluation_status: str = "promoted",
    model_class: str = "cloud",
) -> dict[str, Path]:
    code, matched, err = run_json(
        capsys,
        "--root",
        str(vault),
        "preflight",
        query,
        "--model-class",
        model_class,
        "--today",
        TODAY,
        "--full",
    )
    assert code == 0 and err == ""
    assert matched["status"] in {"matched", "ambiguous", "privacy-filtered"}
    code, no_match, err = run_json(
        capsys,
        "--root",
        str(vault),
        "preflight",
        "quantum llama farming",
        "--model-class",
        model_class,
        "--today",
        TODAY,
        "--full",
    )
    assert code == 0 and err == "" and no_match["status"] == "no-match"
    return {
        "host": _write_json(tmp_path / "evidence" / "host.json", _host(model_class)),
        "matched": _write_json(tmp_path / "evidence" / "matched.json", matched),
        "no_match": _write_json(tmp_path / "evidence" / "no-match.json", no_match),
        "evaluation": _write_json(
            tmp_path / "evidence" / "evaluation.json", _evaluation(evaluation_status)
        ),
    }


def _inputs(
    tmp_path: Path,
    vault: Path,
    evidence: dict[str, Path],
    *,
    wiki: str = "ProductWiki",
    sequence: int = 0,
    prior_proofs: tuple[Path, ...] = (),
) -> RolloutInputs:
    return RolloutInputs(
        state_root=tmp_path / "rollout-state",
        estate=vault,
        wiki_root=vault,
        wiki=wiki,
        host_id=HOST_ID,
        model_class="cloud",
        host_evidence=evidence["host"],
        preflight_evidence=evidence["matched"],
        no_match_evidence=evidence["no_match"],
        evaluation_evidence=evidence["evaluation"],
        governance_approval="synthetic-governance-approval",
        access_approval="synthetic-access-approval",
        sequence=sequence,
        prior_proofs=prior_proofs,
        today=date.fromisoformat(TODAY),
    )


def _promote_args(inputs: RolloutInputs) -> list[str]:
    args = [
        "rollout",
        "promote",
        "--state-root",
        str(inputs.state_root),
        "--estate",
        str(inputs.estate),
        "--wiki-root",
        str(inputs.wiki_root),
        "--wiki",
        inputs.wiki,
        "--host-id",
        inputs.host_id,
        "--model-class",
        inputs.model_class,
        "--host-evidence",
        str(inputs.host_evidence),
        "--preflight-evidence",
        str(inputs.preflight_evidence),
        "--no-match-evidence",
        str(inputs.no_match_evidence),
        "--evaluation-evidence",
        str(inputs.evaluation_evidence),
        "--governance-approval",
        inputs.governance_approval,
        "--access-approval",
        inputs.access_approval,
        "--sequence",
        str(inputs.sequence),
        "--today",
        TODAY,
    ]
    for path in inputs.prior_proofs:
        args.extend(("--prior-proof", str(path)))
    return args


def test_public_rollout_interface_plans_promotes_checks_health_and_rolls_back(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path / "estate")
    evidence = _evidence(tmp_path, capsys, vault)
    inputs = _inputs(tmp_path, vault, evidence)
    args = _promote_args(inputs)

    code, plan, err = run_json(capsys, *args)
    assert code == 0 and err == ""
    assert plan["schema_version"] == "megamind/rollout-plan/v1"
    assert plan["status"] == "ready"
    assert plan["privacy_rank"] == 0
    assert all(check["status"] == "passed" for check in plan["checks"])

    code, result, err = run_json(capsys, *args, "--apply", "--plan-id", str(plan["plan_id"]))
    assert code == 0 and err == ""
    assert result["schema_version"] == "megamind/rollout-result/v1"
    assert result["status"] == "promoted"
    assert result["loadable"] is True
    assert result["effective_access"] == "full"
    assert all(value is False for value in result["proof"]["boundaries"].values())
    serialized = json.dumps(result["proof"], sort_keys=True)
    assert str(tmp_path) not in serialized
    assert "release process" not in serialized
    assert "quantum llama farming" not in serialized

    code, replay, _ = run_json(capsys, *args, "--apply", "--plan-id", str(plan["plan_id"]))
    assert code == 0 and replay["status"] == "noop"
    promotion_id = str(result["promotion_id"])

    code, health, _ = run_json(
        capsys,
        "rollout",
        "health",
        "--state-root",
        str(inputs.state_root),
        "--wiki-root",
        str(vault),
        "--promotion-id",
        promotion_id,
    )
    assert code == 0
    assert health["schema_version"] == "megamind/rollout-health/v1"
    assert health["status"] == "healthy"

    rollback_args = [
        "rollout",
        "rollback",
        "--state-root",
        str(inputs.state_root),
        "--promotion-id",
        promotion_id,
        "--reason",
        "synthetic health exercise",
        "--today",
        TODAY,
    ]
    code, rollback_plan, _ = run_json(capsys, *rollback_args)
    assert code == 0
    assert rollback_plan["schema_version"] == "megamind/rollout-rollback-plan/v1"
    code, receipt, _ = run_json(
        capsys,
        *rollback_args,
        "--apply",
        "--plan-id",
        str(rollback_plan["plan_id"]),
    )
    assert code == 0
    assert receipt["schema_version"] == "megamind/rollout-rollback-receipt/v1"
    assert receipt["loadable"] is False
    assert receipt["proof_retained"] is True

    code, after, _ = run_json(
        capsys,
        "rollout",
        "health",
        "--state-root",
        str(inputs.state_root),
        "--wiki-root",
        str(vault),
        "--promotion-id",
        promotion_id,
    )
    assert code == 1 and after["status"] == "rollback-required"
    code, status, _ = run_json(capsys, "rollout", "status", "--state-root", str(inputs.state_root))
    assert code == 0
    assert status["counts"] == {"promoted": 0, "rolled_back": 1, "blocked": 0}


def test_failed_evaluation_is_a_durable_non_loadable_blocked_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path / "estate")
    evidence = _evidence(tmp_path, capsys, vault, evaluation_status="rollback-required")
    inputs = _inputs(tmp_path, vault, evidence)
    args = _promote_args(inputs)
    code, plan, _ = run_json(capsys, *args)
    assert code == 1
    assert plan["status"] == "blocked"
    failed = {check["check"] for check in plan["checks"] if check["status"] == "failed"}
    assert failed == {"evaluation-promotion"}

    code, result, _ = run_json(capsys, *args, "--apply", "--plan-id", str(plan["plan_id"]))
    assert code == 1
    assert result["status"] == "blocked"
    assert result["loadable"] is False
    assert not (inputs.state_root / "active").exists()
    _, status, _ = run_json(capsys, "rollout", "status", "--state-root", str(inputs.state_root))
    assert status["counts"]["blocked"] == 1


def test_provisional_unapproved_and_none_access_wikis_cannot_be_promoted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path / "estate")
    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    assert product is not None
    product.provisional = True
    save_registry(vault, registry)
    evidence = _evidence(tmp_path, capsys, vault, query="release process")
    inputs = _inputs(tmp_path, vault, evidence)
    plan = build_plan(inputs)
    failed = {check["check"] for check in plan["checks"] if check["status"] == "failed"}
    assert "trusted-governance" in failed
    assert "matched-preflight" in failed  # provisional is only offered, never matched

    product.provisional = False
    product.model_access.cloud = "none"
    save_registry(vault, registry)
    evidence = _evidence(tmp_path / "none", capsys, vault, query="pricing release")
    restricted_inputs = RolloutInputs(
        **{
            **_inputs(tmp_path / "none", vault, evidence).__dict__,
            "governance_approval": "",
            "access_approval": "",
        }
    )
    restricted_plan = build_plan(restricted_inputs)
    restricted_failed = {
        check["check"] for check in restricted_plan["checks"] if check["status"] == "failed"
    }
    assert {
        "matched-preflight",
        "loadable-access",
        "governance-approval",
        "access-approval",
    } <= restricted_failed


def test_privacy_order_requires_a_complete_nondecreasing_prior_proof_chain(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path / "estate")
    public_evidence = _evidence(tmp_path / "public", capsys, vault)
    public_inputs = _inputs(tmp_path / "public", vault, public_evidence)
    public_plan = build_plan(public_inputs)
    public_result = apply_plan(public_inputs, str(public_plan["plan_id"]))
    proof_path = public_inputs.state_root / "proofs" / f"{public_result['promotion_id']}.json"

    digest_evidence = _evidence(
        tmp_path / "digest", capsys, vault, query="research interview study"
    )
    digest_inputs = _inputs(
        tmp_path / "digest",
        vault,
        digest_evidence,
        wiki="ResearchDigest",
        sequence=1,
    )
    blocked = build_plan(digest_inputs)
    assert blocked["status"] == "blocked"
    assert any(
        check["check"] == "incremental-sequence" and check["status"] == "failed"
        for check in blocked["checks"]
    )

    ordered = RolloutInputs(**{**digest_inputs.__dict__, "prior_proofs": (proof_path,)})
    ready = build_plan(ordered)
    assert ready["status"] == "ready", [
        check for check in ready["checks"] if check["status"] == "failed"
    ]
    assert ready["effective_access"] == "digest-only"
    # Unknown sensitivity remains late in the privacy order even with bounded access.
    assert ready["privacy_rank"] == 3


def test_health_fails_closed_on_card_or_access_drift_and_state_cannot_enter_a_vault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path / "estate")
    evidence = _evidence(tmp_path, capsys, vault)
    inputs = _inputs(tmp_path, vault, evidence)
    plan = build_plan(inputs)
    result = apply_plan(inputs, str(plan["plan_id"]))

    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    assert product is not None
    product.model_access.cloud = "digest-only"
    save_registry(vault, registry)
    code, document, _ = run_json(
        capsys,
        "rollout",
        "health",
        "--state-root",
        str(inputs.state_root),
        "--wiki-root",
        str(vault),
        "--promotion-id",
        str(result["promotion_id"]),
    )
    assert code == 1
    assert document["status"] == "rollback-required"
    assert document["loadable"] is False
    failed = {check["check"] for check in document["checks"] if check["status"] == "failed"}
    assert {"wiki-card-current", "access-not-widened"} <= failed

    unsafe = RolloutInputs(**{**inputs.__dict__, "state_root": vault / "rollout-state"})
    with pytest.raises(rollout_module.RolloutError, match=r"isolated|vault"):
        build_plan(unsafe)


def test_apply_and_rollback_resume_after_interrupted_write_ahead_transactions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = build_vault(tmp_path / "estate")
    evidence = _evidence(tmp_path, capsys, vault)
    inputs = _inputs(tmp_path, vault, evidence)
    plan = build_plan(inputs)
    original = rollout_module._write_state
    interrupted = False

    def interrupt_promote(state: Path, relative: str, document: dict[str, Any]) -> Path:
        nonlocal interrupted
        if not interrupted and relative.startswith("proofs/"):
            interrupted = True
            raise OSError("synthetic interruption")
        return original(state, relative, document)

    monkeypatch.setattr(rollout_module, "_write_state", interrupt_promote)
    with pytest.raises(OSError, match="synthetic interruption"):
        apply_plan(inputs, str(plan["plan_id"]))
    monkeypatch.setattr(rollout_module, "_write_state", original)
    result = apply_plan(inputs, str(plan["plan_id"]))
    assert result["status"] == "promoted"

    rollback_plan = plan_rollback(
        inputs.state_root, str(result["promotion_id"]), "synthetic rollback", inputs.today
    )
    interrupted = False

    def interrupt_rollback(state: Path, relative: str, document: dict[str, Any]) -> Path:
        nonlocal interrupted
        if not interrupted and relative.startswith("receipts/"):
            interrupted = True
            raise OSError("synthetic interruption")
        return original(state, relative, document)

    monkeypatch.setattr(rollout_module, "_write_state", interrupt_rollback)
    with pytest.raises(OSError, match="synthetic interruption"):
        apply_rollback(
            inputs.state_root,
            str(result["promotion_id"]),
            "synthetic rollback",
            str(rollback_plan["plan_id"]),
            inputs.today,
        )
    monkeypatch.setattr(rollout_module, "_write_state", original)
    receipt = apply_rollback(
        inputs.state_root,
        str(result["promotion_id"]),
        "synthetic rollback",
        str(rollback_plan["plan_id"]),
        inputs.today,
    )
    assert receipt["status"] == "rolled-back"


def test_rollout_json_and_toon_are_the_same_typed_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path / "estate")
    evidence = _evidence(tmp_path, capsys, vault)
    inputs = _inputs(tmp_path, vault, evidence)
    args = _promote_args(inputs)
    code_json, document, err_json = run_json(capsys, *args)
    code_toon, rendered, err_toon = run_toon(capsys, *args)
    from megamind import toon

    assert code_json == code_toon == 0
    assert err_json == err_toon == ""
    assert toon.encode(document) == rendered
