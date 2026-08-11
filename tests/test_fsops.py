from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from megamind import fsops
from megamind.fsops import (
    PathEscapeError,
    append_audit,
    atomic_write,
    backup_existing,
    content_hash,
    create_private_file,
    remove_empty_directory,
    resolve_contained,
    sync_directory,
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


def test_sync_directory_reports_whether_the_platform_flushed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX flushes the directory entry; a platform without a directory handle
    (Windows) says so instead of raising, and durable writes still work."""
    assert sync_directory(tmp_path) is (os.name != "nt")
    monkeypatch.setattr(fsops, "DIRECTORY_FSYNC", False)
    assert sync_directory(tmp_path) is False
    atomic_write(tmp_path, "a/page.md", "durable\n", durable=True)
    assert (tmp_path / "a/page.md").read_text(encoding="utf-8") == "durable\n"
    assert backup_existing(tmp_path, "a/page.md", durable=True) is not None


def test_remove_empty_directory_never_removes_a_directory_with_content(
    tmp_path: Path,
) -> None:
    (tmp_path / "kept").mkdir()
    (tmp_path / "kept/page.md").write_text("content\n", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    assert remove_empty_directory(tmp_path, "kept") is False
    assert (tmp_path / "kept/page.md").is_file()
    assert remove_empty_directory(tmp_path, "empty", durable=True) is True
    assert not (tmp_path / "empty").exists()
    assert remove_empty_directory(tmp_path, "empty") is False
    assert remove_empty_directory(tmp_path, "kept/page.md") is False
    with pytest.raises(PathEscapeError):
        remove_empty_directory(tmp_path, "../outside")


def test_create_private_file_is_owner_only_from_the_first_syscall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "nested" / "secret.key"
    calls: list[tuple[str, int, int]] = []
    real_open = os.open

    def probe(path: object, flags: int, mode: int = 0o777, **kwargs: object) -> int:
        calls.append((str(path), flags, mode))
        return real_open(path, flags, mode, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", probe)
    create_private_file(target, "s3cret\n", durable=True)
    monkeypatch.undo()
    creations = [call for call in calls if call[0] == str(target)]
    assert len(creations) == 1
    _path, flags, mode = creations[0]
    assert mode == 0o600
    assert flags & os.O_EXCL
    assert flags & os.O_CREAT
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text(encoding="utf-8") == "s3cret\n"


def test_create_private_file_refuses_to_replace_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "secret.key"
    create_private_file(target, "first\n")
    with pytest.raises(FileExistsError):
        create_private_file(target, "second\n")
    assert target.read_text(encoding="utf-8") == "first\n"


def test_create_private_file_leaves_nothing_behind_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "secret.key"

    def explode(handle: int, data: bytes) -> int:
        raise OSError("disk full")

    monkeypatch.setattr(os, "write", explode)
    with pytest.raises(OSError):
        create_private_file(target, "never lands\n")
    monkeypatch.undo()
    assert not target.exists()
