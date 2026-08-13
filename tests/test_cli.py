"""AXI contract tests: one typed document on stdout, TOON/JSON parity,
definitive empty states, truncation, help[], structured errors, exit codes."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import build_vault
from megamind import toon
from megamind.cli import DIFF_LINE_LIMIT, main
from megamind.scaffold import init_wiki_root


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    code = main(["--format", "json", *argv])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def run_toon(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- home -------------------------------------------------------------------


def test_home_uninitialized_is_definitive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, err = run_json(capsys, "--root", str(tmp_path))
    assert code == 0
    assert err == ""
    assert doc["schema_version"] == "megamind/home/v1"
    assert doc["executable"] == "megamind-axi"
    assert doc["initialized"] is False
    assert any("init" in entry for entry in doc["help"])


def test_home_shows_aggregates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(capsys, "--root", str(vault), "--today", "2026-03-01")
    assert code == 0
    assert doc["initialized"] is True
    assert {w["name"] for w in doc["wikis"]} >= {"ProductWiki", "BrandingWiki"}
    assert set(doc["wikis"][0]) == {"name", "privacy", "pages"}
    assert doc["proposals"] == {"open": 0, "applied": 0, "rejected": 0}
    assert doc["doctor"] == {"errors": 0, "warnings": 0}
    assert "review" in doc
    assert doc["help"]


# --- output contract ---------------------------------------------------------


def test_toon_and_json_render_the_same_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    code_j, doc, _ = run_json(capsys, "--root", str(vault), "route", "pricing", "model")
    code_t, toon_out, err = run_toon(capsys, "--root", str(vault), "route", "pricing", "model")
    assert code_j == code_t == 0
    assert err == ""
    assert toon.encode(doc) == toon_out


def test_stdout_is_exactly_one_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, err = run_json(capsys, "--root", str(vault), "doctor")
    assert code == 0
    assert err == ""
    assert isinstance(doc, dict)  # json.loads succeeded on the whole stream


def test_no_help_hints_strips_help(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    _, doc, _ = run_json(capsys, "--root", str(vault), "--no-help-hints", "review")
    assert "help" not in doc


def test_every_document_has_schema_version_and_help(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    for argv in (
        ["--root", str(vault)],
        ["--root", str(vault), "route", "pricing"],
        ["--root", str(vault), "review"],
        ["--root", str(vault), "doctor"],
        ["--root", str(vault), "config", "show"],
        ["setup", "skill"],
    ):
        _, doc, _ = run_json(capsys, *argv)
        assert doc["schema_version"].startswith("megamind/")
        assert doc["help"], argv


# --- init --------------------------------------------------------------------


def test_init_and_reinit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "fresh"
    code, doc, _ = run_json(capsys, "init", str(target))
    assert code == 0
    assert doc["schema_version"] == "megamind/init-result/v1"
    assert doc["status"] == "initialized"
    assert ".megamind/registry.json" in doc["created"]
    code, doc, _ = run_json(capsys, "init", str(target))
    assert code == 0
    assert doc["status"] == "already_initialized"


# --- route -------------------------------------------------------------------


def test_route_uses_the_authoritative_card_at_a_canonical_wiki_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    card_path = root / ".megamind/wiki-card.json"
    card = json.loads(card_path.read_text(encoding="utf-8"))
    card["keywords"] = ["synthetic", "lighting"]
    card_path.write_text(json.dumps(card), encoding="utf-8")
    (root / "wiki/index.md").write_text(
        "# Index\n\n- [Synthetic lighting](lighting.md)\n", encoding="utf-8"
    )
    (root / "wiki/lighting.md").write_text("# Synthetic lighting\n", encoding="utf-8")

    code, doc, err = run_json(capsys, "--root", str(root), "route", "synthetic", "lighting")

    assert code == 0
    assert err == ""
    assert doc["schema_version"] == "megamind/route-result/v2"
    assert doc["decision"] == "load"
    assert doc["candidates"][0]["path"] == "wiki/lighting.md"


def test_canonical_wiki_route_uses_its_declared_context_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    card_path = root / ".megamind/wiki-card.json"
    card = json.loads(card_path.read_text(encoding="utf-8"))
    card["keywords"] = ["synthetic"]
    card["context_budget"] = {"max_candidates": 1, "max_context_chars": 700}
    card_path.write_text(json.dumps(card), encoding="utf-8")

    code, doc, _ = run_json(capsys, "--root", str(root), "route", "synthetic")

    assert code == 0
    assert doc["max_candidates"] == 1
    assert doc["max_context_chars"] == 700


def test_route_default_fields_are_minimal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(capsys, "--root", str(vault), "route", "pricing", "model")
    assert code == 0
    assert doc["matched"] is True
    best = doc["candidates"][0]
    assert list(best) == ["path", "kind", "score", "reason"]
    assert best["path"] == "ProductWiki/topics/pricing-model.md"
    assert best["reason"]


def test_route_fields_opt_in(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(
        capsys, "--root", str(vault), "route", "pricing", "--fields", "path,wiki,privacy,reasons"
    )
    assert code == 0
    assert list(doc["candidates"][0]) == ["path", "wiki", "privacy", "reasons"]


def test_route_unknown_field_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(capsys, "--root", str(vault), "route", "x", "--fields", "path,bogus")
    assert code == 2
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "usage_error"


def test_route_no_match_is_structured_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    code, doc, err = run_json(capsys, "--root", str(vault), "route", "llama", "farming")
    assert code == 0
    assert err == ""
    assert doc["matched"] is False
    assert doc["candidates"] == []
    assert doc["help"]


# --- capture -----------------------------------------------------------------


def test_capture_and_duplicate_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "capture",
        "--text",
        "Pricing gains a student tier.",
        "--type",
        "decision",
        "--today",
        "2026-03-01",
    )
    assert code == 0
    assert doc["schema_version"] == "megamind/capture-result/v1"
    assert doc["status"] == "captured"
    code, doc2, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "capture",
        "--text",
        "Pricing gains a student tier.",
        "--type",
        "decision",
        "--today",
        "2026-03-01",
    )
    assert code == 0
    assert doc2["status"] == "duplicate"
    assert doc2["proposal_id"] == doc["proposal_id"]


def test_capture_creates_a_proposal_at_a_canonical_wiki_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")

    code, doc, err = run_json(
        capsys,
        "--root",
        str(root),
        "capture",
        "--text",
        "Synthetic lighting uses a neutral reference.",
        "--type",
        "guidance",
        "--today",
        "2026-03-01",
    )

    assert code == 0
    assert err == ""
    assert doc["schema_version"] == "megamind/capture-result/v1"
    assert doc["status"] == "captured"
    assert doc["path"].startswith(".megamind/proposals/")


def test_capture_empty_is_typed_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(capsys, "--root", str(vault), "capture", "--text", "   ")
    assert code == 1
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "capture_invalid"
    assert doc["help"]


def test_capture_from_stdin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import io

    vault = build_vault(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("A release note from stdin."))
    code, doc, _ = run_json(capsys, "--root", str(vault), "capture", "--today", "2026-03-01")
    assert code == 0
    assert doc["status"] == "captured"
    assert "source: stdin" in (vault / doc["path"]).read_text(encoding="utf-8")


def test_review_lists_proposals_at_a_canonical_wiki_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    proposal_id = _capture_id(capsys, root, "Synthetic lighting uses a neutral reference.")

    code, doc, err = run_json(capsys, "--root", str(root), "review")

    assert code == 0
    assert err == ""
    assert doc["schema_version"] == "megamind/review-report/v1"
    assert any(proposal_id in item for item in doc["open_proposals"])


def test_review_renders_and_caps_research_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    research_root = vault / ".megamind" / "research"
    packets = research_root / "packets"
    evidence = research_root / "evidence"
    packets.mkdir(parents=True)
    evidence.mkdir()
    for index in range(21):
        (packets / f"packet-{index:02}.json").write_text("{}\n", encoding="utf-8")
        (evidence / f"evidence-{index:02}.json").write_text(
            '{"decision":"deferred","correction_status":"expression_of_concern"}\n',
            encoding="utf-8",
        )

    code, doc, err = run_json(capsys, "--root", str(vault), "review")

    assert code == 0
    assert err == ""
    assert doc["status"] == "attention"
    assert doc["aggregates"]["research_packets"] == 21
    assert doc["aggregates"]["pending_source_rights"] == 21
    assert doc["aggregates"]["contradictions"] == 21
    assert len(doc["research_packets"]) == 20
    assert len(doc["pending_source_rights"]) == 20
    assert len(doc["contradictions"]) == 20
    assert set(doc["notes"]) == {
        "research_packets truncated to 20 of 21; re-run with --full",
        "pending_source_rights truncated to 20 of 21; re-run with --full",
        "contradictions truncated to 20 of 21; re-run with --full",
    }


def test_review_at_a_canonical_wiki_root_reports_only_compiled_pages(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    (root / "raw/2024-lighting.md").write_text(
        "---\ntitle: Synthetic lighting\nupdated: 2019-01-01\n---\n\n"
        "# Synthetic lighting\n\nSource note. [gone](nowhere.md)\n",
        encoding="utf-8",
    )
    (root / "wiki/synthetic-lighting.md").write_text(
        "---\ntitle: Synthetic lighting\n---\n\n# Synthetic lighting\n", encoding="utf-8"
    )
    _capture_id(capsys, root, "Synthetic lighting uses a neutral reference.")

    code, doc, err = run_json(capsys, "--root", str(root), "review", "--today", "2026-03-01")

    assert code == 0
    assert err == ""
    assert "raw/" not in json.dumps(doc)
    assert "AGENTS.md" not in json.dumps(doc)
    assert doc["aggregates"]["duplicate_titles"] == 0
    assert doc["aggregates"]["stale_pages"] == 0
    assert doc["aggregates"]["dead_links"] == 0


# --- evolve ------------------------------------------------------------------


@pytest.mark.parametrize(
    "destination", ["raw/forbidden.md", "./raw/forbidden.md", "wiki/../raw/forbidden.md"]
)
def test_evolve_refuses_the_immutable_raw_layer_at_a_canonical_wiki_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], destination: str
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    proposal_id = _capture_id(capsys, root, "Synthetic lighting uses a neutral reference.")

    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--dest",
        destination,
    )

    assert code == 1
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "evolve_invalid"
    assert not (root / "raw/forbidden.md").exists()


@pytest.mark.parametrize(
    "destination", ["AGENTS.md", ".megamind/wiki-card.json", "notes/outside.md"]
)
def test_evolve_rejects_non_compiled_targets_at_a_canonical_wiki_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], destination: str
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    proposal_id = _capture_id(capsys, root, "Synthetic lighting uses a neutral reference.")

    code, doc, _ = run_json(
        capsys, "--root", str(root), "evolve", proposal_id, "--dest", destination
    )

    assert code == 1
    assert doc["code"] == "evolve_invalid"


def test_evolve_plans_a_compiled_page_at_a_canonical_wiki_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    proposal_id = _capture_id(capsys, root, "Synthetic lighting uses a neutral reference.")

    code, doc, err = run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--dest",
        "wiki/synthetic-lighting.md",
    )

    assert code == 0
    assert err == ""
    assert doc["schema_version"] == "megamind/evolve-plan/v1"
    assert doc["status"] == "planned"
    assert doc["destination"] == "wiki/synthetic-lighting.md"


def test_evolve_default_destination_is_compiled_at_a_canonical_wiki_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    proposal_id = _capture_id(capsys, root, "SoloWiki lighting uses a neutral reference.")

    code, planned, err = run_json(capsys, "--root", str(root), "evolve", proposal_id)

    assert code == 0
    assert err == ""
    assert planned["status"] == "planned"
    assert planned["destination"].startswith("wiki/topics/")
    assert planned["creates_new_wiki"] is False

    code, applied, _ = run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--apply",
        "--plan-id",
        planned["plan_id"],
        "--today",
        "2026-03-01",
    )

    assert code == 0
    assert applied["status"] == "applied"
    assert (root / planned["destination"]).is_file()


def test_evolve_rollback_restores_the_compiled_tree_and_retains_the_proposal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    proposal_id = _capture_id(capsys, root, "Synthetic lighting uses a neutral reference.")
    _, planned, _ = run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--dest",
        "wiki/synthetic-lighting.md",
    )
    code, applied, _ = run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--dest",
        "wiki/synthetic-lighting.md",
        "--apply",
        "--plan-id",
        planned["plan_id"],
        "--today",
        "2026-03-01",
    )
    assert code == 0
    assert applied["status"] == "applied"

    code, rolled_back, err = run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--rollback",
        "--plan-id",
        planned["plan_id"],
    )

    assert code == 0
    assert err == ""
    assert rolled_back["schema_version"] == "megamind/evolve-result/v1"
    assert rolled_back["status"] == "rolled_back"
    assert rolled_back["rolled_back"] == ["wiki/index.md", "wiki/synthetic-lighting.md"]
    assert applied["pre_change_tree_sha256"] == rolled_back["restored_tree_sha256"]
    assert applied["applied_tree_sha256"] != applied["pre_change_tree_sha256"]
    assert not (root / "wiki/synthetic-lighting.md").exists()
    assert (root / f".megamind/proposals/{proposal_id}.md").is_file()
    audit = (root / ".megamind/audit/log.jsonl").read_text(encoding="utf-8")
    assert '"action": "evolve-rollback"' in audit


def test_evolve_result_renders_one_stable_key_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    proposal_id = _capture_id(capsys, vault, "Pricing model gains an annual discount tier.")
    _, planned, _ = run_json(capsys, "--root", str(vault), "evolve", proposal_id)
    _, applied, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "evolve",
        proposal_id,
        "--apply",
        "--plan-id",
        planned["plan_id"],
        "--today",
        "2026-03-01",
    )
    _, resumed, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "evolve",
        proposal_id,
        "--apply",
        "--plan-id",
        planned["plan_id"],
    )
    _, replanned, _ = run_json(capsys, "--root", str(vault), "evolve", proposal_id)
    _, noop, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "evolve",
        proposal_id,
        "--apply",
        "--plan-id",
        replanned["plan_id"],
    )
    _, rolled_back, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "evolve",
        proposal_id,
        "--rollback",
        "--plan-id",
        planned["plan_id"],
    )

    for doc in (applied, resumed, noop, rolled_back):
        assert doc["schema_version"] == "megamind/evolve-result/v1"
        assert list(doc) == list(applied)
    assert applied["rolled_back"] == []
    assert applied["restored_tree_sha256"] == ""
    assert resumed["status"] == "noop"
    assert noop["status"] == "noop"
    assert noop["pre_change_tree_sha256"] == ""
    assert noop["applied_tree_sha256"] == ""
    assert rolled_back["applied"] == []
    assert rolled_back["applied_tree_sha256"] == applied["applied_tree_sha256"]


def test_evolve_rollback_refuses_foreign_content_without_deleting_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    proposal_id = _capture_id(capsys, root, "Synthetic lighting uses a neutral reference.")
    _, planned, _ = run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--dest",
        "wiki/synthetic-lighting.md",
    )
    run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--dest",
        "wiki/synthetic-lighting.md",
        "--apply",
        "--plan-id",
        planned["plan_id"],
        "--today",
        "2026-03-01",
    )
    target = root / "wiki/synthetic-lighting.md"
    target.write_text("foreign content\n", encoding="utf-8")

    code, refused, _ = run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--rollback",
        "--plan-id",
        planned["plan_id"],
    )

    assert code == 1
    assert refused["schema_version"] == "megamind/error/v1"
    assert refused["code"] == "evolve_invalid"
    assert target.read_text(encoding="utf-8") == "foreign content\n"


def test_evolve_apply_resumes_an_interrupted_canonical_transaction(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    proposal_id = _capture_id(capsys, root, "Synthetic lighting uses a neutral reference.")
    _, planned, _ = run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--dest",
        "wiki/synthetic-lighting.md",
    )
    import megamind.evolve as evolve_module

    real_write = evolve_module.atomic_write

    def interrupt(root_path: Path, target: str | Path, content: str, **kwargs: object) -> Path:
        if str(target) == f".megamind/proposals/{proposal_id}.md":
            raise KeyboardInterrupt("synthetic interruption")
        return real_write(root_path, target, content, **kwargs)  # type: ignore[arg-type]

    with monkeypatch.context() as patched:
        patched.setattr(evolve_module, "atomic_write", interrupt)
        with pytest.raises(KeyboardInterrupt, match="synthetic interruption"):
            main(
                [
                    "--format",
                    "json",
                    "--root",
                    str(root),
                    "evolve",
                    proposal_id,
                    "--dest",
                    "wiki/synthetic-lighting.md",
                    "--apply",
                    "--plan-id",
                    planned["plan_id"],
                    "--today",
                    "2026-03-01",
                ]
            )
    capsys.readouterr()

    code, recovered, err = run_json(
        capsys,
        "--root",
        str(root),
        "evolve",
        proposal_id,
        "--apply",
        "--plan-id",
        planned["plan_id"],
    )

    assert code == 0
    assert err == ""
    assert recovered["status"] == "applied"
    assert recovered["notes"] == ["recovered from the durable evolution transaction"]
    assert (root / "wiki/synthetic-lighting.md").is_file()


def _capture_id(capsys: pytest.CaptureFixture[str], vault: Path, text: str) -> str:
    _, doc, _ = run_json(
        capsys, "--root", str(vault), "capture", "--text", text, "--today", "2026-03-01"
    )
    return str(doc["proposal_id"])


def test_evolve_plan_apply_and_noop(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    pid = _capture_id(capsys, vault, "Pricing model gains an annual discount tier.")
    code, planned, _ = run_json(capsys, "--root", str(vault), "evolve", pid)
    assert code == 0
    assert planned["schema_version"] == "megamind/evolve-plan/v1"
    assert planned["status"] == "planned"
    assert planned["diff_truncated"] is False
    assert any("--apply --plan-id " + planned["plan_id"] in h for h in planned["help"])

    code, applied, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "evolve",
        pid,
        "--apply",
        "--plan-id",
        planned["plan_id"],
        "--today",
        "2026-03-01",
    )
    assert code == 0
    assert applied["schema_version"] == "megamind/evolve-result/v1"
    assert applied["status"] == "applied"
    assert applied["applied"] == ["ProductWiki/topics/pricing-model.md"]

    code, replan, _ = run_json(capsys, "--root", str(vault), "evolve", pid)
    assert code == 0
    assert replan["status"] == "noop"


def test_existing_wiki_evolution_updates_index_routes_and_rolls_back_exactly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    index = vault / "ProductWiki/INDEX.md"
    card = vault / "ProductWiki/CARD.md"
    registry = vault / ".megamind/registry.json"
    index_before = index.read_bytes()
    card_before = card.read_bytes()
    registry_before = registry.read_bytes()
    proposal_id = _capture_id(capsys, vault, "# Pricing tiers\n\nThree synthetic tiers apply.")
    destination = "ProductWiki/topics/pricing-tiers.md"

    code, planned, err = run_json(
        capsys,
        "--root",
        str(vault),
        "evolve",
        proposal_id,
        "--dest",
        destination,
    )
    assert code == 0 and err == ""
    assert planned["files_changed"] == 2
    assert any("ProductWiki/INDEX.md" in line for line in planned["diff"])

    code, applied, err = run_json(
        capsys,
        "--root",
        str(vault),
        "evolve",
        proposal_id,
        "--dest",
        destination,
        "--apply",
        "--plan-id",
        planned["plan_id"],
        "--today",
        "2026-03-01",
    )
    assert code == 0 and err == ""
    assert applied["applied"] == [destination, "ProductWiki/INDEX.md"]
    assert card.read_bytes() == card_before
    assert registry.read_bytes() == registry_before

    code, preflight, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "preflight",
        "pricing tiers",
        "--model-class",
        "local",
    )
    assert code == 0 and preflight["status"] == "matched"
    code, routed, _ = run_json(capsys, "--root", str(vault), "route", "pricing tiers")
    assert code == 0 and routed["decision"] == "load"
    assert routed["candidates"][0]["path"] == destination

    code, rolled_back, err = run_json(
        capsys,
        "--root",
        str(vault),
        "evolve",
        proposal_id,
        "--rollback",
        "--plan-id",
        planned["plan_id"],
    )
    assert code == 0 and err == ""
    assert rolled_back["rolled_back"] == ["ProductWiki/INDEX.md", destination]
    assert index.read_bytes() == index_before
    assert not (vault / destination).exists()
    assert card.read_bytes() == card_before
    assert registry.read_bytes() == registry_before


def test_evolve_apply_without_plan_id_is_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(capsys, "--root", str(vault), "evolve", "whatever", "--apply")
    assert code == 2
    assert doc["code"] == "usage_error"


def test_evolve_plan_mismatch_is_typed_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    pid = _capture_id(capsys, vault, "Pricing model gains an annual discount tier.")
    code, doc, _ = run_json(
        capsys, "--root", str(vault), "evolve", pid, "--apply", "--plan-id", "wrong"
    )
    assert code == 1
    assert doc["code"] == "plan_mismatch"
    assert doc["help"]


def test_evolve_new_wiki_needs_approval(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    pid = _capture_id(capsys, vault, "# Fleet notes\n\nSynthetic fleet knowledge.")
    code, planned, _ = run_json(
        capsys, "--root", str(vault), "evolve", pid, "--dest", "FleetWiki/topics/fleet.md"
    )
    assert planned["creates_new_wiki"] is True
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "evolve",
        pid,
        "--dest",
        "FleetWiki/topics/fleet.md",
        "--apply",
        "--plan-id",
        planned["plan_id"],
    )
    assert code == 1
    assert doc["code"] == "approval_required"


def test_evolve_diff_truncation_and_full(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    long_text = "# Long note\n\n" + "\n".join(f"Line {i} of synthetic content." for i in range(120))
    pid = _capture_id(capsys, vault, long_text)
    code, doc, _ = run_json(capsys, "--root", str(vault), "evolve", pid, "--dest", "ProductWiki")
    assert code == 0
    assert doc["diff_truncated"] is True
    assert len(doc["diff"]) == DIFF_LINE_LIMIT
    assert doc["diff_lines_total"] > DIFF_LINE_LIMIT
    assert any("--full" in entry for entry in doc["help"])
    _, full_doc, _ = run_json(
        capsys, "--root", str(vault), "evolve", pid, "--dest", "ProductWiki", "--full"
    )
    assert full_doc["diff_truncated"] is False
    assert len(full_doc["diff"]) == full_doc["diff_lines_total"]


# --- review ------------------------------------------------------------------


def test_review_clean_is_definitive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(capsys, "--root", str(vault), "review", "--today", "2026-03-01")
    assert code == 0
    assert doc["schema_version"] == "megamind/review-report/v1"
    assert doc["status"] == "clean"
    assert all(count == 0 for count in doc["aggregates"].values())
    assert "open_proposals" not in doc  # empty sections stay out; aggregates are definitive


def test_review_attention_lists_sections(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    _capture_id(capsys, vault, "Unfiled zeppelin maintenance note.")
    code, doc, _ = run_json(capsys, "--root", str(vault), "review", "--today", "2026-03-01")
    assert code == 0
    assert doc["status"] == "attention"
    assert doc["aggregates"]["uncategorized_proposals"] == 1
    assert len(doc["uncategorized_proposals"]) == 1
    assert any("evolve" in entry for entry in doc["help"])


# --- doctor ------------------------------------------------------------------


def test_doctor_healthy_exit_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(capsys, "--root", str(vault), "doctor")
    assert code == 0
    assert doc["status"] == "healthy"
    assert doc["findings"] == []


def test_doctor_errors_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    (vault / "ROUTER.md").write_text("hand edited, no header\n", encoding="utf-8")
    code, doc, _ = run_json(capsys, "--root", str(vault), "doctor")
    assert code == 1
    assert doc["status"] == "errors"
    assert doc["errors"] >= 1
    assert set(doc["findings"][0]) == {"check", "severity", "path", "message"}


# --- config / setup ----------------------------------------------------------


def test_config_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(capsys, "--root", str(vault), "config", "show")
    assert code == 0
    assert doc["schema_version"] == "megamind/config/v1"
    assert doc["registry"] == ".megamind/registry.json"
    assert doc["budgets"]["max_candidates"] == 5
    assert {w["name"] for w in doc["wikis"]} >= {"ProductWiki", "BrandingWiki"}


def test_setup_skill_plan_and_install(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, plan_doc, _ = run_json(capsys, "setup", "skill")
    assert code == 0
    assert plan_doc["schema_version"] == "megamind/setup-plan/v1"
    assert plan_doc["status"] == "plan"
    # A vault root is a plausible working directory, so no suggestion may be a bare
    # project-relative path that would resolve inside one and be refused.
    assert not any("--dest .claude" in entry for entry in plan_doc["help"])
    assert any("$HOME/.claude/skills" in entry for entry in plan_doc["help"])
    dest = tmp_path / "skills"
    code, doc, _ = run_json(capsys, "setup", "skill", "--dest", str(dest))
    assert code == 0
    assert doc["status"] == "installed"
    assert (dest / "megamind" / "SKILL.md").is_file()
    code, doc, _ = run_json(capsys, "setup", "skill", "--dest", str(dest))
    assert code == 0
    assert doc["status"] == "already_installed"
    assert doc["created"] == []


# --- errors and exits ---------------------------------------------------------


def test_uninitialized_root_is_typed_error_exit_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(capsys, "--root", str(tmp_path / "nowhere"), "route", "anything")
    assert code == 2
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "not_initialized"
    assert any("init" in entry for entry in doc["help"])


def test_unknown_flag_is_rejected_with_error_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["doctor", "--explode"])
    captured = capsys.readouterr()
    assert code == 2
    assert "usage_error" in captured.out
    assert "schema_version: megamind/error/v1" in captured.out


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.startswith("megamind-axi ")


def test_malformed_index_is_a_typed_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    (vault / "ProductWiki/INDEX.md").write_text(
        "---\nmegamind: index\nbroken line without a colon\n---\n\n# Index\n", encoding="utf-8"
    )
    code, doc, err = run_json(capsys, "--root", str(vault), "route", "pricing", "model")
    assert code == 1
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "frontmatter_invalid"
    assert doc["help"]
    assert err == ""


def test_capture_from_a_directory_is_a_typed_io_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    code, doc, err = run_json(capsys, "--root", str(vault), "capture", "--file", str(vault))
    assert code == 1
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "io_error"
    assert err == ""


def test_unicode_digit_frontmatter_never_tracebacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    (vault / "ProductWiki/topics/pricing-model.md").write_text(
        "---\ntitle: Pricing\ntype: fact\nstatus: active\nqty: \u00b2\n---\n\n# Pricing\n",
        encoding="utf-8",
    )
    code, doc, err = run_json(capsys, "--root", str(vault), "route", "pricing", "model")
    assert code == 0
    assert doc["schema_version"] == "megamind/route-result/v2"
    assert err == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"version": "one"},
        {"version": 1, "budgets": {"max_candidates": "five"}},
        {"version": 1, "wikis": [{"name": "W", "path": 7}]},
        {"version": 1, "wikis": "nope"},
    ],
)
def test_mistyped_registry_is_a_typed_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: dict[str, Any]
) -> None:
    vault = build_vault(tmp_path)
    (vault / ".megamind/registry.json").write_text(json.dumps(payload), encoding="utf-8")
    code, doc, err = run_json(capsys, "--root", str(vault), "config", "show")
    assert code == 2
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "registry_invalid"
    assert str(vault) not in doc["message"]
    assert err == ""


def test_mistyped_registry_is_a_doctor_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    (vault / ".megamind/registry.json").write_text('{"version": "one"}', encoding="utf-8")
    code, doc, err = run_json(capsys, "--root", str(vault), "doctor")
    assert code == 1
    assert doc["schema_version"] == "megamind/doctor-report/v1"
    assert doc["errors"] >= 1
    assert err == ""


# --- execution outside the source tree ---------------------------------------


def test_module_execution_outside_source_tree(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "megamind.cli", "--format", "json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    doc = json.loads(result.stdout)
    assert doc["schema_version"] == "megamind/home/v1"
    assert doc["initialized"] is False
    assert result.stderr == ""


# --- migrate -------------------------------------------------------------------


def _write_v1_registry(root: Path) -> None:
    directory = root / ".megamind"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "wikis": [{"name": "OldWiki", "path": "OldWiki", "privacy": "public-reference"}],
    }
    (directory / "registry.json").write_text(json.dumps(payload), encoding="utf-8")
    (root / "OldWiki").mkdir(exist_ok=True)


def test_migrate_upgrades_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_v1_registry(vault)
    code, doc, _ = run_json(capsys, "--root", str(vault), "migrate")
    assert code == 0
    assert doc["schema_version"] == "megamind/migrate-result/v1"
    assert doc["status"] == "migrated"
    assert doc["version"] == 2
    saved = json.loads((vault / ".megamind/registry.json").read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert saved["wikis"][0]["model_access"] == {"cloud": "full", "local": "full"}
    code, doc, _ = run_json(capsys, "--root", str(vault), "migrate")
    assert doc["status"] == "already_current"


def test_migrate_requires_an_initialized_vault(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(capsys, "--root", str(tmp_path), "migrate")
    assert code == 2
    assert doc["code"] == "not_initialized"


# --- init --wiki ----------------------------------------------------------------


def test_init_wiki_scaffolds_canonical_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "SoloWiki"
    code, doc, _ = run_json(capsys, "init", str(target), "--wiki", "SoloWiki")
    assert code == 0
    assert doc["layout"] == "canonical-wiki"
    created = set(doc["created"])
    assert "AGENTS.md" in created
    assert "raw/" in created
    assert "wiki/index.md" in created
    assert "wiki/log.md" in created
    assert ".megamind/wiki-card.json" in created
    assert ".megamind/gaps.jsonl" in created
    card = json.loads((target / ".megamind/wiki-card.json").read_text(encoding="utf-8"))
    assert card["schema"] == "megamind/wiki-card/v2"
    assert card["name"] == "SoloWiki"
    assert card["privacy"] == ""  # unclassified: restrictive cloud default
    assert card["index"] == "wiki/index.md"

    code, doc, _ = run_json(capsys, "init", str(target), "--wiki", "SoloWiki")
    assert doc["status"] == "already_initialized"


def test_init_wiki_on_a_registry_vault_is_a_typed_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(capsys, "init", str(vault), "--wiki", "SoloWiki")
    assert code == 1
    assert doc["code"] == "init_invalid"
    assert doc["help"]
    assert not (vault / ".megamind/wiki-card.json").exists()


# --- catalog -------------------------------------------------------------------


def test_catalog_single_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, err = run_json(capsys, "--root", str(vault), "catalog", "--today", "2026-08-10")
    assert code == 0
    assert err == ""
    assert doc["schema_version"] == "megamind/catalog/v1"
    assert doc["counts"]["ok"] == 5
    assert doc["total"] == 5
    names = {row["name"] for row in doc["wikis"]}
    assert {"ProductWiki", "ArchiveBox"} <= names
    assert doc["catalog_hash"]
    assert doc["help"]


def test_catalog_estate_and_toon_json_parity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    estate = tmp_path / "estate"
    estate.mkdir()
    build_vault(estate)
    code_j, doc, _ = run_json(capsys, "catalog", "--estate", str(estate), "--today", "2026-08-10")
    code_t, toon_out, _ = run_toon(
        capsys, "catalog", "--estate", str(estate), "--today", "2026-08-10"
    )
    assert code_j == code_t == 0
    assert toon.encode(doc) == toon_out


def test_catalog_projection_drift_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    projection = tmp_path / "CATALOG.md"
    code, doc, _ = run_json(
        capsys, "--root", str(vault), "catalog", "--check-projection", str(projection)
    )
    assert code == 1
    assert doc["projection_check"]["status"] == "missing"

    code, emitted, _ = run_json(capsys, "--root", str(vault), "catalog", "--emit-projection")
    assert code == 0
    projection.write_text(emitted["projection"], encoding="utf-8")
    code, doc, _ = run_json(
        capsys, "--root", str(vault), "catalog", "--check-projection", str(projection)
    )
    assert code == 0
    assert doc["projection_check"]["status"] == "current"

    projection.write_text(emitted["projection"] + "hand edit\n", encoding="utf-8")
    code, doc, _ = run_json(
        capsys, "--root", str(vault), "catalog", "--check-projection", str(projection)
    )
    assert code == 1
    assert doc["projection_check"]["status"] == "drifted"


# --- preflight ------------------------------------------------------------------


def test_preflight_matched_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, err = run_json(
        capsys, "--root", str(vault), "preflight", "how does pricing work", "--model-class", "cloud"
    )
    assert code == 0
    assert err == ""
    assert doc["schema_version"] == "megamind/preflight-result/v2"
    assert doc["status"] == "matched"
    assert doc["confidence"] >= 0.75
    assert doc["thresholds"]["reliance_floor"] == 0.75
    assert doc["semantic"]["status"] == "disabled"
    assert doc["matches"][0]["confidence"]["meets_floor"] is True
    assert doc["matches"][0]["evidence"]["lexical_classes"]
    assert "lexical" not in doc["matches"][0]["evidence"]
    assert doc["status"] == "matched"
    assert doc["preflight_id"]
    assert doc["matches"][0]["name"] == "ProductWiki"
    assert doc["matches"][0]["follow_up"]
    assert doc["help"]


def test_preflight_requires_model_class(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, out, _ = run_toon(capsys, "--root", str(vault), "preflight", "pricing")
    assert code == 2
    assert "usage_error" in out


def test_preflight_toon_json_parity(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    argv = ("--root", str(vault), "preflight", "pricing", "--model-class", "local")
    code_j, doc, _ = run_json(capsys, *argv)
    code_t, toon_out, _ = run_toon(capsys, *argv)
    assert code_j == code_t == 0
    assert toon.encode(doc) == toon_out


def test_preflight_no_match_is_structured_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(
        capsys, "--root", str(vault), "preflight", "quantum llama", "--model-class", "local"
    )
    assert code == 0
    assert doc["status"] == "no-match"
    assert doc["matches"] == []
    assert doc["help"]


# --- adopt ---------------------------------------------------------------------


def test_adopt_dry_run_apply_and_rollback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "LegacyWiki"
    target.mkdir()
    (target / "README.md").write_text("# Legacy\n", encoding="utf-8")

    code, planned, _ = run_json(capsys, "adopt", str(target))
    assert code == 0
    assert planned["schema_version"] == "megamind/adopt-plan/v1"
    assert planned["status"] == "planned"
    assert ".megamind/wiki-card.json" in planned["files"]
    assert not (target / ".megamind").exists()  # dry run writes nothing

    code, doc, _ = run_json(capsys, "adopt", str(target), "--apply")
    assert code == 2
    assert doc["code"] == "usage_error"

    code, applied, _ = run_json(
        capsys, "adopt", str(target), "--apply", "--plan-id", planned["plan_id"]
    )
    assert code == 0
    assert applied["schema_version"] == "megamind/adopt-result/v1"
    assert applied["status"] == "applied"
    assert (target / ".megamind/wiki-card.json").is_file()
    assert (target / "README.md").read_text(encoding="utf-8") == "# Legacy\n"

    code, rolled, _ = run_json(capsys, "adopt", str(target), "--rollback")
    assert code == 0
    assert rolled["status"] == "rolled_back"
    assert not (target / ".megamind/wiki-card.json").exists()
    assert (target / "README.md").is_file()


def _adopted_legacy_root(capsys: pytest.CaptureFixture[str], target: Path) -> Path:
    """A wiki adopted around a legacy README hub: pages live at the root."""
    (target / "topics").mkdir(parents=True)
    (target / "README.md").write_text(
        "# Legacy wiki\n\n- [Pricing](topics/pricing.md)\n- [Old pricing](topics/old-pricing.md)\n",
        encoding="utf-8",
    )
    (target / "topics/pricing.md").write_text(
        "---\ntitle: Pricing\nupdated: 2019-01-01\n---\n\n# Pricing\n\n[gone](nowhere.md)\n",
        encoding="utf-8",
    )
    (target / "topics/old-pricing.md").write_text(
        "---\ntitle: Pricing\nstatus: shaky\n---\n\n# Pricing\n", encoding="utf-8"
    )
    _, planned, _ = run_json(capsys, "adopt", str(target))
    run_json(capsys, "adopt", str(target), "--apply", "--plan-id", planned["plan_id"])
    return target


def test_review_walks_the_declared_page_tree_of_an_adopted_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _adopted_legacy_root(capsys, tmp_path / "LegacyWiki")
    (target / "raw/source.md").write_text(
        "---\ntitle: Pricing\nupdated: 2019-01-01\n---\n\n# Pricing\n\n[gone](nowhere.md)\n",
        encoding="utf-8",
    )

    code, doc, err = run_json(capsys, "--root", str(target), "review", "--today", "2026-03-01")

    assert code == 0
    assert err == ""
    assert doc["status"] == "attention"
    assert doc["duplicate_titles"] == [
        {"title": "pricing", "pages": "topics/old-pricing.md; topics/pricing.md"}
    ]
    assert doc["dead_links"] == [{"page": "topics/pricing.md", "target": "nowhere.md"}]
    assert {item["page"] for item in doc["stale_pages"]} == {
        "topics/pricing.md",
        "topics/old-pricing.md",
    }
    assert "raw/" not in json.dumps(doc)
    assert "AGENTS.md" not in json.dumps(doc)


def test_evolve_targets_the_declared_page_tree_of_an_adopted_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _adopted_legacy_root(capsys, tmp_path / "LegacyWiki")
    proposal_id = _capture_id(capsys, target, "Pricing gains an annual tier.")

    code, planned, err = run_json(
        capsys, "--root", str(target), "evolve", proposal_id, "--dest", "topics/pricing.md"
    )

    assert code == 0
    assert err == ""
    assert planned["status"] == "planned"
    assert planned["action"] == "merge"
    assert planned["creates_new_wiki"] is False

    code, defaulted, _ = run_json(
        capsys, "--root", str(target), "evolve", proposal_id, "--dest", "LegacyWiki"
    )
    assert code == 0
    assert defaulted["destination"].startswith("topics/")

    for refused in ("raw/source.md", ".megamind/proposals/x.md", "AGENTS.md", ".trash/sneak.md"):
        code, doc, _ = run_json(
            capsys, "--root", str(target), "evolve", proposal_id, "--dest", refused
        )
        assert code == 1, refused
        assert doc["code"] == "evolve_invalid", refused
        assert "may write only compiled pages" in doc["message"], refused


def test_review_skips_hidden_trees_of_an_adopted_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An adopted Obsidian vault keeps deleted notes in `.trash/`; they are not pages."""
    target = _adopted_legacy_root(capsys, tmp_path / "LegacyWiki")
    (target / ".trash").mkdir()
    (target / ".trash/pricing.md").write_text(
        "---\ntitle: Pricing\nupdated: 2019-01-01\n---\n\n# Pricing\n\n[[Deleted note]]\n",
        encoding="utf-8",
    )
    (target / ".trash/deleted-note.md").write_text(
        "---\ntitle: Deleted note\n---\n\n# Deleted note\n", encoding="utf-8"
    )

    code, doc, err = run_json(capsys, "--root", str(target), "review", "--today", "2026-03-01")

    assert code == 0
    assert err == ""
    assert ".trash" not in json.dumps(doc)
    assert doc["duplicate_titles"] == [
        {"title": "pricing", "pages": "topics/old-pricing.md; topics/pricing.md"}
    ]
    assert doc["dead_links"] == [{"page": "topics/pricing.md", "target": "nowhere.md"}]


def test_adopt_apply_and_rollback_are_mutually_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "W"
    target.mkdir()
    code, doc, _ = run_json(capsys, "adopt", str(target), "--apply", "--rollback", "--plan-id", "x")
    assert code == 2
    assert doc["code"] == "usage_error"


def test_adopt_missing_directory_is_typed_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(capsys, "adopt", str(tmp_path / "ghost"))
    assert code == 1
    assert doc["code"] == "adopt_invalid"


def test_adopt_corrupt_rollback_record_is_a_typed_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A truncated adoption record must still produce one typed document, not a traceback."""
    target = tmp_path / "LegacyWiki"
    target.mkdir()
    (target / "README.md").write_text("# Legacy\n", encoding="utf-8")
    _, planned, _ = run_json(capsys, "adopt", str(target))
    run_json(capsys, "adopt", str(target), "--apply", "--plan-id", planned["plan_id"])

    record = next((target / ".megamind" / "audit").glob("adoption-*.json"))
    record.write_text('{"files": [{"path"', encoding="utf-8")
    code, doc, err = run_json(capsys, "adopt", str(target), "--rollback")
    assert code == 1
    assert err == ""
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "adopt_invalid"
    assert (target / ".megamind/wiki-card.json").is_file()


@pytest.mark.parametrize("through_symlink", [False, True])
def test_setup_skill_refuses_vault_destinations_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    through_symlink: bool,
) -> None:
    vault = build_vault(tmp_path)
    audit = vault / ".megamind/audit/log.jsonl"
    audit_before = audit.read_bytes()
    if through_symlink:
        link = tmp_path / "vault-link"
        link.symlink_to(vault, target_is_directory=True)
        destination = link / "skills"
    else:
        destination = vault / "skills"

    code, document, err = run_json(capsys, "setup", "skill", "--dest", str(destination))

    assert code == 1
    assert err == ""
    assert document["schema_version"] == "megamind/error/v1"
    assert document["code"] == "path_escape"
    assert "must not resolve inside a Megamind vault" in document["message"]
    assert any("--dest" in entry for entry in document["help"])
    assert not (vault / "skills/megamind").exists()
    assert audit.read_bytes() == audit_before


def test_new_commands_emit_schema_version_and_help(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    for argv in (
        ["--root", str(vault), "catalog"],
        ["--root", str(vault), "preflight", "pricing", "--model-class", "local"],
        ["--root", str(vault), "migrate"],
        ["adopt", str(tmp_path / "new-legacy")],
        ["bench"],
        ["experiment"],
    ):
        if "new-legacy" in argv[-1]:
            (tmp_path / "new-legacy").mkdir(exist_ok=True)
        _, doc, _ = run_json(capsys, *argv)
        assert doc["schema_version"].startswith("megamind/"), argv
        assert doc["help"], argv


# --- help[] entries are runnable commands ---------------------------------------


def test_help_entries_never_leave_a_command_unterminated(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every backticked help entry must close its backtick and parse as one command."""
    vault = build_vault(tmp_path)
    legacy = tmp_path / "Legacy Wiki"
    legacy.mkdir()
    (legacy / "README.md").write_text("# Legacy\n", encoding="utf-8")
    for argv in (
        ["--root", str(vault)],
        ["--root", str(vault), "catalog"],
        ["--root", str(vault), "catalog", "--emit-projection"],
        ["--root", str(vault), "preflight", "pricing", "--model-class", "local"],
        ["--root", str(vault), "route", "pricing"],
        ["--root", str(vault), "review", "--today", "2026-08-10"],
        ["--root", str(vault), "doctor"],
        ["adopt", str(legacy)],
        ["init", str(tmp_path / "New Vault")],
    ):
        _, doc, _ = run_json(capsys, *argv)
        for entry in doc["help"]:
            assert entry.count("`") % 2 == 0, (argv, entry)
            for command in entry.split("`")[1::2]:
                if command.startswith("megamind-axi "):
                    assert shlex.split(command)[0] == "megamind-axi", (argv, entry)


def _help_command(doc: dict[str, Any], marker: str) -> list[str]:
    """The argv an agent gets by running the help entry that carries `marker`."""
    entry = next(item for item in doc["help"] if marker in item)
    argv = shlex.split(entry.split("`")[1])
    assert argv[0] == "megamind-axi", entry
    return argv[1:]


def test_adopt_help_commands_run_verbatim_for_a_space_containing_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """help[] is the agent's exact next command, so an argv path must survive re-parsing."""
    target = tmp_path / "Legacy Wiki"
    target.mkdir()
    (target / "README.md").write_text("# Legacy\n", encoding="utf-8")

    _, planned, _ = run_json(capsys, "adopt", str(target))
    apply_argv = _help_command(planned, "--apply")
    assert str(target) in apply_argv  # the whole path is one argument
    code, applied, _ = run_json(capsys, *apply_argv)
    assert code == 0
    assert applied["status"] == "applied"

    rollback_argv = _help_command(planned, "--rollback")
    assert str(target) in rollback_argv
    code, rolled, _ = run_json(capsys, *rollback_argv)
    assert code == 0
    assert rolled["status"] == "rolled_back"
    assert (target / "README.md").is_file()


def test_init_help_commands_run_verbatim_for_a_space_containing_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "My Vault"
    _, doc, _ = run_json(capsys, "init", str(target))
    home_argv = _help_command(doc, "--root")
    assert str(target) in home_argv
    code, home, _ = run_json(capsys, *home_argv)
    assert code == 0
    assert home["initialized"] is True


def test_evolve_help_command_runs_verbatim_with_a_quoted_destination(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    pid = _capture_id(capsys, vault, "Pricing model gains an annual discount tier.")
    _, planned, _ = run_json(capsys, "--root", str(vault), "evolve", pid, "--dest", "ProductWiki")
    apply_argv = _help_command(planned, "--apply")
    assert "ProductWiki" in apply_argv
    code, applied, _ = run_json(capsys, "--root", str(vault), *apply_argv)
    assert code == 0
    assert applied["status"] == "applied"


def test_route_help_shell_quotes_the_query(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The query is untrusted: it must land in help[] as a single quoted argument."""
    vault = build_vault(tmp_path)
    query = 'pricing product" ; rm -rf ~ #'
    code, doc, _ = run_json(capsys, "--root", str(vault), "route", query)
    assert code == 0
    entry = next(item for item in doc["help"] if "megamind-axi route" in item)
    tokens = shlex.split(entry.split("`")[1])
    assert query in tokens
    assert ";" not in tokens
    assert "rm" not in tokens
