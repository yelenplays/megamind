from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from megamind.fsops import (
    PathEscapeError,
    append_audit,
    atomic_write,
    backup_existing,
    content_hash,
    resolve_contained,
)


def test_contained_paths_resolve(tmp_path: Path) -> None:
    resolved = resolve_contained(tmp_path, "wiki/page.md")
    assert resolved == tmp_path.resolve() / "wiki" / "page.md"
    assert resolve_contained(tmp_path, ".") == tmp_path.resolve()


def test_traversal_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_contained(tmp_path, "../outside.md")
    with pytest.raises(PathEscapeError):
        resolve_contained(tmp_path, "wiki/../../outside.md")
    with pytest.raises(PathEscapeError):
        resolve_contained(tmp_path, "/etc/hosts")


def test_symlink_escape_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "sneaky").symlink_to(outside)
    with pytest.raises(PathEscapeError):
        resolve_contained(root, "sneaky/page.md")


def test_internal_symlink_allowed(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real")
    resolved = resolve_contained(tmp_path, "alias/page.md")
    assert resolved == tmp_path.resolve() / "real" / "page.md"


def test_atomic_write_creates_parents_and_leaves_no_temp(tmp_path: Path) -> None:
    target = atomic_write(tmp_path, "a/b/c.md", "hello\n")
    assert target.read_text(encoding="utf-8") == "hello\n"
    leftovers = [p for p in (tmp_path / "a" / "b").iterdir() if p.name.startswith(".megamind-tmp")]
    assert leftovers == []


def test_atomic_write_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        atomic_write(tmp_path, "../evil.md", "nope")
    assert not (tmp_path.parent / "evil.md").exists()


def test_backup_existing_is_idempotent(tmp_path: Path) -> None:
    atomic_write(tmp_path, "page.md", "version one\n")
    first = backup_existing(tmp_path, "page.md")
    second = backup_existing(tmp_path, "page.md")
    assert first is not None and first.is_file()
    assert first == second
    assert first.read_text(encoding="utf-8") == "version one\n"
    assert backup_existing(tmp_path, "missing.md") is None


def test_append_audit_writes_jsonl(tmp_path: Path) -> None:
    log = append_audit(tmp_path, "test-action", {"path": "x.md"})
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[0])
    assert record["action"] == "test-action"
    assert record["path"] == "x.md"
    assert "ts" in record


def test_content_hash_stable() -> None:
    assert content_hash("abc") == content_hash("abc")
    assert content_hash("abc") != content_hash("abd")
    assert len(content_hash("abc")) == 12


def test_atomic_write_replaces_existing(tmp_path: Path) -> None:
    atomic_write(tmp_path, "page.md", "old\n")
    atomic_write(tmp_path, "page.md", "new\n")
    assert (tmp_path / "page.md").read_text(encoding="utf-8") == "new\n"
    assert os.listdir(tmp_path) == ["page.md"]
