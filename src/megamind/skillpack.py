"""Packaged Agent Skill: single source for `setup skill` and the repo copy.

The authoritative skill files ship inside the package under ``megamind/skill``.
``megamind-axi setup skill --dest DIR`` copies them into ``DIR/megamind`` without
ever overwriting existing files. The repository's ``skills/megamind`` directory
is a checked-in copy of the same files; a test guards against drift.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from importlib import resources
from pathlib import Path

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
    created: list[str] = []
    skipped: list[str] = []
    target_root = dest / SKILL_DIR_NAME
    for name, content in skill_files():
        target = target_root / name
        rel = f"{SKILL_DIR_NAME}/{name}"
        if target.exists():
            skipped.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, content)
        created.append(rel)
    return created, skipped


def _atomic_write_text(target: Path, content: str) -> None:
    """Write content to target via a temp file plus rename, never a partial file."""
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".megamind-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
