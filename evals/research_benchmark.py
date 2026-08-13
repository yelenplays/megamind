"""Contracts and fail-closed loader for the frozen research benchmark corpus.

This module is evaluation-only. It contains no research worker, network adapter,
model code, or production record implementation. The corpus is a set of typed,
content-addressed inputs for a host-side benchmark.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

Doc = dict[str, Any]
MANIFEST_SCHEMA = "megamind/research-benchmark-manifest/v1"
CASE_SCHEMA = "megamind/research-benchmark-case/v1"
LABEL_SCHEMA = "megamind/research-source-label/v1"
MUTATION_SCHEMA = "megamind/research-benchmark-mutation/v1"
THRESHOLD_SCHEMA = "megamind/research-benchmark-thresholds/v1"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_DATA = re.compile(
    r"(?:/Users/|/home/|[A-Za-z]:\\|@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\+?\d{1,3}[- .]\d{3}[- .]\d{4})",
    re.IGNORECASE,
)

_CASE_KEYS = {
    "schema",
    "id",
    "tier",
    "domain",
    "question",
    "model_class",
    "sources",
    "transcripts",
    "required_claims",
    "forbidden_claims",
    "required_outcome",
    "expected_typed_outcome",
    "mutations",
    "canary_paths",
}
_LABEL_KEYS = {
    "schema",
    "source_id",
    "snapshot_path",
    "snapshot_sha256",
    "origin_id",
    "origin_family",
    "quality",
    "lifecycle",
    "eligibility",
    "claim_types",
    "injection",
    "rights",
    "date_precision",
    "expected_reason_codes",
}
_MUTATION_KEYS = {
    "schema",
    "id",
    "case_id",
    "category",
    "fixture",
    "operator",
    "expected_typed_outcome",
    "covers",
}


class CorpusError(ValueError):
    """The frozen corpus is absent, stale, incomplete, or malformed."""


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as error:
        raise CorpusError(f"cannot read corpus file: {path.name}") from error


def tree_digest(root: Path, relative_paths: list[str] | None = None) -> str:
    paths = (
        relative_paths
        if relative_paths is not None
        else [
            path.relative_to(root).as_posix()
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        ]
    )
    entries = []
    for relative in sorted(paths):
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise CorpusError(f"manifest names a missing or symlinked file: {relative}")
        entries.append({"path": relative, "sha256": sha256_file(path)})
    return sha256_bytes(canonical(entries))


def _read_json(path: Path, label: str) -> Doc:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorpusError(f"malformed {label}") from error
    if not isinstance(value, dict):
        raise CorpusError(f"{label} must be an object")
    return value


def _read_jsonl(path: Path, label: str) -> list[Doc]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise CorpusError(f"cannot read {label}") from error
    result: list[Doc] = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise CorpusError(f"malformed {label} line {number}") from error
        if not isinstance(value, dict):
            raise CorpusError(f"{label} line {number} is not an object")
        result.append(value)
    return result


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise CorpusError(f"{label} must be a sha256 digest")
    return value


def _check_unknown(value: Doc, allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise CorpusError(f"{label} has unknown field: {unknown[0]}")


def _check_outcome(outcome: Any, label: str) -> None:
    if not isinstance(outcome, dict):
        raise CorpusError(f"{label} must be an object")
    required = {"status", "reason_codes", "writes", "answer_kind"}
    if set(outcome) != required:
        raise CorpusError(f"{label} must contain exactly {sorted(required)}")
    if not isinstance(outcome["status"], str) or not outcome["status"]:
        raise CorpusError(f"{label}.status must be non-empty")
    if not isinstance(outcome["reason_codes"], list) or not all(
        isinstance(item, str) and item for item in outcome["reason_codes"]
    ):
        raise CorpusError(f"{label}.reason_codes must be a list of strings")
    if not isinstance(outcome["writes"], list) or not all(
        isinstance(item, str) for item in outcome["writes"]
    ):
        raise CorpusError(f"{label}.writes must be a list of strings")
    if not isinstance(outcome["answer_kind"], str) or not outcome["answer_kind"]:
        raise CorpusError(f"{label}.answer_kind must be non-empty")


def _check_no_real_data(value: Any, label: str) -> None:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if _FORBIDDEN_DATA.search(text):
        raise CorpusError(f"{label} contains a personal or machine-path pattern")


def _validate_manifest(root: Path) -> tuple[Doc, list[str]]:
    path = root / "manifest.json"
    if not path.is_file():
        raise CorpusError("research corpus manifest is missing")
    manifest = _read_json(path, "manifest")
    _check_unknown(
        manifest,
        {
            "schema",
            "version",
            "frozen",
            "today",
            "corpus_sha256",
            "files",
            "binding_files",
            "binding_file_digests",
            "component_digests",
            "tiers",
            "domains",
            "required_categories",
            "holdout",
            "canaries",
        },
        "manifest",
    )
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("version") != "research-adversarial-v1"
    ):
        raise CorpusError("unsupported research corpus manifest")
    if manifest.get("frozen") is not True or not isinstance(manifest.get("today"), str):
        raise CorpusError("manifest must be frozen and declare today")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(item, str) for item in files):
        raise CorpusError("manifest files must be a non-empty list")
    if len(set(files)) != len(files) or "manifest.json" in files:
        raise CorpusError("manifest files must be unique and exclude manifest.json")
    binding_files = manifest.get("binding_files", [])
    if not isinstance(binding_files, list) or not all(
        isinstance(item, str) and item not in files for item in binding_files
    ):
        raise CorpusError("manifest binding_files are invalid")
    expected = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.name != "manifest.json"
        and path.relative_to(root).as_posix() not in binding_files
    )
    if sorted(files) != expected:
        raise CorpusError("manifest is stale: generated file set changed")
    _require_sha(manifest.get("corpus_sha256"), "manifest.corpus_sha256")
    if tree_digest(root, files) != manifest["corpus_sha256"]:
        raise CorpusError("manifest is stale: corpus digest changed")
    binding_digests = manifest.get("binding_file_digests")
    if not isinstance(binding_digests, dict) or set(binding_digests) != set(binding_files):
        raise CorpusError("manifest binding file digests are missing")
    for relative, digest in binding_digests.items():
        if sha256_file(root / relative) != _require_sha(digest, f"binding file {relative}"):
            raise CorpusError(f"manifest is stale: binding file {relative} changed")
    components = manifest.get("component_digests")
    if not isinstance(components, dict) or not components:
        raise CorpusError("manifest component digests are missing")
    for relative, digest in components.items():
        if relative not in files:
            raise CorpusError(f"component digest names an unknown file: {relative}")
        if sha256_file(root / relative) != _require_sha(digest, f"component {relative}"):
            raise CorpusError(f"manifest is stale: component {relative} changed")
    tiers = manifest.get("tiers")
    domains = manifest.get("domains")
    for name, values in (("tiers", tiers), ("domains", domains)):
        if not isinstance(values, dict) or not values:
            raise CorpusError(f"manifest {name} denominators are missing")
        for key, item in values.items():
            if not isinstance(item, dict) or set(item) != {
                "denominator",
                "minimum_pass_rate",
                "expected_statuses",
            }:
                raise CorpusError(f"manifest {name}.{key} floor is incomplete")
            if not isinstance(item["denominator"], int) or item["denominator"] <= 0:
                raise CorpusError(f"manifest {name}.{key} denominator is not positive")
            rate = item["minimum_pass_rate"]
            if not isinstance(rate, (int, float)) or isinstance(rate, bool) or not 0 <= rate <= 1:
                raise CorpusError(f"manifest {name}.{key} pass floor is invalid")
            if not isinstance(item["expected_statuses"], list) or not item["expected_statuses"]:
                raise CorpusError(f"manifest {name}.{key} expected outcomes are missing")
    required_categories = manifest.get("required_categories")
    if (
        not isinstance(required_categories, list)
        or not required_categories
        or not all(isinstance(item, str) for item in required_categories)
    ):
        raise CorpusError("manifest required mutation categories are missing")
    holdout = manifest.get("holdout")
    if not isinstance(holdout, dict) or holdout.get("supported") is not True:
        raise CorpusError("manifest hidden-holdout support is missing")
    if holdout.get("labels_published") is not False:
        raise CorpusError("hidden holdout labels must not be published")
    canaries = manifest.get("canaries")
    if not isinstance(canaries, list) or not canaries:
        raise CorpusError("manifest canaries are missing")
    return manifest, files


def load_corpus(root: Path, *, holdout_root: Path | None = None) -> Doc:
    """Load and validate a frozen corpus, refusing absent or stale manifests."""
    root = root.resolve()
    manifest, files = _validate_manifest(root)
    cases = _read_jsonl(root / "cases.jsonl", "cases")
    labels = _read_jsonl(root / "labels.jsonl", "labels")
    mutations = _read_jsonl(root / "mutations.jsonl", "mutations")
    thresholds = _read_json(root / "thresholds.json", "thresholds")
    if (
        thresholds.get("schema") != THRESHOLD_SCHEMA
        or thresholds.get("version") != manifest["version"]
    ):
        raise CorpusError("thresholds are not bound to this corpus version")
    if thresholds.get("corpus_sha256") != manifest["corpus_sha256"]:
        raise CorpusError("thresholds are not bound to this corpus")
    floors = thresholds.get("per_domain_hard_floors")
    if not isinstance(floors, dict) or set(floors) != set(manifest["domains"]):
        raise CorpusError("thresholds do not provide every per-domain hard floor")
    for domain, floor in floors.items():
        declared = manifest["domains"][domain]
        if not isinstance(floor, dict) or floor.get("denominator") != declared["denominator"]:
            raise CorpusError(f"threshold floor denominator is stale for {domain}")
        if floor.get("minimum_pass_rate") != declared["minimum_pass_rate"]:
            raise CorpusError(f"threshold floor is stale for {domain}")
    label_map: dict[str, Doc] = {}
    for label in labels:
        _check_unknown(label, _LABEL_KEYS, "source label")
        if label.get("schema") != LABEL_SCHEMA or not isinstance(label.get("source_id"), str):
            raise CorpusError("source label schema is invalid")
        source_id = label["source_id"]
        if source_id in label_map:
            raise CorpusError(f"duplicate source label: {source_id}")
        label_map[source_id] = label
        snapshot = root / str(label.get("snapshot_path", ""))
        if not snapshot.is_file() or sha256_file(snapshot) != label.get("snapshot_sha256"):
            raise CorpusError(f"source snapshot binding failed: {source_id}")
        _check_no_real_data(label, f"source label {source_id}")
    case_map: dict[str, Doc] = {}
    for case in cases:
        _check_unknown(case, _CASE_KEYS, "benchmark case")
        if case.get("schema") != CASE_SCHEMA or not isinstance(case.get("id"), str):
            raise CorpusError("benchmark case schema is invalid")
        case_id = case["id"]
        if case_id in case_map:
            raise CorpusError(f"duplicate benchmark case: {case_id}")
        case_map[case_id] = case
        for field in ("sources", "transcripts", "required_claims", "forbidden_claims", "mutations"):
            if not isinstance(case.get(field), list) or not all(
                isinstance(item, str) for item in case[field]
            ):
                raise CorpusError(f"case {case_id}.{field} must be a list of strings")
        _check_outcome(case.get("expected_typed_outcome"), f"case {case_id}.expected_typed_outcome")
        if not isinstance(case.get("required_outcome"), str) or not case["required_outcome"]:
            raise CorpusError(f"case {case_id} has no required outcome")
        for source_id in case["sources"]:
            if source_id not in label_map:
                raise CorpusError(f"case {case_id} references unknown source: {source_id}")
        for relative in case.get("canary_paths", []):
            if relative not in files:
                raise CorpusError(f"case {case_id} names a missing canary: {relative}")
        _check_no_real_data(case, f"case {case_id}")
    for relative in files:
        try:
            _check_no_real_data((root / relative).read_text(encoding="utf-8"), relative)
        except UnicodeDecodeError:
            pass
    for field in ("tier", "domain"):
        observed: dict[str, dict[str, Any]] = {}
        for case in case_map.values():
            bucket = observed.setdefault(
                str(case[field]), {"denominator": 0, "expected_statuses": []}
            )
            bucket["denominator"] += 1
            status = case["expected_typed_outcome"]["status"]
            if status not in bucket["expected_statuses"]:
                bucket["expected_statuses"].append(status)
        declared = manifest[field + "s"]
        if set(declared) != set(observed):
            raise CorpusError(f"manifest {field} floors do not cover every case")
        for name, actual in observed.items():
            floor = declared[name]
            if floor["denominator"] != actual["denominator"] or set(
                floor["expected_statuses"]
            ) != set(actual["expected_statuses"]):
                raise CorpusError(f"manifest {field}.{name} denominator or outcomes are stale")
    mutation_map: dict[str, Doc] = {}
    categories: set[str] = set()
    for mutation in mutations:
        _check_unknown(mutation, _MUTATION_KEYS, "mutation")
        if mutation.get("schema") != MUTATION_SCHEMA or not isinstance(mutation.get("id"), str):
            raise CorpusError("mutation schema is invalid")
        mutation_id = mutation["id"]
        if mutation_id in mutation_map:
            raise CorpusError(f"duplicate mutation: {mutation_id}")
        if mutation.get("case_id") not in case_map:
            raise CorpusError(f"mutation {mutation_id} references unknown case")
        if not isinstance(mutation.get("category"), str) or not mutation["category"]:
            raise CorpusError(f"mutation {mutation_id} has no category")
        _check_outcome(
            mutation.get("expected_typed_outcome"), f"mutation {mutation_id}.expected_typed_outcome"
        )
        covers = mutation.get("covers")
        if (
            not isinstance(covers, list)
            or not covers
            or not all(isinstance(item, str) for item in covers)
        ):
            raise CorpusError(f"mutation {mutation_id} has no gate coverage")
        mutation_map[mutation_id] = mutation
        categories.add(mutation["category"])
        _check_no_real_data(mutation, f"mutation {mutation_id}")
    for case_id, case in case_map.items():
        if set(case["mutations"]) != {
            mid for mid, item in mutation_map.items() if item["case_id"] == case_id
        }:
            raise CorpusError(f"case {case_id} mutation coverage is incomplete")
    missing_categories = set(manifest["required_categories"]) - categories
    if missing_categories:
        raise CorpusError(f"mutation coverage is incomplete: {sorted(missing_categories)[0]}")
    for canary in manifest["canaries"]:
        if not isinstance(canary, dict) or not isinstance(canary.get("path"), str):
            raise CorpusError("manifest canary entry is invalid")
        canary_path = canary["path"]
        if canary_path not in files:
            raise CorpusError(f"manifest canary is missing: {canary_path}")
        for relative in files:
            if relative == canary_path or not relative.endswith((".md", ".txt")):
                continue
            try:
                body = (root / relative).read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if canary_path in body:
                raise CorpusError(f"canary path is linked: {canary_path}")
    if holdout_root is not None:
        _validate_holdout(holdout_root, manifest)
    return {
        "manifest": manifest,
        "cases": cases,
        "labels": labels,
        "mutations": mutations,
        "thresholds": thresholds,
        "files": files,
    }


def _validate_holdout(root: Path, manifest: Doc) -> None:
    """Validate a separately supplied holdout without requiring its labels in public data."""
    holdout_manifest = root / "manifest.json"
    if not holdout_manifest.is_file():
        raise CorpusError("hidden holdout manifest is missing")
    value = _read_json(holdout_manifest, "hidden holdout manifest")
    if value.get("schema") != MANIFEST_SCHEMA or value.get("version") != manifest["version"]:
        raise CorpusError("hidden holdout manifest is not bound to the corpus version")
    if value.get("labels_published") is not False or not isinstance(value.get("cases"), list):
        raise CorpusError("hidden holdout must keep labels sealed")
    for case_id in value["cases"]:
        if not isinstance(case_id, str) or not case_id.startswith("H"):
            raise CorpusError("hidden holdout case identities must be opaque")


def assert_complete(root: Path) -> None:
    """Small assertion entry point useful to release scripts and CI."""
    load_corpus(root)
