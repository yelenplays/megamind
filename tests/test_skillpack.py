from __future__ import annotations

import re
from pathlib import Path

import pytest

from megamind.fsops import PathEscapeError
from megamind.skillpack import SKILL_FILE_NAMES, skill_files, write_skill

REPO_ROOT = Path(__file__).resolve().parent.parent
REPO_SKILL = REPO_ROOT / "skills" / "megamind"
AXI_DOC = REPO_ROOT / "docs" / "axi.md"
SKILL_COMMANDS_DOC = REPO_ROOT / "src" / "megamind" / "skill" / "references" / "commands.md"
SOURCE_DIR = REPO_ROOT / "src" / "megamind"

# The packaged skill ships standalone to agents outside this repo, so it keeps its
# own copy of the error/exit contract instead of linking to docs/axi.md. These
# drift tests are what keeps that deliberate duplication honest.


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


@pytest.mark.parametrize("link_rel", ["megamind", "megamind/references"])
def test_write_skill_refuses_a_symlinked_destination_component(
    tmp_path: Path, link_rel: str
) -> None:
    """A symlink below dest must be refused too, before a single file is written."""
    vault = tmp_path / "vault"
    (vault / ".megamind").mkdir(parents=True)
    (vault / "inside").mkdir()
    dest = tmp_path / "dest"
    link = dest / link_rel
    link.parent.mkdir(parents=True)
    link.symlink_to(vault / "inside", target_is_directory=True)

    with pytest.raises(PathEscapeError, match="must not resolve inside a Megamind vault"):
        write_skill(dest)

    assert list((vault / "inside").iterdir()) == []
    assert not (dest / "megamind" / "SKILL.md").exists()


def test_command_reference_sections_are_unique_and_have_bodies() -> None:
    """A stray duplicate heading silently splits a command's documentation."""
    text = SKILL_COMMANDS_DOC.read_text(encoding="utf-8")
    headings = re.findall(r"^## (.*)$", text, re.M)
    duplicates = sorted({h for h in headings if headings.count(h) > 1})
    assert not duplicates, f"duplicate sections in commands.md: {duplicates}"
    for heading in headings:
        assert _section(SKILL_COMMANDS_DOC, f"## {heading}").strip(), (
            f"commands.md section `{heading}` has no body"
        )


def _section(doc: Path, heading: str) -> str:
    text = doc.read_text(encoding="utf-8")
    match = re.search(rf"^{re.escape(heading)}$(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"{doc.name} no longer has a `{heading}` section"
    return match.group(1)


def _documented_codes(doc: Path, heading: str) -> set[str]:
    """Backtick-quoted snake_case identifiers in an error-code section."""
    return set(re.findall(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)+)`", _section(doc, heading)))


def _raised_codes() -> set[str]:
    """Every error code the package can actually put on an error document."""
    codes = set()
    for module in sorted(SOURCE_DIR.glob("*.py")):
        codes.update(
            re.findall(r'^\s*code = "([a-z_]+)"', module.read_text(encoding="utf-8"), re.M)
        )
    cli = (SOURCE_DIR / "cli.py").read_text(encoding="utf-8")
    codes.update(re.findall(r'_error_doc\(\s*"([a-z_]+)"', cli))
    return codes


def test_error_codes_agree_across_docs_and_source() -> None:
    axi = _documented_codes(AXI_DOC, "## Error codes")
    skill = _documented_codes(SKILL_COMMANDS_DOC, "## Errors")
    raised = _raised_codes()
    fix = "update docs/axi.md and src/megamind/skill/references/commands.md together"
    assert axi == skill, f"error-code drift between the two docs: {axi ^ skill}; {fix}"
    assert raised <= axi, f"error codes raised but undocumented: {sorted(raised - axi)}; {fix}"
    assert axi <= raised, f"error codes documented but never raised: {sorted(axi - raised)}; {fix}"


EXIT_SEMANTICS = {
    "0": ("success", "no-op"),
    "1": ("operational failure", "doctor"),
    "2": ("usage", "config"),
}


def _documented_exits(doc: Path) -> dict[str, str]:
    text = " ".join(doc.read_text(encoding="utf-8").split())
    # Anchor on the clause that lists all three exits, not a passing "exit 0" mention.
    match = re.search(r"[Ee]xit(?: codes)?:?\s*0\b[^.;]*?\b1\b[^.;]*?\b2\b[^.;]*", text)
    assert match, f"{doc.name} no longer states the exit-code semantics"
    sentence = re.sub(r"^[Ee]xit(?: codes)?:?\s*", "", match.group(0))
    exits = {}
    for chunk in sentence.split(","):
        parts = re.match(r"\s*([0-9])\s+(.*)", chunk)
        assert parts, f"{doc.name} has an unparsable exit-code clause: {chunk!r}"
        exits[parts.group(1)] = parts.group(2).lower()
    return exits


def test_exit_code_semantics_agree_across_docs() -> None:
    fix = "update docs/axi.md and src/megamind/skill/references/commands.md together"
    for doc in (AXI_DOC, SKILL_COMMANDS_DOC):
        exits = _documented_exits(doc)
        assert set(exits) == set(EXIT_SEMANTICS), (
            f"{doc.name} documents exits {sorted(exits)}; {fix}"
        )
        for code, keywords in EXIT_SEMANTICS.items():
            for keyword in keywords:
                assert keyword in exits[code], f"{doc.name} exit {code} lost `{keyword}`; {fix}"
