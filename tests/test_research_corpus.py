"""Independent, offline contracts for the governed-research corpus."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]
CORPUS = ROOT / "evals/fixtures/research-adversarial"

sys.path.insert(0, str(ROOT / "evals"))
try:
    from research_benchmark import (
        CorpusError,
        load_corpus,
        sha256_file,
        transcript_digest,
        tree_digest,
    )
finally:
    sys.path.pop(0)


def _rebind(root: Path) -> None:
    """Recompute the digests a hand-edit would have to forge to pass binding."""
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["corpus_sha256"] = tree_digest(root, manifest["files"])
    manifest["component_digests"] = {
        relative: sha256_file(root / relative) for relative in manifest["component_digests"]
    }
    thresholds_path = root / "thresholds.json"
    thresholds = json.loads(thresholds_path.read_text(encoding="utf-8"))
    thresholds["corpus_sha256"] = manifest["corpus_sha256"]
    thresholds_path.write_text(json.dumps(thresholds), encoding="utf-8")
    manifest["binding_file_digests"] = {"thresholds.json": sha256_file(thresholds_path)}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


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
    assert tree_digest(target) == tree_digest(CORPUS)
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


def test_transcript_digests_are_independently_recomputable() -> None:
    corpus = load_corpus(CORPUS)
    referenced = {name for case in corpus["cases"] for name in case["transcripts"]}
    paths = sorted((CORPUS / "transcripts").glob("*.json"))
    assert paths and referenced
    for path in paths:
        doc = json.loads(path.read_text(encoding="utf-8"))
        payload: dict[str, Any] = {
            "id": doc["id"],
            "video_id": doc["video_id"],
            "upload_date": doc["upload_date"],
            "date_precision": doc["date_precision"],
            "caption_source": doc["caption_source"],
            "segments": [
                {
                    "start": float(segment["start"]),
                    "end": float(segment["end"]),
                    "speaker": segment["speaker"],
                    "text": segment["text"],
                }
                for segment in doc["segments"]
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert doc["transcript_sha256"] == hashlib.sha256((encoded + "\n").encode()).hexdigest()
        assert doc["transcript_sha256"] == transcript_digest(doc)
    assert referenced <= {path.stem for path in paths}


def test_edited_transcript_refuses_even_after_rebinding(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    shutil.copytree(CORPUS, root)
    path = root / "transcripts/transcript-malicious.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["segments"][0]["text"] = "A rewritten synthetic line."
    path.write_text(json.dumps(doc), encoding="utf-8")
    _rebind(root)
    with pytest.raises(CorpusError, match="transcript digest binding failed"):
        load_corpus(root)


def test_case_referencing_an_unknown_transcript_refuses(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    shutil.copytree(CORPUS, root)
    path = root / "cases.jsonl"
    lines = path.read_text(encoding="utf-8").replace('"transcript-malicious"', '"typo-transcript"')
    path.write_text(lines, encoding="utf-8")
    _rebind(root)
    with pytest.raises(CorpusError, match="unknown transcript"):
        load_corpus(root)


def test_thresholds_without_gate_blocks_refuse(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    shutil.copytree(CORPUS, root)
    path = root / "thresholds.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["hard_gates"]
    path.write_text(json.dumps(value), encoding="utf-8")
    _rebind(root)
    with pytest.raises(CorpusError, match="hard_gates are missing"):
        load_corpus(root)


def test_thresholds_without_sealed_holdout_refuse(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    shutil.copytree(CORPUS, root)
    path = root / "thresholds.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["hidden_holdout"]["aggregate_substitution_forbidden"] = False
    path.write_text(json.dumps(value), encoding="utf-8")
    _rebind(root)
    with pytest.raises(CorpusError, match="aggregate substitution"):
        load_corpus(root)


def test_relative_canary_link_refuses(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    shutil.copytree(CORPUS, root)
    index = root / "estate/HealthWiki/INDEX.md"
    index.write_text(
        index.read_text(encoding="utf-8") + "- [Notes](unlinked/private-canary.md)\n",
        encoding="utf-8",
    )
    _rebind(root)
    with pytest.raises(CorpusError, match="canary path is linked"):
        load_corpus(root)


def test_copied_canary_marker_refuses(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    shutil.copytree(CORPUS, root)
    page = root / "estate/HealthWiki/general.md"
    page.write_text(
        page.read_text(encoding="utf-8") + "CANARY-PRIVATE-HEALTH-001\n", encoding="utf-8"
    )
    _rebind(root)
    with pytest.raises(CorpusError, match="canary marker is copied"):
        load_corpus(root)


def test_mutation_naming_a_missing_corpus_path_refuses(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    shutil.copytree(CORPUS, root)
    path = root / "mutations.jsonl"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "estate/HealthWiki/unlinked/private-canary.md",
            "estate/PersonalWiki/unlinked/private-canary.md",
        ),
        encoding="utf-8",
    )
    _rebind(root)
    with pytest.raises(CorpusError, match="missing corpus path"):
        load_corpus(root)


def test_synthetic_estate_registry_loads_with_megamind(tmp_path: Path) -> None:
    from megamind.registry import load_registry

    registry = load_registry(CORPUS / "estate")
    wikis = {wiki.name: wiki for wiki in registry.wikis}
    assert set(wikis) == {"HealthWiki", "FinanceWiki", "ProvisionalWiki"}
    assert all(wiki.path for wiki in registry.wikis)
    assert wikis["ProvisionalWiki"].provisional


def test_generator_refuses_to_replace_a_foreign_directory(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "evals"))
    try:
        import gen_research_corpus
    finally:
        sys.path.pop(0)
    foreign = tmp_path / "fixtures"
    foreign.mkdir()
    (foreign / "keepsake.txt").write_text("not a generated corpus\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a generated corpus"):
        gen_research_corpus.generate(foreign)
    assert (foreign / "keepsake.txt").is_file()
