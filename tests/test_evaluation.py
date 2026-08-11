"""Phase 4 evaluation contracts, exercised through the real public CLI.

Every test here runs the same interfaces a host runs. The benchmark tests drive
the frozen release fixture; the experiment tests drive the plan/validate/score/
record contract end to end, including the refusal paths.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from megamind import toon
from megamind.cli import main
from megamind.evaluation import (
    EvaluationError,
    _load_plan,
    _PublicRunner,
    check_benchmark,
    digest_file,
    digest_tree,
    plan_experiment,
    run_benchmark,
    score_experiment,
    validate_experiment,
)

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "evals/fixtures/release-mini"
QUERIES = ROOT / "evals/queries.jsonl"
THRESHOLDS = ROOT / "evals/thresholds.toml"
TASKS = ROOT / "evals/experiment-tasks-v1.json"
RUBRIC = ROOT / "evals/rubric-v1.json"

_CONDITION_ANSWERS = {
    "no-wiki": {
        "t01": "I do not know the plan names.",
        "t02": "Unclear rollout details.",
        "t03": "The coverage is unavailable.",
    },
    "current-wiki": {
        "t01": "There is a starter plan and a team plan.",
        "t02": "Unclear rollout details.",
        "t03": "The coverage is unavailable.",
    },
    "updated-wiki": {
        "t01": "There is a starter plan and a team plan.",
        "t02": "A staged rollout precedes each release note.",
        "t03": "The coverage is unavailable.",
    },
}
_PROVENANCE = {
    "t01": ["ProductWiki/topics/pricing.md"],
    "t02": ["ProductWiki/topics/release.md"],
    "t03": [],
}


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    code = main(["--format", "json", *argv])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


# --- frozen release benchmark ------------------------------------------------


def test_release_benchmark_is_canonical_and_repeatable() -> None:
    first = run_benchmark(FIXTURES, QUERIES, THRESHOLDS)
    second = run_benchmark(FIXTURES, QUERIES, THRESHOLDS)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert set(first["aggregates"]) == {
        "exact",
        "near",
        "paraphrase",
        "ambiguous",
        "no-match",
        "privacy",
    }
    assert first["benchmark_version"] == "release-mini-v1"
    assert first["safety"]["canary_leaks"] == 0
    assert first["safety"]["model_access_violations"] == 0
    assert first["safety"]["contract_violations"] == 0
    assert first["safety"]["public_invocations"] == 2 * len(first["per_query"])
    assert first["help"]


def test_benchmark_repeats_across_processes_temp_roots_and_hash_seeds(tmp_path: Path) -> None:
    """A frozen result must not depend on the process, the cwd, or PYTHONHASHSEED."""
    digests: set[str] = set()
    for index, seed in enumerate(("0", "1", "random")):
        env = dict(os.environ)
        env["PYTHONHASHSEED"] = seed
        env["PYTHONPATH"] = str(ROOT / "src")
        env["TMPDIR"] = str(tmp_path / f"tmp{index}")
        Path(env["TMPDIR"]).mkdir()
        workdir = tmp_path / f"work{index}"
        workdir.mkdir()
        out = tmp_path / f"result{index}.json"
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "megamind.cli",
                "--format",
                "json",
                "bench",
                "run",
                "--fixtures",
                str(FIXTURES),
                "--queries",
                str(QUERIES),
                "--thresholds",
                str(THRESHOLDS),
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
            cwd=workdir,
            env=env,
            check=False,
        )
        assert process.returncode == 0, process.stderr
        digests.add(digest_file(out))
    assert len(digests) == 1


def test_benchmark_gates_pass_on_the_frozen_release_inputs(tmp_path: Path) -> None:
    result = run_benchmark(FIXTURES, QUERIES, THRESHOLDS)
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(result), encoding="utf-8")
    check = check_benchmark(results_path, THRESHOLDS)
    assert check["status"] == "passed", check["failed_gates"]
    assert check["help"]


def test_benchmark_loads_pages_and_keeps_canaries_unreachable() -> None:
    result = run_benchmark(FIXTURES, QUERIES, THRESHOLDS)
    rows = {row["id"]: row for row in result["per_query"]}
    # Exact pages are actually loaded, so the canary and budget metrics are real.
    assert rows["q01"]["loaded"] == ["ProductWiki/topics/release.md"]
    assert rows["q01"]["context_chars"] > 0
    assert any(row["context_chars"] > 0 for row in result["per_query"])
    assert all(not row["canary_paths"] for row in result["per_query"])
    # The unindexed canary pages exist and a naive baseline does surface them.
    assert (FIXTURES / "OpsWiki/private/approval-notes.md").is_file()
    assert result["baselines"]["grep"]["canary_leaks"] > 0
    assert result["baselines"]["stuff"]["canary_leaks"] > 0
    assert result["baselines"]["stuff"]["context_chars"] > sum(
        row["context_chars"] for row in result["per_query"]
    )


def test_declared_access_is_enforced_for_every_model_class() -> None:
    result = run_benchmark(FIXTURES, QUERIES, THRESHOLDS)
    rows = {row["id"]: row for row in result["per_query"]}
    # pointer-only wiki, local model class: routed, never loaded.
    assert rows["q06"]["model_class"] == "local"
    assert rows["q06"]["candidates"] == ["ArchiveWiki"]
    assert rows["q06"]["loaded"] == []
    # pointer-only wiki, cloud model class: same rule.
    assert rows["q17"]["loaded"] == []
    # digest-only wiki authorizes exactly its digest.
    assert rows["q18"]["loaded"] == ["DigestWiki/DIGEST.md"]
    # company-private wiki is surfaced by route but never loaded for cloud.
    assert "OpsWiki" in rows["q19"]["candidates"]
    assert rows["q19"]["loaded"] == ["ProductWiki/topics/pricing.md"]
    # a provisional wiki may be offered, never authorized.
    assert rows["q07"]["candidates"] == ["ProvisionalWiki"]
    assert rows["q07"]["loaded"] == []


def test_benchmark_requires_a_registered_frozen_query_set_header(tmp_path: Path) -> None:
    unheaded = tmp_path / "queries.jsonl"
    unheaded.write_text(
        json.dumps({"id": "a", "tier": "exact", "query": "release", "expected_wikis": []}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(EvaluationError, match="registered"):
        run_benchmark(FIXTURES, unheaded, THRESHOLDS)


def test_benchmark_refuses_a_stale_threshold_binding(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "README.md").write_text("synthetic\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="binding"):
        run_benchmark(corpus, QUERIES, THRESHOLDS)


def test_benchmark_refuses_thresholds_without_a_binding(tmp_path: Path) -> None:
    unbound = tmp_path / "thresholds.toml"
    unbound.write_text("[release]\nexact_accuracy_min = 0.5\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="binding"):
        run_benchmark(FIXTURES, QUERIES, unbound)


def test_benchmark_version_is_derived_from_the_query_set(tmp_path: Path) -> None:
    """A query set that claims another version is refused, not relabelled."""
    lines = QUERIES.read_text(encoding="utf-8").splitlines()
    header = json.loads(lines[0])
    header["version"] = "not-the-release-set"
    forged = tmp_path / "queries.jsonl"
    forged.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="binding"):
        run_benchmark(FIXTURES, forged, THRESHOLDS)


def test_benchmark_refuses_a_failed_public_invocation(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    (broken / ".megamind").mkdir(parents=True)
    (broken / ".megamind" / "registry.json").write_text(
        json.dumps({"version": 2, "wikis": [{"name": "X"}]}), encoding="utf-8"
    )
    runner = _PublicRunner(broken)
    with pytest.raises(EvaluationError, match="exited"):
        runner.run(["route", "release"], "megamind/route-result/")
    assert runner.rejected == 1


def test_benchmark_refuses_an_unexpected_public_schema() -> None:
    runner = _PublicRunner(FIXTURES)
    with pytest.raises(EvaluationError, match="required"):
        runner.run(["route", "release"], "megamind/preflight-result/")


def test_check_scores_an_absent_tier_as_zero_instead_of_crashing(tmp_path: Path) -> None:
    queries = tmp_path / "queries.jsonl"
    queries.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema": "megamind/benchmark-query-set/v1",
                        "version": "single-tier-v1",
                        "frozen": True,
                    }
                ),
                json.dumps(
                    {
                        "id": "a1",
                        "tier": "privacy",
                        "query": "research",
                        "expected_wikis": ["DigestWiki"],
                        "model_class": "local",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    thresholds = tmp_path / "thresholds.toml"
    thresholds.write_text(
        "[binding]\n"
        'benchmark_version = "single-tier-v1"\n'
        f'corpus_sha256 = "{digest_tree(FIXTURES)}"\n'
        f'queries_sha256 = "{digest_file(queries)}"\n'
        "\n[release]\nexact_accuracy_min = 0.50\n",
        encoding="utf-8",
    )
    result = run_benchmark(FIXTURES, queries, thresholds)
    assert result["aggregates"]["exact"]["accuracy"] is None
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(result), encoding="utf-8")
    check = check_benchmark(results_path, thresholds)
    assert check["status"] == "failed"
    assert "exact_accuracy" in check["failed_gates"]


def test_bench_run_and_check_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "results.json"
    code, doc, err = run_json(
        capsys,
        "bench",
        "run",
        "--fixtures",
        str(FIXTURES),
        "--queries",
        str(QUERIES),
        "--thresholds",
        str(THRESHOLDS),
        "--out",
        str(out),
        "--repeat",
    )
    assert code == 0
    assert err == ""
    assert doc["schema_version"] == "megamind/benchmark-result/v1"
    assert doc["help"]
    code, doc, _ = run_json(
        capsys,
        "bench",
        "check",
        "--results",
        str(out),
        "--thresholds",
        str(THRESHOLDS),
    )
    assert code == 0
    assert doc["status"] == "passed"


def test_bench_refuses_an_output_destination_inside_the_fixture(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(
        capsys,
        "bench",
        "run",
        "--fixtures",
        str(FIXTURES),
        "--queries",
        str(QUERIES),
        "--thresholds",
        str(THRESHOLDS),
        "--out",
        str(FIXTURES / "leak.json"),
    )
    assert code == 1
    assert doc["code"] == "evaluation_invalid"
    assert not (FIXTURES / "leak.json").exists()


def test_bench_refuses_an_output_destination_inside_a_vault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = tmp_path / "vault"
    (vault / ".megamind").mkdir(parents=True)
    code, doc, _ = run_json(
        capsys,
        "bench",
        "run",
        "--fixtures",
        str(FIXTURES),
        "--queries",
        str(QUERIES),
        "--thresholds",
        str(THRESHOLDS),
        "--out",
        str(vault / "notes" / "results.json"),
    )
    assert code == 1
    assert doc["code"] == "evaluation_invalid"
    assert not (vault / "notes").exists()


# --- frozen fixture and its generator ---------------------------------------


def test_generator_reproduces_the_frozen_fixture_byte_for_byte(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "evals"))
    try:
        import gen_corpus
    finally:
        sys.path.pop(0)
    target = tmp_path / "release-mini"
    gen_corpus.generate(target)
    assert digest_tree(target) == digest_tree(FIXTURES)
    checked = {
        path.relative_to(FIXTURES).as_posix() for path in FIXTURES.rglob("*") if path.is_file()
    }
    assert checked == set(gen_corpus.FILES)


def test_thresholds_stay_bound_to_the_frozen_inputs() -> None:
    text = THRESHOLDS.read_text(encoding="utf-8")
    assert f'corpus_sha256 = "{digest_tree(FIXTURES)}"' in text
    assert f'queries_sha256 = "{digest_file(QUERIES)}"' in text
    assert f'task_set_sha256 = "{digest_file(TASKS)}"' in text


# --- three-arm experiment ----------------------------------------------------


def _workspace(tmp_path: Path) -> dict[str, Path]:
    names = ("none", "current", "updated", "outputs", "artifacts", "audit")
    paths = {name: tmp_path / name for name in names}
    for path in paths.values():
        path.mkdir(parents=True)
    (paths["current"] / "notes.md").write_text("starter plan and team plan\n", encoding="utf-8")
    (paths["updated"] / "notes.md").write_text(
        "starter plan and team plan, staged release\n", encoding="utf-8"
    )
    return paths


def _plan(
    tmp_path: Path, seed: int = 7, thresholds: Path = THRESHOLDS
) -> tuple[dict[str, Path], dict[str, Any]]:
    paths = _workspace(tmp_path)
    plan, grader, unblinding = plan_experiment(
        TASKS,
        paths["none"],
        paths["current"],
        paths["updated"],
        RUBRIC,
        thresholds,
        "synthetic-model-v1",
        "none",
        "fixed",
        seed,
        paths["outputs"],
    )
    (paths["artifacts"] / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (paths["artifacts"] / "grader.json").write_text(json.dumps(grader), encoding="utf-8")
    (paths["artifacts"] / "map.json").write_text(json.dumps(unblinding), encoding="utf-8")
    return paths, {"plan": plan, "grader": grader, "map": unblinding}


def _write_arms(
    paths: dict[str, Path], docs: dict[str, Any], labels: list[str] | None = None
) -> list[Path]:
    plan = docs["plan"]
    assignments = docs["map"]["assignments"]
    written: list[Path] = []
    for index, arm in enumerate(plan["arms"]):
        label = arm["blind_label"]
        if labels is not None and label not in labels:
            continue
        condition = assignments[label]
        target = Path(arm["output_root"]) / f"{label}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {
                    "schema": "megamind/evaluation-arm-output/v1",
                    "plan_id": plan["plan_id"],
                    "task_set": plan["task_set"],
                    "arm_label": label,
                    "wiki_sha256": arm["wiki_sha256"],
                    "session_id": f"session-{index}",
                    "results": [
                        {
                            "task_id": task_id,
                            "answer": answer,
                            "authorized_context_chars": 0 if condition == "no-wiki" else 120,
                            "provenance": [] if condition == "no-wiki" else _PROVENANCE[task_id],
                            "privacy_violations": 0,
                            "model_access_violations": 0,
                        }
                        for task_id, answer in sorted(_CONDITION_ANSWERS[condition].items())
                    ],
                }
            ),
            encoding="utf-8",
        )
        written.append(target)
    return written


def test_plan_blinds_conditions_with_a_seed_dependent_permutation(tmp_path: Path) -> None:
    seen: set[tuple[tuple[str, str], ...]] = set()
    for seed in range(8):
        workspace = tmp_path / f"seed{seed}"
        workspace.mkdir()
        _, docs = _plan(workspace, seed=seed)
        assignments = docs["map"]["assignments"]
        assert sorted(assignments) == ["arm-0", "arm-1", "arm-2"]
        assert sorted(assignments.values()) == ["current-wiki", "no-wiki", "updated-wiki"]
        seen.add(tuple(sorted(assignments.items())))
    # The seed genuinely moves the assignment; it is not a fixed alphabetical map.
    assert len(seen) > 1


def test_plan_is_reproducible_for_the_same_seed(tmp_path: Path) -> None:
    _first_paths, first = _plan(tmp_path / "a", seed=11)
    _second_paths, second = _plan(tmp_path / "b", seed=11)
    assert first["map"]["assignments"] == second["map"]["assignments"]


def test_grader_packet_carries_no_condition_seed_or_snapshot(tmp_path: Path) -> None:
    _, docs = _plan(tmp_path)
    grader = dict(docs["grader"])
    grader.pop("help", None)
    body = json.dumps(grader)
    for leak in ("no-wiki", "current-wiki", "updated-wiki", "seed", "wiki_sha256", "output_root"):
        assert leak not in body, leak
    plan = json.dumps(docs["plan"])
    for leak in ("no-wiki", "current-wiki", "updated-wiki"):
        assert leak not in plan, leak


def test_validation_needs_no_unblinding_map(tmp_path: Path) -> None:
    paths, docs = _plan(tmp_path)
    outputs = _write_arms(paths, docs)
    result = validate_experiment(paths["artifacts"] / "plan.json", outputs)
    assert result["status"] == "valid"
    assert result["help"]
    assert "assignments" not in json.dumps(result)


def test_score_seals_blind_scores_before_unblinding(tmp_path: Path) -> None:
    paths, docs = _plan(tmp_path)
    outputs = _write_arms(paths, docs)
    result = score_experiment(
        paths["artifacts"] / "plan.json", outputs, paths["artifacts"] / "map.json"
    )
    assert result["status"] == "promoted"
    assert set(result["arm_scores"]) == {"arm-0", "arm-1", "arm-2"}
    assert result["blind_scores_sha256"]
    assert result["help"]


def test_incomplete_arms_are_typed_unsettled_not_a_traceback(tmp_path: Path) -> None:
    paths, docs = _plan(tmp_path)
    labels = [arm["blind_label"] for arm in docs["plan"]["arms"]]
    outputs = _write_arms(paths, docs, labels=labels[:2])
    plan_path = paths["artifacts"] / "plan.json"
    validation = validate_experiment(plan_path, outputs)
    assert validation["status"] == "unsettled"
    assert validation["missing_arms"] == [labels[2]]
    score = score_experiment(plan_path, outputs, paths["artifacts"] / "map.json")
    assert score["schema_version"] == "megamind/evaluation-score/v1"
    assert score["status"] == "unsettled"
    assert score["missing_arms"] == [labels[2]]
    assert "arm_scores" not in score
    assert score["help"]


def test_score_refuses_a_tampered_unblinding_map(tmp_path: Path) -> None:
    paths, docs = _plan(tmp_path)
    outputs = _write_arms(paths, docs)
    tampered = dict(docs["map"])
    tampered["assignments"] = {
        "arm-0": "current-wiki",
        "arm-1": "no-wiki",
        "arm-2": "updated-wiki",
    }
    if tampered["assignments"] == docs["map"]["assignments"]:
        tampered["assignments"] = {
            "arm-0": "updated-wiki",
            "arm-1": "no-wiki",
            "arm-2": "current-wiki",
        }
    bad_map = paths["artifacts"] / "bad-map.json"
    bad_map.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(EvaluationError, match="unblinding map"):
        score_experiment(paths["artifacts"] / "plan.json", outputs, bad_map)


def test_score_refuses_a_map_from_another_plan(tmp_path: Path) -> None:
    paths, docs = _plan(tmp_path / "a")
    other_paths, _other = _plan(tmp_path / "b", seed=3)
    outputs = _write_arms(paths, docs)
    with pytest.raises(EvaluationError, match="different plan"):
        score_experiment(
            paths["artifacts"] / "plan.json", outputs, other_paths["artifacts"] / "map.json"
        )


def test_plan_rejects_threshold_tampering(tmp_path: Path) -> None:
    thresholds = tmp_path / "thresholds.toml"
    thresholds.write_text(THRESHOLDS.read_text(encoding="utf-8"), encoding="utf-8")
    paths = _workspace(tmp_path)
    plan, _grader, _map = plan_experiment(
        TASKS,
        paths["none"],
        paths["current"],
        paths["updated"],
        RUBRIC,
        thresholds,
        "synthetic-model",
        "none",
        "fixed",
        3,
        paths["outputs"],
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    thresholds.write_text("[release]\nexact_accuracy_min = 0.99\n", encoding="utf-8")
    with pytest.raises(EvaluationError, match="frozen evaluation input"):
        _load_plan(plan_path)


def test_plan_rejects_a_task_set_the_thresholds_do_not_bind(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks.json"
    payload = json.loads(TASKS.read_text(encoding="utf-8"))
    payload["tasks"][0]["required_terms"] = ["something-else"]
    tasks.write_text(json.dumps(payload), encoding="utf-8")
    paths = _workspace(tmp_path)
    with pytest.raises(EvaluationError, match="binding"):
        plan_experiment(
            tasks,
            paths["none"],
            paths["current"],
            paths["updated"],
            RUBRIC,
            THRESHOLDS,
            "synthetic-model",
            "none",
            "fixed",
            3,
            paths["outputs"],
        )


def test_plan_identity_tampering_is_refused(tmp_path: Path) -> None:
    paths, docs = _plan(tmp_path)
    plan = dict(docs["plan"])
    plan["status"] = "approved"
    plan_path = paths["artifacts"] / "tampered.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(EvaluationError, match="tampered"):
        _load_plan(plan_path)


def _drop_updated_provenance(paths: dict[str, Path], docs: dict[str, Any]) -> list[Path]:
    outputs = _write_arms(paths, docs)
    assignments = docs["map"]["assignments"]
    updated_label = next(label for label, name in assignments.items() if name == "updated-wiki")
    updated_path = next(path for path in outputs if path.stem == updated_label)
    payload = json.loads(updated_path.read_text(encoding="utf-8"))
    payload["results"][0]["provenance"] = []
    updated_path.write_text(json.dumps(payload), encoding="utf-8")
    return outputs


def test_provenance_tolerance_allows_regression_up_to_the_preregistered_bound(
    tmp_path: Path,
) -> None:
    """A tolerance named *_regression_max must permit regression, never demand gain."""
    tolerant = tmp_path / "tolerant-thresholds.toml"
    tolerant.write_text(
        THRESHOLDS.read_text(encoding="utf-8").replace(
            "provenance_regression_max = 0.00", "provenance_regression_max = 0.50"
        ),
        encoding="utf-8",
    )
    paths, docs = _plan(tmp_path / "tolerant", thresholds=tolerant)
    outputs = _drop_updated_provenance(paths, docs)
    result = score_experiment(
        paths["artifacts"] / "plan.json", outputs, paths["artifacts"] / "map.json"
    )
    assert result["comparison"]["provenance_delta"] < 0
    assert result["status"] == "promoted"

    # The shipped zero tolerance still refuses the same regression.
    strict_paths, strict_docs = _plan(tmp_path / "strict")
    strict_outputs = _drop_updated_provenance(strict_paths, strict_docs)
    strict = score_experiment(
        strict_paths["artifacts"] / "plan.json",
        strict_outputs,
        strict_paths["artifacts"] / "map.json",
    )
    assert strict["status"] == "rollback-required"
    assert strict["rollback_ref"]


def test_record_requires_a_nonempty_rollback_reference(tmp_path: Path) -> None:
    from megamind.evaluation import record_evaluation

    score = {
        "schema_version": "megamind/evaluation-score/v1",
        "plan_id": "abc123",
        "status": "rollback-required",
        "rollback_ref": "",
        "summary": {"prompt_leak": False, "canary_leak": False},
        "comparison": {"target_improvement": -0.5},
        "gates": {"passed": False},
    }
    with pytest.raises(EvaluationError, match="rollback reference"):
        record_evaluation(tmp_path, score)
    score["rollback_ref"] = "   "
    with pytest.raises(EvaluationError, match="rollback reference"):
        record_evaluation(tmp_path, score)
    score["rollback_ref"] = "evaluation:abc123"
    result = record_evaluation(tmp_path, score)
    assert result["status"] == "recorded"
    journal = (tmp_path / ".megamind" / "audit" / "evaluations.jsonl").read_text(encoding="utf-8")
    assert "prompt" not in journal.lower()
    assert "canary" not in journal.lower()


def test_record_refuses_an_unsafe_out_before_appending_anything(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, docs = _plan(tmp_path)
    outputs = _write_arms(paths, docs)
    score = score_experiment(
        paths["artifacts"] / "plan.json", outputs, paths["artifacts"] / "map.json"
    )
    score_path = paths["artifacts"] / "score.json"
    score_path.write_text(json.dumps(score), encoding="utf-8")
    code, doc, _ = run_json(
        capsys,
        "experiment",
        "record",
        "--score",
        str(score_path),
        "--audit-root",
        str(paths["audit"]),
        "--out",
        str(paths["audit"] / "record.json"),
    )
    assert code == 1
    assert doc["code"] == "evaluation_invalid"
    assert not (paths["audit"] / ".megamind").exists()


# --- CLI surface -------------------------------------------------------------


def test_experiment_flow_through_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = _workspace(tmp_path)
    artifacts = paths["artifacts"]
    code, plan_doc, err = run_json(
        capsys,
        "experiment",
        "plan",
        "--tasks",
        str(TASKS),
        "--no-wiki",
        str(paths["none"]),
        "--current-wiki",
        str(paths["current"]),
        "--updated-wiki",
        str(paths["updated"]),
        "--rubric",
        str(RUBRIC),
        "--thresholds",
        str(THRESHOLDS),
        "--model",
        "synthetic-model-v1",
        "--seed",
        "7",
        "--output-root",
        str(paths["outputs"]),
        "--out",
        str(artifacts / "plan.json"),
        "--grader-out",
        str(artifacts / "grader.json"),
        "--map-out",
        str(artifacts / "map.json"),
    )
    assert code == 0
    assert err == ""
    assert plan_doc["schema_version"] == "megamind/evaluation-plan/v1"
    assert plan_doc["help"]
    grader = json.loads((artifacts / "grader.json").read_text(encoding="utf-8"))
    unblinding = json.loads((artifacts / "map.json").read_text(encoding="utf-8"))
    assert grader["schema_version"] == "megamind/evaluation-grader-packet/v1"
    assert unblinding["schema_version"] == "megamind/evaluation-unblinding-map/v1"
    outputs = _write_arms(paths, {"plan": plan_doc, "map": unblinding})

    code, doc, _ = run_json(
        capsys,
        "experiment",
        "validate",
        "--plan",
        str(artifacts / "plan.json"),
        "--outputs",
        *[str(path) for path in outputs],
    )
    assert code == 0
    assert doc["status"] == "valid"

    code, doc, _ = run_json(
        capsys,
        "experiment",
        "score",
        "--plan",
        str(artifacts / "plan.json"),
        "--outputs",
        *[str(path) for path in outputs],
        "--unblinding-map",
        str(artifacts / "map.json"),
        "--out",
        str(artifacts / "score.json"),
    )
    assert code == 0
    assert doc["status"] == "promoted"

    code, doc, _ = run_json(
        capsys,
        "experiment",
        "record",
        "--score",
        str(artifacts / "score.json"),
        "--audit-root",
        str(paths["audit"]),
    )
    assert code == 0
    assert doc["status"] == "recorded"
    assert doc["help"]


def test_evaluation_documents_render_identically_in_toon_and_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "results.json"
    code_json, doc, _ = run_json(
        capsys,
        "bench",
        "run",
        "--fixtures",
        str(FIXTURES),
        "--queries",
        str(QUERIES),
        "--thresholds",
        str(THRESHOLDS),
        "--out",
        str(out),
    )
    code_toon = main(
        [
            "bench",
            "run",
            "--fixtures",
            str(FIXTURES),
            "--queries",
            str(QUERIES),
            "--thresholds",
            str(THRESHOLDS),
        ]
    )
    captured = capsys.readouterr()
    assert code_json == code_toon == 0
    assert captured.err == ""
    assert toon.encode(doc) == captured.out


def test_every_evaluation_document_carries_schema_version_and_runnable_help(
    tmp_path: Path,
) -> None:
    """The AXI contract, applied to all six Phase 4 documents plus the failure doc."""
    result = run_benchmark(FIXTURES, QUERIES, THRESHOLDS)
    results_path = tmp_path / "results.json"
    results_path.write_text(json.dumps(result), encoding="utf-8")
    paths, docs = _plan(tmp_path / "experiment")
    outputs = _write_arms(paths, docs)
    plan_path = paths["artifacts"] / "plan.json"
    score = score_experiment(plan_path, outputs, paths["artifacts"] / "map.json")
    from megamind.evaluation import record_evaluation

    documents = [
        result,
        check_benchmark(results_path, THRESHOLDS),
        docs["plan"],
        docs["grader"],
        docs["map"],
        validate_experiment(plan_path, outputs),
        score,
        record_evaluation(paths["audit"], score),
    ]
    for document in documents:
        assert str(document["schema_version"]).startswith("megamind/"), document
        assert document["help"], document["schema_version"]
        for entry in document["help"]:
            assert entry.count("`") % 2 == 0, entry


def test_evaluation_failures_are_typed_documents_with_help(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(
        capsys,
        "bench",
        "check",
        "--results",
        str(tmp_path / "missing.json"),
        "--thresholds",
        str(THRESHOLDS),
    )
    assert code == 1
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "evaluation_invalid"
    assert doc["help"]
