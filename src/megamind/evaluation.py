"""Deterministic, host-executed evaluation contracts.

This module deliberately contains no model, worker, network, or provider code.  The
release benchmark invokes the public executable with synthetic inputs.  The arm
harness validates and scores outputs supplied by a host; it never produces an arm
answer itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from .fsops import atomic_write, atomic_write_path, content_hash, resolve_contained

Doc = dict[str, Any]
BENCH_SCHEMA = "megamind/benchmark-result/v1"
PLAN_SCHEMA = "megamind/evaluation-plan/v1"
VALIDATE_SCHEMA = "megamind/evaluation-validation/v1"
SCORE_SCHEMA = "megamind/evaluation-score/v1"
RECORD_SCHEMA = "megamind/evaluation-record/v1"
TASK_SCHEMA = "megamind/evaluation-task-set/v1"
OUTPUT_SCHEMA = "megamind/evaluation-arm-output/v1"

_TIERS = ("exact", "near", "paraphrase", "ambiguous", "no-match", "privacy")
_SECRET_RE = re.compile(r"(?:canary|secret|password|token|credential)[-_=:]", re.I)


class EvaluationError(ValueError):
    """A malformed or unsafe evaluation input."""

    code = "evaluation_invalid"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> str:
    if not path.is_file():
        raise EvaluationError(f"input file is missing: {path.name}")
    return digest_bytes(path.read_bytes())


def digest_tree(root: Path) -> str:
    if not root.is_dir():
        raise EvaluationError(f"evaluation root is not a directory: {root.name}")
    entries: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise EvaluationError(f"symlink is not allowed in evaluation root: {path.name}")
        if path.is_file():
            entries.append({"path": path.relative_to(root).as_posix(), "sha256": digest_file(path)})
    return digest_bytes(_canonical(entries).encode())


def write_document(path: Path, document: Doc, *, root: Path | None = None) -> None:
    text = _canonical(document) + "\n"
    if root is None:
        atomic_write_path(path.resolve(), text, durable=True)
    else:
        atomic_write(root, path, text, durable=True)


def read_json(path: Path, label: str) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvaluationError(f"malformed {label}") from error
    return value


def read_jsonl(path: Path, label: str) -> list[Doc]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise EvaluationError(f"cannot read {label}") from error
    records: list[Doc] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise EvaluationError(f"malformed {label} line {number}") from error
        if not isinstance(value, dict):
            raise EvaluationError(f"{label} line {number} is not an object")
        records.append(value)
    return records


def _validate_tasks(raw: Any) -> tuple[str, list[Doc]]:
    if not isinstance(raw, dict) or raw.get("schema") != TASK_SCHEMA:
        raise EvaluationError("task set schema is invalid")
    version = raw.get("version")
    tasks = raw.get("tasks")
    if not isinstance(version, str) or not version or not isinstance(tasks, list) or not tasks:
        raise EvaluationError("task set must have a version and non-empty tasks")
    seen: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str):
            raise EvaluationError("each task must have a string id")
        task_id = task["id"]
        if task_id in seen:
            raise EvaluationError(f"duplicate task id: {task_id}")
        seen.add(task_id)
        if not isinstance(task.get("prompt"), str) or not task["prompt"]:
            raise EvaluationError(f"task {task_id} has no prompt")
        if not isinstance(task.get("required_terms", []), list):
            raise EvaluationError(f"task {task_id} required_terms must be a list")
    return version, tasks


def _parse_thresholds(path: Path) -> Doc:
    raw = path.read_text(encoding="utf-8")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = {}
        section: str | None = None
        for line in raw.splitlines():
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                continue
            if "=" not in line:
                raise EvaluationError("malformed thresholds") from None
            key, val = (part.strip() for part in line.split("=", 1))
            try:
                parsed: Any = json.loads(val)
            except json.JSONDecodeError:
                if val in {"true", "false"}:
                    parsed = val == "true"
                else:
                    raise EvaluationError("malformed thresholds") from None
            target = value.setdefault(section, {}) if section else value
            if not isinstance(target, dict):
                raise EvaluationError("malformed thresholds") from None
            target[key] = parsed
    if not isinstance(value, dict):
        raise EvaluationError("thresholds must be an object")
    return value


def _run_public(command: list[str], root: Path) -> Doc:
    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "0"
    process = subprocess.run(
        [sys.executable, "-m", "megamind.cli", "--format", "json", "--root", str(root), *command],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    try:
        value = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise EvaluationError("public CLI returned malformed evaluation output") from error
    if not isinstance(value, dict):
        raise EvaluationError("public CLI returned a non-object")
    return value


def _result_for_query(root: Path, query: Doc) -> Doc:
    text = query.get("query")
    if not isinstance(text, str):
        raise EvaluationError("query has no string query")
    model_class = str(query.get("model_class", "local"))
    preflight = _run_public(["preflight", text, "--model-class", model_class], root)
    route = _run_public(
        ["route", text, "--fields", "path,kind,score,wiki,privacy,chars,confidence,provisional"],
        root,
    )
    candidates = route.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    # `route` is intentionally a vault-local surface; preflight is the model-
    # access authority. The benchmark never treats a route candidate as loaded
    # until the declared model class authorizes it.
    if model_class == "cloud":
        authorized = preflight.get("matches", [])
        candidates = [
            item
            for item in candidates
            if isinstance(item, dict)
            and any(
                isinstance(match, dict) and match.get("wiki") == item.get("wiki")
                for match in authorized
            )
        ]
    names = [str(item.get("wiki", "")) for item in candidates if isinstance(item, dict)]
    expected = query.get("expected_wikis", [])
    if not isinstance(expected, list):
        raise EvaluationError("expected_wikis must be a list")
    expected_names = {str(item) for item in expected}
    tier = str(query.get("tier", ""))
    status = str(preflight.get("status", ""))
    if tier == "no-match":
        ok = status == "no-match" and not candidates
    elif tier == "ambiguous":
        ok = status == "ambiguous" and expected_names.issubset(
            set(str(x.get("wiki", "")) for x in preflight.get("offers", []) if isinstance(x, dict))
        )
    elif tier == "privacy":
        ok = status in {"privacy-filtered", "matched"} and (
            not candidates or bool(expected_names.intersection(names))
        )
    else:
        ok = bool(expected_names.intersection(names))
    context_chars = sum(
        int(item.get("chars", 0))
        for item in candidates
        if isinstance(item, dict) and isinstance(item.get("chars", 0), int)
    )
    budget = route.get("max_context_chars")
    budget_ok = not isinstance(budget, int) or context_chars <= budget
    canary_leak = False
    if model_class == "cloud":
        for item in candidates:
            if not isinstance(item, dict):
                continue
            candidate_path = item.get("path")
            if isinstance(candidate_path, str):
                body_path = root / candidate_path
                if body_path.is_file() and _SECRET_RE.search(body_path.read_text(encoding="utf-8")):
                    canary_leak = True
    return {
        "id": str(query.get("id", "")),
        "tier": tier,
        "status": status,
        "expected_wikis": sorted(expected_names),
        "candidates": names,
        "loaded": [str(item.get("path", "")) for item in candidates if isinstance(item, dict)],
        "context_chars": context_chars,
        "privacy_safe": not canary_leak,
        "budget_ok": budget_ok,
        "ok": bool(ok),
    }


def _baseline(root: Path, queries: list[Doc], mode: str) -> Doc:
    rows: list[Doc] = []
    all_text = ""
    if mode == "stuff":
        for path in sorted(root.rglob("*.md")):
            all_text += path.read_text(encoding="utf-8")
    for query in queries:
        words = set(re.findall(r"[a-z0-9]+", str(query.get("query", "")).lower()))
        hits: list[tuple[int, str]] = []
        for path in sorted(root.rglob("*.md")):
            if mode == "stuff":
                continue
            text = path.read_text(encoding="utf-8").lower()
            hits.append(
                (sum(text.count(word) for word in words), path.relative_to(root).as_posix())
            )
        hits.sort(key=lambda item: (-item[0], item[1]))
        top = [x[1] for x in hits[:3]]
        loaded_chars = sum((root / item).stat().st_size for item in top)
        rows.append(
            {
                "id": query.get("id", ""),
                "context_chars": len(all_text) if mode == "stuff" else loaded_chars,
                "top": [] if mode == "stuff" else top,
            }
        )
    return {
        "queries": rows,
        "context_chars": len(all_text)
        if mode == "stuff"
        else sum(row["context_chars"] for row in rows),
        "canary_leaks": sum(
            1
            for row in rows
            for item in row["top"]
            if _SECRET_RE.search((root / item).read_text(encoding="utf-8"))
        )
        if mode != "stuff"
        else sum(
            1 for path in root.rglob("*.md") if _SECRET_RE.search(path.read_text(encoding="utf-8"))
        ),
    }


def run_benchmark(fixtures: Path, queries_path: Path, thresholds_path: Path) -> Doc:
    query_records = read_jsonl(queries_path, "queries")
    if query_records and query_records[0].get("schema") == "megamind/benchmark-query-set/v1":
        metadata = query_records.pop(0)
        if metadata.get("version") != "release-mini-v1" or metadata.get("frozen") is not True:
            raise EvaluationError("benchmark query set is not the frozen release version")
    queries = query_records
    for query in queries:
        if str(query.get("tier")) not in _TIERS:
            raise EvaluationError(f"unknown benchmark tier: {query.get('tier')}")
    thresholds = _parse_thresholds(thresholds_path)
    rows = [_result_for_query(fixtures, query) for query in queries]
    aggregates: Doc = {}
    for tier in _TIERS:
        tier_rows = [row for row in rows if row["tier"] == tier]
        aggregates[tier] = {
            "n": len(tier_rows),
            "correct": sum(1 for row in tier_rows if row["ok"]),
            "accuracy": (sum(1 for row in tier_rows if row["ok"]) / len(tier_rows))
            if tier_rows
            else None,
            "context_chars_p50": sorted(row["context_chars"] for row in tier_rows)[
                len(tier_rows) // 2
            ]
            if tier_rows
            else 0,
        }
    safety = {
        "canary_leaks": sum(1 for row in rows if not row["privacy_safe"]),
        "budget_violations": sum(1 for row in rows if not row["budget_ok"]),
        "malformed_public_outputs": 0,
        "network_calls": 0,
    }
    result: Doc = {
        "schema_version": BENCH_SCHEMA,
        "benchmark_version": "release-mini-v1",
        "corpus_sha256": digest_tree(fixtures),
        "queries_sha256": digest_file(queries_path),
        "thresholds_sha256": digest_file(thresholds_path),
        "thresholds": thresholds,
        "per_query": rows,
        "aggregates": aggregates,
        "safety": safety,
        "baselines": {
            "grep": _baseline(fixtures, queries, "grep"),
            "stuff": _baseline(fixtures, queries, "stuff"),
        },
        "deterministic": True,
    }
    return result


def _load_plan(path: Path) -> Doc:
    plan = read_json(path, "evaluation plan")
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA:
        raise EvaluationError("evaluation plan schema is invalid")
    if not isinstance(plan.get("plan_id"), str) or not isinstance(plan.get("arms"), list):
        raise EvaluationError("evaluation plan is incomplete")
    frozen = plan.get("frozen")
    if not isinstance(frozen, dict):
        raise EvaluationError("evaluation plan has no frozen inputs")
    task_path = Path(str(plan.get("task_set_path", "")))
    rubric_path = Path(str(plan.get("rubric_path", "")))
    thresholds_path = Path(str(plan.get("thresholds_path", "")))
    expected_digests = {
        task_path: frozen.get("task_set_sha256"),
        rubric_path: frozen.get("rubric_sha256"),
        thresholds_path: frozen.get("thresholds_sha256"),
    }
    for input_path, expected in expected_digests.items():
        if not isinstance(expected, str) or digest_file(input_path) != expected:
            raise EvaluationError("frozen evaluation input was changed after planning")
    identity = dict(plan)
    identity.pop("plan_id", None)
    identity.pop("help", None)
    if content_hash(_canonical(identity)) != plan["plan_id"]:
        raise EvaluationError("evaluation plan identity has been tampered with")
    return plan


def _safe_arm_root(path: Path, label: str) -> str:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise EvaluationError(f"{label} root is not a directory")
    return str(resolved)


def plan_experiment(
    task_path: Path,
    no_wiki: Path,
    current_wiki: Path,
    updated_wiki: Path,
    rubric_path: Path,
    thresholds_path: Path,
    model: str,
    tools: str,
    effort: str,
    seed: int,
    output_root: Path,
) -> Doc:
    raw_tasks = read_json(task_path, "task set")
    task_version, _tasks = _validate_tasks(raw_tasks)
    roots = {
        "no-wiki": _safe_arm_root(no_wiki, "no-wiki"),
        "current-wiki": _safe_arm_root(current_wiki, "current-wiki"),
        "updated-wiki": _safe_arm_root(updated_wiki, "updated-wiki"),
    }
    root_paths = [Path(value) for value in roots.values()]
    if any(
        left == right or left in right.parents or right in left.parents
        for index, left in enumerate(root_paths)
        for right in root_paths[index + 1 :]
    ):
        raise EvaluationError("arm roots must be isolated and distinct")
    output = _safe_arm_root(output_root, "output")
    output_path = Path(output)
    if any(
        output_path == root_path
        or output_path in root_path.parents
        or root_path in output_path.parents
        for root_path in root_paths
    ):
        raise EvaluationError("output root must be isolated from arm roots")
    rubric = read_json(rubric_path, "rubric")
    thresholds = _parse_thresholds(thresholds_path)
    if not isinstance(rubric, dict) or not isinstance(thresholds, dict):
        raise EvaluationError("rubric and thresholds must be objects")
    arms = [
        {
            "id": name,
            "blind_label": f"arm-{index}",
            "wiki_sha256": digest_tree(Path(root)),
            "output_root": str(Path(output) / f"arm-{index}"),
        }
        for index, (name, root) in enumerate(sorted(roots.items()))
    ]
    # The label permutation is deterministic but does not expose the condition in the label.
    rotation = seed % len(arms)
    arms = arms[rotation:] + arms[:rotation]
    frozen = {
        "task_set_version": task_version,
        "task_set_sha256": digest_file(task_path),
        "rubric_sha256": digest_file(rubric_path),
        "thresholds_sha256": digest_file(thresholds_path),
        "model": model,
        "tools": tools,
        "effort": effort,
        "seed": seed,
        "prompts_frozen": True,
        "inputs_frozen": True,
    }
    body = {
        "schema_version": PLAN_SCHEMA,
        "task_set": task_version,
        "task_set_path": str(task_path.resolve()),
        "rubric_path": str(rubric_path.resolve()),
        "thresholds_path": str(thresholds_path.resolve()),
        "frozen": frozen,
        "arms": arms,
        "output_root": output,
        "status": "planned",
    }
    plan_id = content_hash(_canonical(body))
    body["plan_id"] = plan_id
    body["help"] = [
        "Host must execute each arm in an isolated session and submit arm outputs",
        "Run validate before score; Megamind never invokes a model",
    ]
    return body


def _validate_output(plan: Doc, path: Path) -> tuple[str, list[Doc]]:
    raw = read_json(path, "arm output")
    if not isinstance(raw, dict) or raw.get("schema") != OUTPUT_SCHEMA:
        raise EvaluationError("arm output schema is invalid")
    if raw.get("plan_id") != plan.get("plan_id") or raw.get("task_set") != plan.get("task_set"):
        raise EvaluationError("arm output provenance does not match the frozen plan")
    label = raw.get("arm_label")
    arms = {
        str(arm.get("blind_label")): arm for arm in plan.get("arms", []) if isinstance(arm, dict)
    }
    if not isinstance(label, str) or label not in arms:
        raise EvaluationError("arm output has an unknown blind label")
    arm = arms[label]
    try:
        resolve_contained(Path(str(arm["output_root"])), path)
    except (OSError, ValueError) as error:
        raise EvaluationError("arm output is outside its isolated output root") from error
    if raw.get("wiki_sha256") != arm.get("wiki_sha256"):
        raise EvaluationError("arm output wiki snapshot does not match the plan")
    results = raw.get("results")
    if not isinstance(results, list):
        raise EvaluationError("arm output results must be a list")
    task_records = _validate_tasks(read_json(Path(str(plan["task_set_path"])), "task set"))[1]
    task_ids = {str(task.get("id")) for task in task_records}
    prompts = {str(task.get("id")): str(task.get("prompt", "")) for task in task_records}
    seen: set[str] = set()
    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise EvaluationError("arm output session provenance is malformed")
    for row in results:
        if not isinstance(row, dict) or not isinstance(row.get("task_id"), str):
            raise EvaluationError("arm result is malformed")
        task_id = row["task_id"]
        if task_id in seen or (task_ids and task_id not in task_ids):
            raise EvaluationError("arm result has duplicate or unknown task")
        seen.add(task_id)
        if (
            not isinstance(row.get("answer"), str)
            or type(row.get("authorized_context_chars")) is not int
            or row["authorized_context_chars"] < 0
        ):
            raise EvaluationError("arm result answer or context accounting is malformed")
        if _SECRET_RE.search(row["answer"]) or prompts[task_id] in row["answer"]:
            raise EvaluationError("arm output contains a prohibited secret, canary, or prompt")
        if any(other != label and other in row["answer"] for other in arms):
            raise EvaluationError("arm output contains another arm label")
        provenance = row.get("provenance")
        if not isinstance(provenance, list) or any(
            not isinstance(item, str) for item in provenance
        ):
            raise EvaluationError("arm result provenance is malformed")
        privacy_violations = row.get("privacy_violations", 0)
        access_violations = row.get("model_access_violations", 0)
        if type(privacy_violations) is not int or type(access_violations) is not int:
            raise EvaluationError("arm violation counts are malformed")
        if privacy_violations != 0 or access_violations != 0:
            raise EvaluationError("arm output reports a privacy or model-access violation")
    if task_ids and seen != task_ids:
        raise EvaluationError("arm output is missing one or more frozen tasks")
    return label, results


def validate_experiment(plan_path: Path, output_paths: list[Path]) -> Doc:
    plan = _load_plan(plan_path)
    if not output_paths:
        raise EvaluationError("at least one arm output is required")
    labels: set[str] = set()
    all_results: dict[str, list[Doc]] = {}
    for path in output_paths:
        label, rows = _validate_output(plan, path)
        if label in labels:
            raise EvaluationError("duplicate arm output")
        labels.add(label)
        all_results[label] = rows
    expected = {str(arm.get("blind_label")) for arm in plan.get("arms", [])}
    if labels != expected:
        return {
            "schema_version": VALIDATE_SCHEMA,
            "plan_id": plan["plan_id"],
            "status": "unsettled",
            "arms": sorted(labels),
            "missing_arms": sorted(expected - labels),
            "cross_arm_contamination": False,
            "blinding": "not-graded",
        }
    result: Doc = {
        "schema_version": VALIDATE_SCHEMA,
        "plan_id": plan["plan_id"],
        "status": "valid",
        "arms": sorted(labels),
        "task_counts": {label: len(rows) for label, rows in sorted(all_results.items())},
        "cross_arm_contamination": False,
        "blinding": "blind-labels-validated",
    }
    return result


def score_experiment(
    plan_path: Path, output_paths: list[Path], validation: Doc | None = None
) -> Doc:
    plan = _load_plan(plan_path)
    if validation is not None and validation.get("status") != "valid":
        raise EvaluationError("cannot score an invalid experiment")
    labels_and_rows = [_validate_output(plan, path) for path in output_paths]
    tasks_raw = (
        read_json(Path(str(plan.get("task_set_path", ""))), "task set")
        if plan.get("task_set_path")
        else None
    )
    if tasks_raw is None:
        raise EvaluationError("plan does not retain task-set provenance")
    _, tasks = _validate_tasks(tasks_raw)
    by_label = {label: rows for label, rows in labels_and_rows}
    task_by_id = {str(task["id"]): task for task in tasks}
    arm_scores: dict[str, Doc] = {}
    for label, rows in sorted(by_label.items()):
        target = 0
        adjacent = 0
        provenance = 0
        for row in rows:
            task = task_by_id[str(row["task_id"])]
            answer = row["answer"].lower()
            terms = [str(term).lower() for term in task.get("required_terms", [])]
            target += int(all(term in answer for term in terms))
            adjacent_terms = [str(term).lower() for term in task.get("adjacent_terms", [])]
            adjacent += int(all(term in answer for term in adjacent_terms)) if adjacent_terms else 1
            required_sources = [str(item) for item in task.get("required_provenance", [])]
            provenance += int(all(source in row["provenance"] for source in required_sources))
        count = len(rows)
        arm_scores[label] = {
            "n": count,
            "target_accuracy": target / count if count else 0.0,
            "adjacent_accuracy": adjacent / count if count else 0.0,
            "provenance_rate": provenance / count if count else 0.0,
            "context_chars_total": sum(row["authorized_context_chars"] for row in rows),
        }
    # IDs are retained in the plan, but labels are intentionally opaque to this comparison.
    id_by_label = {
        str(arm["blind_label"]): str(arm["id"]) for arm in plan["arms"] if isinstance(arm, dict)
    }
    current = next(
        (arm_scores[label] for label, arm_id in id_by_label.items() if arm_id == "current-wiki"),
        None,
    )
    updated = next(
        (arm_scores[label] for label, arm_id in id_by_label.items() if arm_id == "updated-wiki"),
        None,
    )
    if current is None or updated is None:
        raise EvaluationError("current and updated arms are required")
    thresholds = _parse_thresholds(Path(str(plan["thresholds_path"])))
    gates = thresholds.get("promotion", thresholds)
    improvement = float(updated["target_accuracy"]) - float(current["target_accuracy"])
    adjacent_delta = float(updated["adjacent_accuracy"]) - float(current["adjacent_accuracy"])
    provenance_delta = float(updated["provenance_rate"]) - float(current["provenance_rate"])
    passed = (
        improvement >= float(gates.get("improvement_min", 0.0))
        and adjacent_delta >= -float(gates.get("adjacent_regression_max", 0.0))
        and provenance_delta >= float(gates.get("provenance_regression_max", 0.0))
    )
    outcome = "promoted" if passed else "rollback-required"
    return {
        "schema_version": SCORE_SCHEMA,
        "plan_id": plan["plan_id"],
        "status": outcome,
        "arm_scores": arm_scores,
        "comparison": {
            "target_improvement": improvement,
            "adjacent_delta": adjacent_delta,
            "provenance_delta": provenance_delta,
        },
        "gates": {"promotion": gates, "passed": passed},
        "rollback_ref": f"evaluation:{plan['plan_id']}" if not passed else "",
        "summary": {
            "prompt_leak": False,
            "canary_leak": False,
            "privacy_violations": 0,
            "model_access_violations": 0,
        },
    }


def record_evaluation(audit_root: Path, score: Doc) -> Doc:
    if score.get("schema_version") != SCORE_SCHEMA:
        raise EvaluationError("score document schema is invalid")
    status = score.get("status")
    if not isinstance(status, str) or status not in {"promoted", "rollback-required", "unsettled"}:
        raise EvaluationError("score outcome is malformed")
    if status == "rollback-required" and not isinstance(score.get("rollback_ref"), str):
        raise EvaluationError("rollback-required score has no rollback reference")
    summary = score.get("summary")
    if not isinstance(summary, dict) or any(
        summary.get(key) is True for key in ("prompt_leak", "canary_leak")
    ):
        raise EvaluationError("score contains unsafe summary data")
    plan_id = str(score.get("plan_id", ""))
    if not plan_id:
        raise EvaluationError("score has no plan id")
    path = Path(".megamind") / "audit" / "evaluations.jsonl"
    resolved = resolve_contained(audit_root, path)
    previous = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
    event = {
        "schema": RECORD_SCHEMA,
        "event_id": content_hash(previous + _canonical(score)),
        "plan_id": plan_id,
        "outcome": score.get("status"),
        "safe_summary": {
            "target_improvement": score.get("comparison", {}).get("target_improvement"),
            "gates_passed": score.get("gates", {}).get("passed"),
        },
        "rollback_ref": score.get("rollback_ref", ""),
    }
    atomic_write(audit_root, path, previous + _canonical(event) + "\n", durable=True)
    return {
        "schema_version": RECORD_SCHEMA,
        "status": "recorded",
        "event_id": event["event_id"],
        "plan_id": plan_id,
        "audit_path": path.as_posix(),
        "rollback_ref": event["rollback_ref"],
        "help": ["Evaluation audit event appended without prompt or content"],
    }


def check_benchmark(results_path: Path, thresholds_path: Path) -> Doc:
    result = read_json(results_path, "benchmark results")
    thresholds = _parse_thresholds(thresholds_path)
    if not isinstance(result, dict) or result.get("schema_version") != BENCH_SCHEMA:
        raise EvaluationError("benchmark results schema is invalid")
    if result.get("thresholds_sha256") != digest_file(thresholds_path):
        raise EvaluationError("benchmark results were produced with different thresholds")
    release = thresholds.get("release", thresholds)
    aggregates = result.get("aggregates")
    safety = result.get("safety")
    if not isinstance(aggregates, dict) or not isinstance(safety, dict):
        raise EvaluationError("benchmark results are incomplete")
    gates: dict[str, bool] = {}
    gates["exact_accuracy"] = float(aggregates.get("exact", {}).get("accuracy", 0.0)) >= float(
        release.get("exact_accuracy_min", 0.0)
    )
    gates["near_accuracy"] = float(aggregates.get("near", {}).get("accuracy", 0.0)) >= float(
        release.get("near_accuracy_min", 0.0)
    )
    gates["no_match_accuracy"] = float(
        aggregates.get("no-match", {}).get("accuracy", 0.0)
    ) >= float(release.get("no_match_accuracy_min", 0.0))
    gates["canary_leaks"] = int(safety.get("canary_leaks", 1)) <= int(
        release.get("canary_leaks_max", 0)
    )
    gates["budget_violations"] = int(safety.get("budget_violations", 1)) <= int(
        release.get("budget_violations_max", 0)
    )
    gates["deterministic"] = result.get("deterministic") is True
    return {
        "schema_version": "megamind/benchmark-check/v1",
        "status": "passed" if all(gates.values()) else "failed",
        "results_sha256": digest_file(results_path),
        "thresholds_sha256": digest_file(thresholds_path),
        "gates": gates,
        "failed_gates": sorted(name for name, passed in gates.items() if not passed),
        "help": ["Use the frozen result and threshold files as the release evidence"],
    }
