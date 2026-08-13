"""Typed authorization for the picker\'s Different existing wiki path."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from megamind import selection, toon
from megamind.registry import ContextBudget, ModelAccess, load_registry, save_registry
from test_cli import run_json, run_toon
from test_selection import _preflight


def _list_args(vault: Path, request: str = "knowledge base") -> tuple[str, ...]:
    return (
        "--root",
        str(vault),
        "select-existing",
        "--request",
        request,
        "--model-class",
        "local",
        "--owner-id",
        "captain",
        "--session-id",
        "session-1",
        "--today",
        "2026-08-10",
    )


def test_unoffered_eligible_wiki_can_be_chosen_with_distinct_basis(
    vault: Path, capsys: Any
) -> None:
    request = "knowledge base"
    registry = load_registry(vault)
    branding_card = registry.wiki_by_name("BrandingWiki")
    assert branding_card is not None
    branding_card.context_budget = ContextBudget(max_candidates=5, max_context_chars=8000)
    save_registry(vault, registry)
    original = _preflight(capsys, vault, request, model_class="local")
    assert [str(item["name"]) for item in original["offers"]] == ["ProductWiki"]

    code, listed, error = run_json(capsys, *_list_args(vault, request))
    assert code == 0 and error == ""
    names = [str(item["name"]) for item in listed["wikis"]]
    assert "BrandingWiki" in names
    assert "ProductWiki" in names
    assert listed["catalog_hash"]
    branding = next(item for item in listed["wikis"] if item["name"] == "BrandingWiki")

    code, selected, error = run_json(
        capsys,
        *_list_args(vault, request),
        "BrandingWiki",
        "--selection-id",
        listed["selection_id"],
    )
    assert code == 0 and error == ""
    assert selected["schema_version"] == "megamind/existing-selection-result/v1"
    assert selected["selection"]["basis"] == "selected-eligible-existing"
    assert selected["selection"]["threshold_matched"] is False
    assert selected["selected"]["threshold_matched"] is False
    assert selected["selected"]["access"] == branding["access"]
    assert selected["selected"]["context_budget"] == {
        "max_candidates": 5,
        "max_context_chars": 8000,
    }


def test_existing_selection_list_has_json_toon_parity(vault: Path, capsys: Any) -> None:
    code, document, error = run_json(capsys, *_list_args(vault))
    assert code == 0 and error == ""
    code, rendered, error = run_toon(capsys, *_list_args(vault))
    assert code == 0 and error == ""
    assert toon.encode(document) == rendered


def test_existing_selection_refuses_forged_names_and_replay(vault: Path, capsys: Any) -> None:
    code, listed, _ = run_json(capsys, *_list_args(vault))
    assert code == 0
    forged = run_json(
        capsys,
        *_list_args(vault),
        "ManufacturedWiki",
        "--selection-id",
        listed["selection_id"],
    )
    assert forged[0] == 1
    assert forged[1]["code"] == "selection_invalid"
    toon_forged = run_toon(
        capsys,
        *_list_args(vault),
        "ManufacturedWiki",
        "--selection-id",
        listed["selection_id"],
    )
    assert toon_forged[0] == 1
    assert toon_forged[1] == toon.encode(forged[1])

    code, selected, _ = run_json(
        capsys,
        *_list_args(vault),
        "BrandingWiki",
        "--selection-id",
        listed["selection_id"],
    )
    assert code == 0
    replay = run_json(
        capsys,
        *_list_args(vault),
        "BrandingWiki",
        "--selection-id",
        listed["selection_id"],
    )
    assert replay[0] == 1
    assert replay[1]["code"] == "selection_invalid"
    toon_replay = run_toon(
        capsys,
        *_list_args(vault),
        "BrandingWiki",
        "--selection-id",
        listed["selection_id"],
    )
    assert toon_replay[0] == 1
    assert toon_replay[1] == toon.encode(replay[1])
    assert selected["selected"]["allows"]


def test_existing_selection_excludes_unsafe_current_rows(vault: Path, capsys: Any) -> None:
    registry = load_registry(vault)
    branding = registry.wiki_by_name("BrandingWiki")
    product = registry.wiki_by_name("ProductWiki")
    assert branding is not None and product is not None
    branding.model_access = ModelAccess(local="none", cloud="none")
    product.provisional = True
    product.freshness.half_life_days = 1
    product.freshness.last_confirmed = "2026-08-01"
    save_registry(vault, registry)

    code, listed, error = run_json(capsys, *_list_args(vault))
    assert code == 0 and error == ""
    names = {str(item["name"]) for item in listed["wikis"]}
    assert "BrandingWiki" not in names
    assert "ProductWiki" not in names
    refused = run_json(
        capsys,
        *_list_args(vault),
        "BrandingWiki",
        "--selection-id",
        listed["selection_id"],
    )
    assert refused[0] == 1
    toon_refused = run_toon(
        capsys,
        *_list_args(vault),
        "BrandingWiki",
        "--selection-id",
        listed["selection_id"],
    )
    assert toon_refused[0] == 1
    assert toon_refused[1] == toon.encode(refused[1])


def test_existing_selection_binds_model_session_date_and_catalog(vault: Path, capsys: Any) -> None:
    code, listed, _ = run_json(capsys, *_list_args(vault))
    assert code == 0
    base = (
        "--root",
        str(vault),
        "select-existing",
        "BrandingWiki",
        "--request",
        "knowledge base",
        "--model-class",
        "local",
        "--owner-id",
        "captain",
        "--session-id",
        "session-2",
        "--today",
        "2026-08-10",
        "--selection-id",
        listed["selection_id"],
    )
    code, document, _ = run_json(capsys, *base)
    assert code == 1 and document["code"] == "selection_invalid"
    code, rendered, _ = run_toon(capsys, *base)
    assert code == 1 and rendered == toon.encode(document)

    code, listed2, _ = run_json(capsys, *_list_args(vault))
    assert code == 0 and listed2["selection_id"] == listed["selection_id"]
    registry = load_registry(vault)
    branding = registry.wiki_by_name("BrandingWiki")
    assert branding is not None
    branding.purpose = "Changed synthetic purpose."
    save_registry(vault, registry)
    code, document, _ = run_json(
        capsys,
        *_list_args(vault),
        "BrandingWiki",
        "--selection-id",
        listed2["selection_id"],
    )
    assert code == 1 and document["code"] == "selection_invalid"
    code, rendered, _ = run_toon(
        capsys,
        *_list_args(vault),
        "BrandingWiki",
        "--selection-id",
        listed2["selection_id"],
    )
    assert code == 1 and rendered == toon.encode(document)


def test_existing_selection_refuses_model_date_and_home_changes(
    vault: Path, capsys: Any, tmp_path: Path
) -> None:
    code, listed, _ = run_json(capsys, *_list_args(vault))
    assert code == 0
    common = (
        "--root",
        str(vault),
        "select-existing",
        "BrandingWiki",
        "--request",
        "knowledge base",
        "--owner-id",
        "captain",
        "--session-id",
        "session-1",
        "--selection-id",
        listed["selection_id"],
    )
    for context in (
        (*common, "--model-class", "cloud", "--today", "2026-08-10"),
        (*common, "--model-class", "local", "--today", "2026-08-11"),
    ):
        code, document, _ = run_json(capsys, *context)
        assert code == 1 and document["code"] == "selection_invalid"
        code, rendered, _ = run_toon(capsys, *context)
        assert code == 1 and rendered == toon.encode(document)

    other_vault = tmp_path / "other-vault"
    shutil.copytree(vault, other_vault)
    home_context = (
        "--root",
        str(other_vault),
        "select-existing",
        "BrandingWiki",
        "--request",
        "knowledge base",
        "--model-class",
        "local",
        "--owner-id",
        "captain",
        "--session-id",
        "session-1",
        "--today",
        "2026-08-10",
        "--selection-id",
        listed["selection_id"],
    )
    code, document, _ = run_json(capsys, *home_context)
    assert code == 1 and document["code"] == "selection_invalid"
    code, rendered, _ = run_toon(capsys, *home_context)
    assert code == 1 and rendered == toon.encode(document)

def test_existing_selection_recovers_pending_audit_events(
    vault: Path, capsys: Any, monkeypatch: Any
) -> None:
    original_append_audit = selection.append_audit

    def fail_audit(*args: Any, **kwargs: Any) -> Path:
        raise OSError("synthetic audit failure")

    monkeypatch.setattr(selection, "append_audit", fail_audit)
    code, document, _ = run_json(capsys, *_list_args(vault))
    assert code == 1 and document["code"] == "selection_invalid"

    monkeypatch.setattr(selection, "append_audit", original_append_audit)
    code, listed, _ = run_json(capsys, *_list_args(vault))
    assert code == 0
    assert "existing_selection_listed" in (
        vault / ".megamind" / "audit" / "log.jsonl"
    ).read_text(encoding="utf-8")

    monkeypatch.setattr(selection, "append_audit", fail_audit)
    code, document, _ = run_json(
        capsys,
        *_list_args(vault),
        "BrandingWiki",
        "--selection-id",
        listed["selection_id"],
    )
    assert code == 1 and document["code"] == "selection_invalid"

    monkeypatch.setattr(selection, "append_audit", original_append_audit)
    code, document, _ = run_json(
        capsys,
        *_list_args(vault),
        "BrandingWiki",
        "--selection-id",
        listed["selection_id"],
    )
    assert code == 1 and document["code"] == "selection_invalid"
    assert "existing_selection_consumed" in (
        vault / ".megamind" / "audit" / "log.jsonl"
    ).read_text(encoding="utf-8")
