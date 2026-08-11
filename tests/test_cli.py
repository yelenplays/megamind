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


# --- evolve ------------------------------------------------------------------


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
