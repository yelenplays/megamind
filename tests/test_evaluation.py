from __future__ import annotations

import json
from pathlib import Path

import pytest

from megamind.evaluation import (
    EvaluationError,
    _load_plan,
    plan_experiment,
    run_benchmark,
    validate_experiment,
)

ROOT = Path(__file__).parents[1]


def test_release_benchmark_is_canonical_and_repeatable() -> None:
    first = run_benchmark(
        ROOT / "evals/fixtures/release-mini",
        ROOT / "evals/queries.jsonl",
        ROOT / "evals/thresholds.toml",
    )
    second = run_benchmark(
        ROOT / "evals/fixtures/release-mini",
        ROOT / "evals/queries.jsonl",
        ROOT / "evals/thresholds.toml",
    )
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert set(first["aggregates"]) == {
        "exact",
        "near",
        "paraphrase",
        "ambiguous",
        "no-match",
        "privacy",
    }
    assert first["safety"]["canary_leaks"] == 0


def test_plan_rejects_threshold_tampering(tmp_path: Path) -> None:
    task = tmp_path / "tasks.json"
    rubric = tmp_path / "rubric.json"
    thresholds = tmp_path / "thresholds.toml"
    task.write_text((ROOT / "evals/experiment-tasks-v1.json").read_text(encoding="utf-8"))
    rubric.write_text((ROOT / "evals/rubric-v1.json").read_text(encoding="utf-8"))
    thresholds.write_text((ROOT / "evals/thresholds.toml").read_text(encoding="utf-8"))
    roots = [tmp_path / name for name in ("none", "current", "updated", "outputs")]
    for root in roots:
        root.mkdir()
    plan = plan_experiment(
        task,
        roots[0],
        roots[1],
        roots[2],
        rubric,
        thresholds,
        "synthetic-model",
        "none",
        "fixed",
        3,
        roots[3],
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    thresholds.write_text("[release]\nexact_accuracy_min = 0.99\n")
    with pytest.raises(EvaluationError, match="frozen evaluation input"):
        _load_plan(plan_path)


def test_incomplete_blinded_experiment_is_unsettled(tmp_path: Path) -> None:
    task = tmp_path / "tasks.json"
    rubric = tmp_path / "rubric.json"
    thresholds = tmp_path / "thresholds.toml"
    for source, target in (
        (ROOT / "evals/experiment-tasks-v1.json", task),
        (ROOT / "evals/rubric-v1.json", rubric),
        (ROOT / "evals/thresholds.toml", thresholds),
    ):
        target.write_text(source.read_text(encoding="utf-8"))
    roots = [tmp_path / name for name in ("none", "current", "updated", "outputs")]
    for root in roots:
        root.mkdir()
    plan = plan_experiment(
        task,
        roots[0],
        roots[1],
        roots[2],
        rubric,
        thresholds,
        "synthetic-model",
        "none",
        "fixed",
        1,
        roots[3],
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    output = tmp_path / "outputs" / "arm-0.json"
    output.write_text("{}")
    with pytest.raises(EvaluationError):
        validate_experiment(plan_path, [output])
