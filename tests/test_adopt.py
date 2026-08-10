"""Adoption: dry-run plan, gated apply, rollback, and adversarial shapes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from megamind.adopt import (
    AdoptError,
    AdoptPlanMismatch,
    apply_adoption,
    plan_adoption,
    rollback_adoption,
)
from megamind.card import load_wiki_card
from megamind.fsops import PathEscapeError, content_hash
from megamind.scaffold import init_vault


def _legacy_wiki(base: Path) -> Path:
    """A pre-existing wiki directory with legacy shapes and no Megamind state."""
    target = base / "LegacyWiki"
    (target / "notes").mkdir(parents=True)
    (target / "README.md").write_text("# Legacy wiki\n\nSynthetic hub page.\n", encoding="utf-8")
    (target / "MEMORY.md").write_text("# Memory\n\nSynthetic surrogate digest.\n", encoding="utf-8")
    (target / "notes/deep.md").write_text("# Deep note\n", encoding="utf-8")
    return target


def test_plan_is_read_only_and_spots_legacy_shapes(tmp_path: Path) -> None:
    target = _legacy_wiki(tmp_path)
    before = {p.relative_to(target) for p in target.rglob("*")}
    computed = plan_adoption(target)
    assert computed.status == "planned"
    assert {p.relative_to(target) for p in target.rglob("*")} == before  # discovery is read-only
    paths = [change.path for change in computed.changes]
    assert ".megamind/wiki-card.json" in paths
    assert "wiki/index.md" not in paths  # README.md adopted as the existing hub
    assert any("README.md" in note for note in computed.notes)
    assert any("MEMORY.md" in note for note in computed.notes)
    assert "raw" in computed.directories


def test_apply_requires_the_plan_id(tmp_path: Path) -> None:
    target = _legacy_wiki(tmp_path)
    computed = plan_adoption(target)
    with pytest.raises(AdoptPlanMismatch):
        apply_adoption(computed, approved_plan_id="wrong")
    assert not (target / ".megamind").exists()


def test_apply_creates_only_new_files_and_card_points_at_surrogates(tmp_path: Path) -> None:
    target = _legacy_wiki(tmp_path)
    original_readme = (target / "README.md").read_text(encoding="utf-8")
    computed = plan_adoption(target)
    created = apply_adoption(computed, approved_plan_id=computed.plan_id)
    assert ".megamind/wiki-card.json" in created
    assert (target / "README.md").read_text(encoding="utf-8") == original_readme
    assert (target / "notes/deep.md").is_file()

    card = load_wiki_card(target)
    assert card.name == "LegacyWiki"
    assert card.index == "README.md"  # existing hub adopted, not replaced
    assert card.digest == "MEMORY.md"  # surrogate digest referenced
    policy_card = card
    assert policy_card.privacy == ""  # unclassified: restrictive cloud default

    # re-planning an adopted wiki is an explicit error, not a second adoption
    with pytest.raises(AdoptError, match="no-op"):
        plan_adoption(target)


def test_apply_without_existing_hub_creates_index_skeleton(tmp_path: Path) -> None:
    target = tmp_path / "PlainWiki"
    target.mkdir()
    (target / "page.md").write_text("# Page\n", encoding="utf-8")
    computed = plan_adoption(target)
    paths = [change.path for change in computed.changes]
    assert "wiki/index.md" in paths
    apply_adoption(computed, approved_plan_id=computed.plan_id)
    card = load_wiki_card(target)
    assert card.index == "wiki/index.md"


def test_existing_files_are_never_overwritten(tmp_path: Path) -> None:
    target = _legacy_wiki(tmp_path)
    (target / "AGENTS.md").write_text("# My own contract\n", encoding="utf-8")
    computed = plan_adoption(target)
    assert "AGENTS.md" not in [change.path for change in computed.changes]
    assert any("kept as is" in note for note in computed.notes)
    apply_adoption(computed, approved_plan_id=computed.plan_id)
    assert (target / "AGENTS.md").read_text(encoding="utf-8") == "# My own contract\n"


def test_rollback_removes_exactly_generated_material(tmp_path: Path) -> None:
    target = _legacy_wiki(tmp_path)
    computed = plan_adoption(target)
    created = apply_adoption(computed, approved_plan_id=computed.plan_id)
    assert created
    removed, _kept = rollback_adoption(target)
    assert ".megamind/wiki-card.json" in removed
    assert not (target / ".megamind/wiki-card.json").exists()
    assert not (target / "raw").exists()
    assert (target / "README.md").is_file()  # pre-existing content untouched
    assert (target / "notes/deep.md").is_file()
    assert (target / ".megamind/audit/log.jsonl").is_file()  # the record stays


def test_rollback_keeps_files_modified_since_adoption(tmp_path: Path) -> None:
    target = _legacy_wiki(tmp_path)
    computed = plan_adoption(target)
    apply_adoption(computed, approved_plan_id=computed.plan_id)
    agents = target / "AGENTS.md"
    agents.write_text("# Human edited after adoption\n", encoding="utf-8")
    removed, kept = rollback_adoption(target)
    assert agents.is_file()
    assert any("AGENTS.md" in entry for entry in kept)
    assert "AGENTS.md" not in removed


def test_rollback_keeps_an_unreadable_file_and_still_finishes(tmp_path: Path) -> None:
    """An unverifiable file is kept, never a reason to abandon the rollback midway."""
    target = _legacy_wiki(tmp_path)
    computed = plan_adoption(target)
    apply_adoption(computed, approved_plan_id=computed.plan_id)
    agents = target / "AGENTS.md"
    agents.write_bytes(b"\xff\xfe not valid utf-8")
    removed, kept = rollback_adoption(target)
    assert agents.is_file()
    assert any("AGENTS.md" in entry and "kept" in entry for entry in kept)
    assert ".megamind/wiki-card.json" in removed
    assert not (target / ".megamind/wiki-card.json").exists()
    assert not list((target / ".megamind" / "audit").glob("adoption-*.json"))


def _adopted(tmp_path: Path) -> tuple[Path, Path]:
    """An adopted legacy wiki plus the on-disk rollback record it produced."""
    target = _legacy_wiki(tmp_path)
    computed = plan_adoption(target)
    apply_adoption(computed, approved_plan_id=computed.plan_id)
    record = next((target / ".megamind" / "audit").glob("adoption-*.json"))
    return target, record


def test_rollback_refuses_a_record_naming_a_path_outside_the_target(tmp_path: Path) -> None:
    """A tampered record must never make rollback delete outside the adoption root."""
    target, record = _adopted(tmp_path)
    victim = tmp_path / "victim.md"
    victim.write_text("# Pre-existing file outside the wiki\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    record.write_text(
        json.dumps(
            {
                "plan_id": "tampered",
                "files": [
                    {
                        "path": "../victim.md",
                        "sha": content_hash(victim.read_text(encoding="utf-8")),
                    }
                ],
                "directories": ["../outside/"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(PathEscapeError, match="escapes root"):
        rollback_adoption(target)
    assert victim.is_file()
    assert outside.is_dir()
    assert (target / ".megamind/wiki-card.json").is_file()  # nothing was removed


def test_rollback_of_a_corrupt_record_is_a_typed_error(tmp_path: Path) -> None:
    """A truncated record fails as a typed adopt error, never a traceback."""
    target, record = _adopted(tmp_path)
    record.write_text('{"plan_id": "abc", "files": [{"path"', encoding="utf-8")
    with pytest.raises(AdoptError, match="not valid JSON"):
        rollback_adoption(target)
    assert (target / ".megamind/wiki-card.json").is_file()


def test_rollback_of_a_record_with_a_malformed_entry_is_a_typed_error(tmp_path: Path) -> None:
    target, record = _adopted(tmp_path)
    record.write_text(json.dumps({"files": [{"path": "AGENTS.md"}]}), encoding="utf-8")
    with pytest.raises(AdoptError, match="path and sha"):
        rollback_adoption(target)
    record.write_text(json.dumps({"files": [], "directories": [7]}), encoding="utf-8")
    with pytest.raises(AdoptError, match="malformed directory entry"):
        rollback_adoption(target)
    record.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(AdoptError, match="not a JSON object"):
        rollback_adoption(target)
    assert (target / ".megamind/wiki-card.json").is_file()


def test_rollback_without_adoption_is_a_typed_error(tmp_path: Path) -> None:
    target = _legacy_wiki(tmp_path)
    with pytest.raises(AdoptError, match="nothing to roll back"):
        rollback_adoption(target)


def test_adopting_a_registry_vault_is_refused(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    init_vault(vault)
    with pytest.raises(AdoptError, match="registry vault"):
        plan_adoption(vault)


def test_adopting_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(AdoptError, match="not a directory"):
        plan_adoption(tmp_path / "ghost")


def test_adoption_through_unsafe_symlink_is_contained(tmp_path: Path) -> None:
    """A symlinked component that escapes the target must fail closed."""
    target = _legacy_wiki(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / "wiki").symlink_to(outside)
    computed = plan_adoption(target)
    # wiki/ resolves outside the target: adoption must not create files there
    wiki_changes = [c for c in computed.changes if c.path.startswith("wiki/")]
    if wiki_changes:
        with pytest.raises(ValueError, match="escapes root"):
            apply_adoption(computed, approved_plan_id=computed.plan_id)
    assert not (outside / "log.md").exists()


def test_raw_layer_is_immutable_through_adoption_and_rollback(tmp_path: Path) -> None:
    """The human-curated raw/ layer must survive adopt and rollback byte-identical."""
    target = _legacy_wiki(tmp_path)
    raw_file = target / "raw" / "source.md"
    raw_file.parent.mkdir(parents=True)  # pre-existing raw/ layer
    raw_file.write_text("# Curated source\n\nHuman content.\n", encoding="utf-8")
    assets = target / "raw" / "assets"
    assets.mkdir()
    (assets / "logo.txt").write_text("synthetic asset\n", encoding="utf-8")

    computed = plan_adoption(target)
    assert "raw" not in computed.directories  # pre-existing raw/ is never recreated
    apply_adoption(computed, approved_plan_id=computed.plan_id)
    assert raw_file.read_text(encoding="utf-8") == "# Curated source\n\nHuman content.\n"
    removed, _kept = rollback_adoption(target)
    assert raw_file.is_file()
    assert (assets / "logo.txt").is_file()
    assert not any(entry.startswith("raw/") and "source" in entry for entry in removed)
