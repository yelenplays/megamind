"""Independent, offline contracts for the governed-research corpus."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CORPUS = ROOT / "evals/fixtures/research-adversarial"

sys.path.insert(0, str(ROOT / "evals"))
from research_benchmark import CorpusError, load_corpus, sha256_file, tree_digest  # noqa: E402


def test_corpus_loads_with_all_required_scenarios() -> None:
    corpus = load_corpus(CORPUS)
    cases = {case["id"]: case for case in corpus["cases"]}
    assert {"C1", "C2", "A1", "A2"} <= set(cases)
    assert cases["C1"]["expected_typed_outcome"]["status"] == "packet-ready"
    assert cases["C2"]["expected_typed_outcome"]["status"] == "refused"
    assert cases["A1"]["expected_typed_outcome"]["answer_kind"] == "general-method"
    assert cases["A2"]["expected_typed_outcome"]["status"] == "refused"
    assert len(corpus["labels"]) >= 12
    assert len(corpus["mutations"]) >= 25


def test_every_tier_and_domain_has_a_denominator_and_expected_outcome() -> None:
    corpus = load_corpus(CORPUS)
    manifest = corpus["manifest"]
    cases = corpus["cases"]
    for field in ("tier", "domain"):
        observed: dict[str, list[str]] = {}
        for case in cases:
            observed.setdefault(case[field], []).append(case["expected_typed_outcome"]["status"])
        for name, floor in manifest[field + "s"].items():
            assert floor["denominator"] == len(observed[name])
            assert floor["denominator"] > 0
            assert set(floor["expected_statuses"]) == set(observed[name])
            assert floor["minimum_pass_rate"] > 0


def test_mutations_cover_every_declared_adversarial_category() -> None:
    corpus = load_corpus(CORPUS)
    categories = {mutation["category"] for mutation in corpus["mutations"]}
    assert categories == set(corpus["manifest"]["required_categories"])
    by_case = {case["id"]: set(case["mutations"]) for case in corpus["cases"]}
    assert all(mutation["id"] in by_case[mutation["case_id"]] for mutation in corpus["mutations"])
    assert all(mutation["covers"] for mutation in corpus["mutations"])


def test_generated_corpus_is_byte_identical(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "evals"))
    try:
        import gen_research_corpus
    finally:
        sys.path.pop(0)
    target = tmp_path / "corpus"
    gen_research_corpus.generate(target)
    assert tree_digest(
        target,
        [
            path.relative_to(target).as_posix()
            for path in sorted(target.rglob("*"))
            if path.is_file() and path.name not in {"manifest.json", "thresholds.json"}
        ],
    ) == tree_digest(
        CORPUS,
        [
            path.relative_to(CORPUS).as_posix()
            for path in sorted(CORPUS.rglob("*"))
            if path.is_file() and path.name not in {"manifest.json", "thresholds.json"}
        ],
    )
    assert {
        path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()
    } == {path.relative_to(CORPUS).as_posix() for path in CORPUS.rglob("*") if path.is_file()}


def test_absent_manifest_refuses(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="manifest is missing"):
        load_corpus(tmp_path)


def test_changed_snapshot_refuses_as_stale(tmp_path: Path) -> None:
    shutil.copytree(CORPUS, tmp_path / "corpus")
    snapshot = tmp_path / "corpus/snapshots/creatine-primary.md"
    snapshot.write_text(snapshot.read_text(encoding="utf-8") + " drift\n", encoding="utf-8")
    with pytest.raises(CorpusError, match=r"stale|binding"):
        load_corpus(tmp_path / "corpus")


def test_changed_threshold_binding_refuses(tmp_path: Path) -> None:
    shutil.copytree(CORPUS, tmp_path / "corpus")
    thresholds = tmp_path / "corpus/thresholds.json"
    value = json.loads(thresholds.read_text(encoding="utf-8"))
    value["hard_gates"]["privacy_leakage"] = 1
    thresholds.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(CorpusError, match=r"stale|not bound"):
        load_corpus(tmp_path / "corpus")


def test_canaries_are_present_but_unlinked() -> None:
    corpus = load_corpus(CORPUS)
    for canary in corpus["manifest"]["canaries"]:
        path = CORPUS / canary["path"]
        assert path.is_file()
        assert canary["marker"] in path.read_text(encoding="utf-8")
        assert canary["path"] not in (CORPUS / "estate/HealthWiki/INDEX.md").read_text(
            encoding="utf-8"
        )


def test_hidden_holdout_contract_keeps_labels_sealed(tmp_path: Path) -> None:
    holdout = tmp_path / "holdout"
    holdout.mkdir()
    (holdout / "manifest.json").write_text(
        json.dumps(
            {
                "schema": "megamind/research-benchmark-manifest/v1",
                "version": "research-adversarial-v1",
                "labels_published": False,
                "cases": ["H-opaque-001"],
            }
        ),
        encoding="utf-8",
    )
    assert load_corpus(CORPUS, holdout_root=holdout)["manifest"]["holdout"]["supported"]


def test_source_snapshot_hashes_are_bound_to_labels() -> None:
    corpus = load_corpus(CORPUS)
    for label in corpus["labels"]:
        assert sha256_file(CORPUS / label["snapshot_path"]) == label["snapshot_sha256"]
