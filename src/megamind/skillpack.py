"""Packaged Agent Skill: single source for `setup skill` and the repo copy.

The authoritative skill files ship inside the package under ``megamind/skill``.
``megamind-axi setup skill --dest DIR`` copies them into ``DIR/megamind`` without
ever overwriting existing files. The repository's ``skills/megamind`` directory
is a checked-in copy of the same files; a test guards against drift.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from . import fsops
from .fsops import MEGAMIND_DIR, PathEscapeError

SKILL_DIR_NAME = "megamind"
SKILL_FILE_NAMES = ("SKILL.md", "references/commands.md", "references/concepts.md")


def skill_files() -> list[tuple[str, str]]:
    """Return (relative name, content) for every packaged skill file."""
    base = resources.files("megamind").joinpath("skill")
    result: list[tuple[str, str]] = []
    for name in SKILL_FILE_NAMES:
        result.append((name, base.joinpath(name).read_text(encoding="utf-8")))
    return result


def write_skill(dest: Path) -> tuple[list[str], list[str]]:
    """Copy the skill into dest/megamind. Never overwrites; returns (created, skipped)."""
    resolved = dest.expanduser().resolve()
    if any((parent / MEGAMIND_DIR).is_dir() for parent in (resolved, *resolved.parents)):
        raise PathEscapeError("skill destination must not resolve inside a Megamind vault")
    created: list[str] = []
    skipped: list[str] = []
    target_root = resolved / SKILL_DIR_NAME
    for name, content in skill_files():
        target = target_root / name
        rel = f"{SKILL_DIR_NAME}/{name}"
        if target.exists():
            skipped.append(rel)
            continue
        fsops.atomic_write_path(target, content)
        created.append(rel)
    return created, skipped
