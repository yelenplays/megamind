from __future__ import annotations

from pathlib import Path

from megamind.skillpack import SKILL_FILE_NAMES, skill_files, write_skill

REPO_SKILL = Path(__file__).resolve().parent.parent / "skills" / "megamind"


def test_repo_skill_copy_matches_packaged_skill() -> None:
    """skills/megamind must never drift from the packaged source of truth."""
    for name, content in skill_files():
        repo_file = REPO_SKILL / name
        assert repo_file.is_file(), f"missing repo copy: {name}"
        assert repo_file.read_text(encoding="utf-8") == content, f"drifted: {name}"


def test_skill_mentions_axi_not_bare_cli() -> None:
    for _name, content in skill_files():
        assert "megamind-axi" in content


def test_write_skill_creates_and_never_overwrites(tmp_path: Path) -> None:
    created, skipped = write_skill(tmp_path)
    assert created == [f"megamind/{name}" for name in SKILL_FILE_NAMES]
    assert skipped == []
    marker = tmp_path / "megamind" / "SKILL.md"
    marker.write_text("user-edited\n", encoding="utf-8")
    created_again, skipped_again = write_skill(tmp_path)
    assert "megamind/SKILL.md" in skipped_again
    assert marker.read_text(encoding="utf-8") == "user-edited\n"
    assert "megamind/SKILL.md" not in created_again
