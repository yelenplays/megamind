"""Deterministic, host-executed evaluation contracts.

This module deliberately contains no model, worker, network, or provider code.  The
release benchmark invokes the public executable with synthetic inputs.  The arm
harness validates and scores outputs supplied by a host; it never produces an arm
answer itself.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .access import effective_policy
from .fsops import atomic_write, atomic_write_path, content_hash, resolve_contained
from .registry import RegistryError, load_registry

Doc = dict[str, Any]
BENCH_SCHEMA = "megamind/benchmark-result/v1"
CHECK_SCHEMA = "megamind/benchmark-check/v1"
PLAN_SCHEMA = "megamind/evaluation-plan/v1"
VALIDATE_SCHEMA = "megamind/evaluation-validation/v1"
SCORE_SCHEMA = "megamind/evaluation-score/v1"
RECORD_SCHEMA = "megamind/evaluation-record/v1"
TASK_SCHEMA = "megamind/evaluation-task-set/v1"
OUTPUT_SCHEMA = "megamind/evaluation-arm-output/v1"
GRADER_SCHEMA = "megamind/evaluation-grader-packet/v1"
MAP_SCHEMA = "megamind/evaluation-unblinding-map/v1"
QUERY_SET_SCHEMA = "megamind/benchmark-query-set/v1"

PREFLIGHT_PREFIX = "megamind/preflight-result/"
ROUTE_PREFIX = "megamind/route-result/"

_TIERS = ("exact", "near", "paraphrase", "ambiguous", "no-match", "privacy")
_CONDITIONS = ("current-wiki", "no-wiki", "updated-wiki")
_SECRET_RE = re.compile(r"(?:canary|secret|password|token|credential)[-_=:]", re.I)

_PLAN_HELP = [
    "Host must execute each arm in an isolated session and submit arm outputs",
    "Keep the unblinding map out of the grader's hands until blind scores are sealed",
]
_BENCH_HELP = [
    "Use the frozen result and threshold files together as the release evidence",
]
_VALIDATE_HELP = [
    "Submit exactly one output per blind arm label before scoring",
]
_SCORE_HELP = [
    "Record the score with `megamind-axi experiment record --score <file> --audit-root <dir>`",
]


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


def guard_output_path(path: Path, forbidden_roots: Iterable[Path] = ()) -> Path:
    """Resolve an evaluation output destination and refuse unsafe ones.

    Evaluation evidence is written outside every evaluated tree: a destination
    inside an evaluated fixture, snapshot, or audit root would mutate the thing
    being measured, and a destination inside any vault would put unbacked-up,
    unaudited content into wiki-owned space. Both are refused here, at the one
    boundary every ``--out`` flag passes through.
    """
    resolved = Path(path).expanduser().resolve()
    for root in forbidden_roots:
        root_resolved = Path(root).expanduser().resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            raise EvaluationError("evaluation output must not be written inside an evaluated root")
    for parent in (resolved.parent, *resolved.parent.parents):
        if (parent / ".megamind").is_dir():
            raise EvaluationError("evaluation output must not be written inside a vault")
    return resolved


def write_document(path: Path, document: Doc, *, forbidden_roots: Iterable[Path] = ()) -> None:
    """Write one canonical typed document to an external destination, atomically."""
    resolved = guard_output_path(path, forbidden_roots)
    atomic_write_path(resolved, _canonical(document) + "\n", durable=True)


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
    if raw.get("frozen") is not True:
        raise EvaluationError("task set must declare frozen: true")
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


def _binding(thresholds: Doc) -> Doc:
    """The identity a threshold file is preregistered against.

    Thresholds are only meaningful for the exact inputs they were frozen for, so
    every threshold file names the task set, corpus, and query digests it binds
    to. A missing binding is refused rather than defaulted: an unbound threshold
    file would silently accept any corpus.
    """
    binding = thresholds.get("binding")
    if not isinstance(binding, dict) or not binding:
        raise EvaluationError("thresholds declare no [binding] identity")
    return binding


def _require_binding(binding: Doc, key: str, actual: str, label: str) -> None:
    expected = binding.get(key)
    if not isinstance(expected, str) or not expected:
        raise EvaluationError(f"thresholds binding is missing {key}")
    if expected != actual:
        raise EvaluationError(f"{label} does not match the preregistered thresholds binding")


class _PublicRunner:
    """Invokes the real public CLI and fails closed on any non-contract answer."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.invocations = 0
        # Every rejected invocation raises, so a benchmark document can only
        # exist when this stayed zero. It is emitted as derived evidence of that.
        self.rejected = 0

    def run(self, command: list[str], expected_prefix: str) -> Doc:
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = "0"
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "megamind.cli",
                "--format",
                "json",
                "--root",
                str(self.root),
                *command,
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        self.invocations += 1
        if process.returncode != 0:
            self.rejected += 1
            raise EvaluationError(
                f"public CLI exited {process.returncode} for `{command[0]}`; "
                "the benchmark refuses to score a failed invocation"
            )
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as error:
            self.rejected += 1
            raise EvaluationError("public CLI returned malformed evaluation output") from error
        if not isinstance(value, dict):
            self.rejected += 1
            raise EvaluationError("public CLI returned a non-object")
        schema = value.get("schema_version")
        if not isinstance(schema, str) or not schema.startswith(expected_prefix):
            self.rejected += 1
            raise EvaluationError(
                f"public CLI returned {schema!r} where {expected_prefix}* was required"
            )
        return value


def _authorized_paths(preflight: Doc, wiki: str) -> tuple[bool, set[str] | None]:
    """What the declared model class authorizes for one wiki, per preflight.

    Returns ``(authorized, allow_list)``. ``allow_list`` of ``None`` means the
    whole route ladder for that wiki is authorized (``full`` access); a set
    means only those exact artifacts may be loaded (``digest-only``). A wiki
    that preflight did not match - filtered, declined, or merely offered as an
    ambiguous choice, which includes every provisional wiki - authorizes
    nothing at all.
    """
    matches = preflight.get("matches", [])
    if not isinstance(matches, list):
        return False, set()
    for match in matches:
        if not isinstance(match, dict) or match.get("wiki", match.get("name")) != wiki:
            continue
        access = str(match.get("access", "none"))
        if access == "full":
            return True, None
        if access == "digest-only":
            allows = match.get("allows", [])
            if not isinstance(allows, list):
                return True, set()
            return True, {str(item) for item in allows}
        return False, set()
    return False, set()


def _canary_paths(root: Path, paths: Iterable[str]) -> list[str]:
    leaked: list[str] = []
    for relative in paths:
        try:
            body = resolve_contained(root, relative)
        except (OSError, ValueError):
            continue
        if body.is_file() and _SECRET_RE.search(body.read_text(encoding="utf-8")):
            leaked.append(relative)
    return leaked


def _result_for_query(runner: _PublicRunner, query: Doc) -> Doc:
    root = runner.root
    text = query.get("query")
    if not isinstance(text, str) or not text:
        raise EvaluationError("query has no string query")
    model_class = str(query.get("model_class", "local"))
    if model_class not in {"local", "cloud"}:
        raise EvaluationError(f"unknown model class: {model_class}")
    preflight = runner.run(["preflight", text, "--model-class", model_class], PREFLIGHT_PREFIX)
    route = runner.run(
        ["route", text, "--fields", "path,kind,score,wiki,privacy,chars,confidence,provisional"],
        ROUTE_PREFIX,
    )
    raw_candidates = route.get("candidates", [])
    candidates = [item for item in raw_candidates if isinstance(item, dict)]

    # `route` is intentionally a vault-local surface; preflight is the model-
    # access authority. The benchmark never treats a route candidate as loaded
    # until the declared model class authorizes it - for every model class, not
    # only cloud. Routing accuracy is measured on what the ladder surfaced;
    # context, canaries, and budgets are measured only on what was authorized.
    names = sorted({str(item.get("wiki", "")) for item in candidates})
    loaded: list[str] = []
    authorization: dict[str, tuple[bool, set[str] | None]] = {}
    for item in candidates:
        wiki = str(item.get("wiki", ""))
        if wiki not in authorization:
            authorization[wiki] = _authorized_paths(preflight, wiki)
        allowed, allow_list = authorization[wiki]
        path = str(item.get("path", ""))
        if not allowed or item.get("kind") == "pointer":
            continue
        if allow_list is not None and path not in allow_list:
            continue
        loaded.append(path)
    loaded.sort()

    expected = query.get("expected_wikis", [])
    if not isinstance(expected, list):
        raise EvaluationError("expected_wikis must be a list")
    expected_names = {str(item) for item in expected}
    tier = str(query.get("tier", ""))
    status = str(preflight.get("status", ""))
    offers = {
        str(item.get("wiki", item.get("name", "")))
        for item in preflight.get("offers", [])
        if isinstance(item, dict)
    }
    if tier == "no-match":
        ok = status == "no-match" and not candidates
    elif tier == "ambiguous":
        ok = status == "ambiguous" and expected_names.issubset(offers)
    else:
        ok = bool(expected_names.intersection(names))

    context_chars = sum(
        int(item.get("chars", 0))
        for item in candidates
        if str(item.get("path", "")) in loaded and isinstance(item.get("chars", 0), int)
    )
    route_context_chars = int(route.get("context_chars", 0) or 0)
    budget = route.get("max_context_chars")
    content_candidates = sum(1 for item in candidates if int(item.get("chars", 0) or 0) > 0)
    # The ladder always keeps its highest scoring candidate, even alone over
    # budget, so a real match never degrades into an empty result. A single
    # oversized artifact is therefore in contract; two or more are not.
    budget_ok = (
        not isinstance(budget, int) or route_context_chars <= budget or content_candidates <= 1
    )

    leaked = _canary_paths(root, loaded)
    access_violations = _model_access_violations(root, model_class, loaded)

    expect_status = query.get("expect_status")
    expect_loaded = query.get("expect_loaded")
    contract_ok = True
    if isinstance(expect_status, str) and expect_status != status:
        contract_ok = False
    if isinstance(expect_loaded, list) and sorted(str(item) for item in expect_loaded) != loaded:
        contract_ok = False
    return {
        "id": str(query.get("id", "")),
        "tier": tier,
        "model_class": model_class,
        "status": status,
        "expected_wikis": sorted(expected_names),
        "candidates": names,
        "loaded": loaded,
        "context_chars": context_chars,
        "route_context_chars": route_context_chars,
        "canary_paths": sorted(leaked),
        "model_access_violations": access_violations,
        "privacy_safe": not leaked,
        "budget_ok": budget_ok,
        "contract_ok": contract_ok,
        "ok": bool(ok),
    }


def _model_access_violations(root: Path, model_class: str, loaded: Sequence[str]) -> int:
    """Re-derive the declared policy for every loaded artifact, independently.

    ``loaded`` is filtered through preflight's answer; this counts the same
    artifacts against ``megamind.access``, the single authority for the derived
    posture. Any disagreement between the two surfaces shows up here instead of
    being reported as a clean run.
    """
    if not loaded:
        return 0
    try:
        registry = load_registry(root)
    except (RegistryError, OSError, UnicodeDecodeError, ValueError):
        # A root whose policy cannot be re-derived counts every load as
        # unverified rather than clean; the run itself reports the cause.
        return len(loaded)
    violations = 0
    for relative in loaded:
        owner = None
        for wiki in registry.wikis:
            prefix = wiki.path.rstrip("/") + "/"
            if relative == wiki.path or relative.startswith(prefix):
                owner = wiki
                break
        if owner is None:
            violations += 1
            continue
        policy = effective_policy(owner)
        access = policy.access_for(model_class)
        denied = access == "none" or policy.routing_mode == "pointer"
        if denied or (access == "digest-only" and relative != owner.digest):
            violations += 1
    return violations


def _baseline(root: Path, queries: list[Doc], mode: str) -> Doc:
    """Honest local baselines: a grep-style top-3 and full-vault stuffing.

    Both are measured in characters, the same unit the routing candidates and
    the context budget promise, so a mixed-language corpus compares like for
    like. Every file is read once for the whole baseline.
    """
    texts: dict[str, str] = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.md"))
    }
    lowered = {name: body.lower() for name, body in texts.items()}
    canary_files = {name for name, body in texts.items() if _SECRET_RE.search(body)}
    all_chars = sum(len(body) for body in texts.values())
    rows: list[Doc] = []
    for query in queries:
        words = set(re.findall(r"[a-z0-9]+", str(query.get("query", "")).lower()))
        if mode == "stuff":
            rows.append({"id": str(query.get("id", "")), "context_chars": all_chars, "top": []})
            continue
        hits = sorted(
            ((sum(body.count(word) for word in words), name) for name, body in lowered.items()),
            key=lambda item: (-item[0], item[1]),
        )
        top = [name for _, name in hits[:3]]
        rows.append(
            {
                "id": str(query.get("id", "")),
                "context_chars": sum(len(texts[name]) for name in top),
                "top": top,
            }
        )
    return {
        "queries": rows,
        "context_chars": all_chars
        if mode == "stuff"
        else sum(int(row["context_chars"]) for row in rows),
        "canary_leaks": len(canary_files)
        if mode == "stuff"
        else sum(1 for row in rows for name in row["top"] if name in canary_files),
    }


def _query_set_metadata(records: list[Doc]) -> tuple[str, list[Doc]]:
    """Require the frozen, registered task-set header before anything is scored."""
    if not records:
        raise EvaluationError("benchmark query set is empty")
    header = records[0]
    if header.get("schema") != QUERY_SET_SCHEMA:
        raise EvaluationError(
            "benchmark query set has no registered "
            f"`{QUERY_SET_SCHEMA}` header; provenance cannot be established"
        )
    version = header.get("version")
    if not isinstance(version, str) or not version:
        raise EvaluationError("benchmark query set header has no version")
    if header.get("frozen") is not True:
        raise EvaluationError("benchmark query set header must declare frozen: true")
    return version, records[1:]


def run_benchmark(fixtures: Path, queries_path: Path, thresholds_path: Path) -> Doc:
    version, queries = _query_set_metadata(read_jsonl(queries_path, "queries"))
    if not queries:
        raise EvaluationError("benchmark query set has no queries")
    seen_ids: set[str] = set()
    for query in queries:
        if str(query.get("tier")) not in _TIERS:
            raise EvaluationError(f"unknown benchmark tier: {query.get('tier')}")
        query_id = str(query.get("id", ""))
        if not query_id or query_id in seen_ids:
            raise EvaluationError("every benchmark query needs a unique id")
        seen_ids.add(query_id)
    thresholds = _parse_thresholds(thresholds_path)
    corpus_sha256 = digest_tree(fixtures)
    queries_sha256 = digest_file(queries_path)
    binding = _binding(thresholds)
    _require_binding(binding, "benchmark_version", version, "benchmark query set version")
    _require_binding(binding, "corpus_sha256", corpus_sha256, "benchmark corpus")
    _require_binding(binding, "queries_sha256", queries_sha256, "benchmark query set")

    runner = _PublicRunner(fixtures)
    rows = [_result_for_query(runner, query) for query in queries]
    aggregates: Doc = {}
    for tier in _TIERS:
        tier_rows = [row for row in rows if row["tier"] == tier]
        correct = sum(1 for row in tier_rows if row["ok"])
        aggregates[tier] = {
            "n": len(tier_rows),
            "correct": correct,
            "accuracy": (correct / len(tier_rows)) if tier_rows else None,
            "context_chars_p50": sorted(int(row["context_chars"]) for row in tier_rows)[
                len(tier_rows) // 2
            ]
            if tier_rows
            else 0,
        }
    safety = {
        "canary_leaks": sum(len(row["canary_paths"]) for row in rows),
        "budget_violations": sum(1 for row in rows if not row["budget_ok"]),
        "model_access_violations": sum(int(row["model_access_violations"]) for row in rows),
        "contract_violations": sum(1 for row in rows if not row["contract_ok"]),
        "public_invocations": runner.invocations,
        "malformed_public_outputs": runner.rejected,
    }
    return {
        "schema_version": BENCH_SCHEMA,
        "benchmark_version": version,
        "corpus_sha256": corpus_sha256,
        "queries_sha256": queries_sha256,
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
        "help": _BENCH_HELP,
    }


def _accuracy(aggregates: Doc, tier: str) -> float:
    """A tier with no queries scores zero, so an absent tier can never pass a gate."""
    value = aggregates.get(tier)
    if not isinstance(value, dict):
        return 0.0
    accuracy = value.get("accuracy")
    if not isinstance(accuracy, (int, float)) or isinstance(accuracy, bool):
        return 0.0
    return float(accuracy)


def _counter(safety: Doc, key: str) -> int:
    value = safety.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        # An absent or malformed safety count fails the gate rather than passing it.
        return 1
    return value


def check_benchmark(results_path: Path, thresholds_path: Path) -> Doc:
    result = read_json(results_path, "benchmark results")
    thresholds = _parse_thresholds(thresholds_path)
    if not isinstance(result, dict) or result.get("schema_version") != BENCH_SCHEMA:
        raise EvaluationError("benchmark results schema is invalid")
    if result.get("thresholds_sha256") != digest_file(thresholds_path):
        raise EvaluationError("benchmark results were produced with different thresholds")
    binding = _binding(thresholds)
    _require_binding(
        binding, "benchmark_version", str(result.get("benchmark_version", "")), "benchmark version"
    )
    _require_binding(
        binding, "corpus_sha256", str(result.get("corpus_sha256", "")), "benchmark corpus"
    )
    _require_binding(
        binding, "queries_sha256", str(result.get("queries_sha256", "")), "benchmark query set"
    )
    release = thresholds.get("release", thresholds)
    aggregates = result.get("aggregates")
    safety = result.get("safety")
    if not isinstance(aggregates, dict) or not isinstance(safety, dict):
        raise EvaluationError("benchmark results are incomplete")
    gates: dict[str, bool] = {
        "exact_accuracy": _accuracy(aggregates, "exact")
        >= float(release.get("exact_accuracy_min", 0.0)),
        "near_accuracy": _accuracy(aggregates, "near")
        >= float(release.get("near_accuracy_min", 0.0)),
        "no_match_accuracy": _accuracy(aggregates, "no-match")
        >= float(release.get("no_match_accuracy_min", 0.0)),
        "privacy_accuracy": _accuracy(aggregates, "privacy")
        >= float(release.get("privacy_accuracy_min", 0.0)),
        "canary_leaks": _counter(safety, "canary_leaks") <= int(release.get("canary_leaks_max", 0)),
        "budget_violations": _counter(safety, "budget_violations")
        <= int(release.get("budget_violations_max", 0)),
        "model_access_violations": _counter(safety, "model_access_violations")
        <= int(release.get("model_access_violations_max", 0)),
        "contract_violations": _counter(safety, "contract_violations")
        <= int(release.get("contract_violations_max", 0)),
        "deterministic": result.get("deterministic") is True,
    }
    return {
        "schema_version": CHECK_SCHEMA,
        "status": "passed" if all(gates.values()) else "failed",
        "benchmark_version": str(result.get("benchmark_version", "")),
        "results_sha256": digest_file(results_path),
        "thresholds_sha256": digest_file(thresholds_path),
        "gates": gates,
        "failed_gates": sorted(name for name, passed in gates.items() if not passed),
        "help": _BENCH_HELP,
    }


# ---------------------------------------------------------------------------
# Three-arm value evaluation
# ---------------------------------------------------------------------------


def _blind_assignment(seed: int, frozen: Doc) -> dict[str, str]:
    """Map each blind label to a condition with a deterministic seeded permutation.

    The permutation is chosen from a digest of the seed and the frozen input
    identity, so a different seed produces a different assignment and the same
    plan always reproduces its own. The grader packet carries neither the seed
    nor this map, so the labels stay opaque to whoever grades the arms.
    """
    material = ":".join(
        [
            str(seed),
            str(frozen.get("task_set_version", "")),
            str(frozen.get("task_set_sha256", "")),
            str(frozen.get("rubric_sha256", "")),
            str(frozen.get("thresholds_sha256", "")),
        ]
    )
    orderings = list(itertools.permutations(_CONDITIONS))
    index = int(hashlib.sha256(material.encode("utf-8")).hexdigest(), 16) % len(orderings)
    return {f"arm-{position}": name for position, name in enumerate(orderings[index])}


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


def _plan_labels(plan: Doc) -> list[str]:
    labels = [str(arm.get("blind_label")) for arm in plan.get("arms", []) if isinstance(arm, dict)]
    if sorted(labels) != sorted(f"arm-{index}" for index in range(len(_CONDITIONS))):
        raise EvaluationError("evaluation plan does not declare the three blind arms")
    return labels


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
) -> tuple[Doc, Doc, Doc]:
    """Freeze every input and blind the arms.

    Returns the plan, the grader packet (blind identities and rubric only), and
    the unblinding map, which the host keeps as a separate execution artifact.
    """
    raw_tasks = read_json(task_path, "task set")
    task_version, tasks = _validate_tasks(raw_tasks)
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
    task_sha256 = digest_file(task_path)
    binding = _binding(thresholds)
    _require_binding(binding, "task_set_version", task_version, "task set version")
    _require_binding(binding, "task_set_sha256", task_sha256, "task set")
    frozen = {
        "task_set_version": task_version,
        "task_set_sha256": task_sha256,
        "rubric_sha256": digest_file(rubric_path),
        "thresholds_sha256": digest_file(thresholds_path),
        "model": model,
        "tools": tools,
        "effort": effort,
        "seed": seed,
        "prompts_frozen": True,
        "inputs_frozen": True,
    }
    assignment = _blind_assignment(seed, frozen)
    # The plan never names a condition next to its label: only the map does.
    arms = [
        {
            "blind_label": label,
            "wiki_sha256": digest_tree(Path(roots[assignment[label]])),
            "output_root": str(output_path / label),
        }
        for label in sorted(assignment)
    ]
    body: Doc = {
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
    body["help"] = _PLAN_HELP
    grader = {
        "schema_version": GRADER_SCHEMA,
        "plan_id": plan_id,
        "task_set": task_version,
        "arms": sorted(assignment),
        "tasks": [
            {"id": str(task["id"]), "prompt": str(task["prompt"])}
            for task in sorted(tasks, key=lambda item: str(item["id"]))
        ],
        "rubric": rubric,
        "rubric_sha256": frozen["rubric_sha256"],
        "help": ["Grade each blind arm without any condition, seed, or snapshot information"],
    }
    unblinding = {
        "schema_version": MAP_SCHEMA,
        "plan_id": plan_id,
        "assignments": assignment,
        "help": ["Keep this map with the host execution record; supply it only at score time"],
    }
    return body, grader, unblinding


def _load_unblinding_map(plan: Doc, path: Path) -> dict[str, str]:
    raw = read_json(path, "unblinding map")
    if not isinstance(raw, dict) or raw.get("schema_version") != MAP_SCHEMA:
        raise EvaluationError("unblinding map schema is invalid")
    if raw.get("plan_id") != plan.get("plan_id"):
        raise EvaluationError("unblinding map belongs to a different plan")
    assignments = raw.get("assignments")
    if not isinstance(assignments, dict):
        raise EvaluationError("unblinding map has no assignments")
    supplied = {str(key): str(value) for key, value in assignments.items()}
    frozen = plan.get("frozen")
    if not isinstance(frozen, dict) or not isinstance(frozen.get("seed"), int):
        raise EvaluationError("evaluation plan has no frozen seed")
    if supplied != _blind_assignment(int(frozen["seed"]), frozen):
        raise EvaluationError("unblinding map does not match the frozen plan seed")
    return supplied


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
        if task_id in seen or task_id not in task_ids:
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
    if seen != task_ids:
        raise EvaluationError("arm output is missing one or more frozen tasks")
    return label, results


def _collect_outputs(plan: Doc, output_paths: list[Path]) -> tuple[dict[str, list[Doc]], list[str]]:
    if not output_paths:
        raise EvaluationError("at least one arm output is required")
    expected = set(_plan_labels(plan))
    collected: dict[str, list[Doc]] = {}
    for path in output_paths:
        label, rows = _validate_output(plan, path)
        if label in collected:
            raise EvaluationError("duplicate arm output")
        collected[label] = rows
    return collected, sorted(expected - set(collected))


def _unsettled(schema: str, plan: Doc, submitted: Iterable[str], missing: list[str]) -> Doc:
    """The typed, bounded outcome for an incomplete experiment: never a promotion."""
    return {
        "schema_version": schema,
        "plan_id": plan["plan_id"],
        "status": "unsettled",
        "arms": sorted(submitted),
        "missing_arms": missing,
        "cross_arm_contamination": False,
        "blinding": "not-graded",
        "help": _VALIDATE_HELP,
    }


def validate_experiment(plan_path: Path, output_paths: list[Path]) -> Doc:
    plan = _load_plan(plan_path)
    collected, missing = _collect_outputs(plan, output_paths)
    if missing:
        return _unsettled(VALIDATE_SCHEMA, plan, collected, missing)
    return {
        "schema_version": VALIDATE_SCHEMA,
        "plan_id": plan["plan_id"],
        "status": "valid",
        "arms": sorted(collected),
        "task_counts": {label: len(rows) for label, rows in sorted(collected.items())},
        "cross_arm_contamination": False,
        "blinding": "blind-labels-validated",
        "help": _VALIDATE_HELP,
    }


def score_experiment(plan_path: Path, output_paths: list[Path], map_path: Path) -> Doc:
    plan = _load_plan(plan_path)
    collected, missing = _collect_outputs(plan, output_paths)
    if missing:
        return _unsettled(SCORE_SCHEMA, plan, collected, missing)
    tasks_raw = (
        read_json(Path(str(plan.get("task_set_path", ""))), "task set")
        if plan.get("task_set_path")
        else None
    )
    if tasks_raw is None:
        raise EvaluationError("plan does not retain task-set provenance")
    _, tasks = _validate_tasks(tasks_raw)
    task_by_id = {str(task["id"]): task for task in tasks}

    # Blind scoring first: every arm is scored by its opaque label only, and the
    # result is sealed with a digest before the unblinding map is even read.
    arm_scores: dict[str, Doc] = {}
    for label, rows in sorted(collected.items()):
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
            "context_chars_total": sum(int(row["authorized_context_chars"]) for row in rows),
        }
    blind_scores_sha256 = content_hash(_canonical(arm_scores))

    assignment = _load_unblinding_map(plan, map_path)
    label_by_condition = {condition: label for label, condition in assignment.items()}
    current = arm_scores.get(label_by_condition.get("current-wiki", ""))
    updated = arm_scores.get(label_by_condition.get("updated-wiki", ""))
    if current is None or updated is None:
        unscored = sorted(set(_plan_labels(plan)) - set(arm_scores))
        return _unsettled(SCORE_SCHEMA, plan, collected, unscored)
    thresholds = _parse_thresholds(Path(str(plan["thresholds_path"])))
    gates = thresholds.get("promotion", thresholds)
    improvement = float(updated["target_accuracy"]) - float(current["target_accuracy"])
    adjacent_delta = float(updated["adjacent_accuracy"]) - float(current["adjacent_accuracy"])
    provenance_delta = float(updated["provenance_rate"]) - float(current["provenance_rate"])
    passed = (
        improvement >= float(gates.get("improvement_min", 0.0))
        and adjacent_delta >= -float(gates.get("adjacent_regression_max", 0.0))
        and provenance_delta >= -float(gates.get("provenance_regression_max", 0.0))
    )
    outcome = "promoted" if passed else "rollback-required"
    return {
        "schema_version": SCORE_SCHEMA,
        "plan_id": plan["plan_id"],
        "status": outcome,
        "arm_scores": arm_scores,
        "blind_scores_sha256": blind_scores_sha256,
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
        "help": _SCORE_HELP,
    }


def record_evaluation(audit_root: Path, score: Doc) -> Doc:
    if score.get("schema_version") != SCORE_SCHEMA:
        raise EvaluationError("score document schema is invalid")
    status = score.get("status")
    if not isinstance(status, str) or status not in {"promoted", "rollback-required", "unsettled"}:
        raise EvaluationError("score outcome is malformed")
    rollback_ref = score.get("rollback_ref")
    if status == "rollback-required" and (
        not isinstance(rollback_ref, str) or not rollback_ref.strip()
    ):
        raise EvaluationError("rollback-required score has no rollback reference")
    summary = score.get("summary")
    if status != "unsettled" and (
        not isinstance(summary, dict)
        or any(summary.get(key) is True for key in ("prompt_leak", "canary_leak"))
    ):
        raise EvaluationError("score contains unsafe summary data")
    plan_id = str(score.get("plan_id", ""))
    if not plan_id:
        raise EvaluationError("score has no plan id")
    path = Path(".megamind") / "audit" / "evaluations.jsonl"
    resolved = resolve_contained(audit_root, path)
    previous = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
    comparison = score.get("comparison")
    event = {
        "schema": RECORD_SCHEMA,
        "event_id": content_hash(previous + _canonical(score)),
        "plan_id": plan_id,
        "outcome": status,
        "safe_summary": {
            "target_improvement": comparison.get("target_improvement")
            if isinstance(comparison, dict)
            else None,
            "gates_passed": score.get("gates", {}).get("passed")
            if isinstance(score.get("gates"), dict)
            else None,
        },
        "rollback_ref": rollback_ref if isinstance(rollback_ref, str) else "",
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
