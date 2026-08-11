"""Safe filesystem operations: path containment, atomic writes, audit records.

Every write Megamind performs goes through this module. Targets must stay inside
the configured root after resolving symlinks, writes are atomic, and mutations of
existing files leave a recoverable backup plus an audit record.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MEGAMIND_DIR = ".megamind"
AUDIT_LOG = "audit/log.jsonl"
BACKUP_DIR = "audit/backups"

AuditValue = str | int | bool | list[str] | None


class PathEscapeError(ValueError):
    """A path resolved outside the configured root (traversal or unsafe symlink)."""

    code = "path_escape"


def resolve_contained(root: Path, target: str | Path) -> Path:
    """Resolve target (relative to root, or absolute) and require containment in root.

    Symlinks are resolved first, so a symlink pointing outside the root is rejected
    even when its literal path looks contained.
    """
    root_resolved = root.resolve()
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = root_resolved / candidate
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise PathEscapeError(f"path escapes root: {target}")
    return resolved


def content_hash(text: str) -> str:
    """Stable short hash used for proposal ids, plan ids, and backup names."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def sync_directory(path: Path) -> None:
    """Flush a directory entry so a completed rename survives a power loss.

    ``os.fsync`` on the file alone only guarantees its bytes; the rename that
    publishes the name is a directory mutation and needs its own flush before a
    write-ahead record can be relied on after an abrupt process or power loss.
    """
    fd = os.open(path, getattr(os, "O_DIRECTORY", os.O_RDONLY))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_path(target: Path, content: str, *, durable: bool = False) -> Path:
    """Write content to an already-resolved absolute path via temp file plus rename.

    Low-level mechanism only: no containment check, backup, or audit. Callers that
    write into a vault must go through ``atomic_write`` (which resolves and contains
    the target first); this primitive exists for writes outside any vault root, such
    as installing the packaged skill into an arbitrary destination.

    ``durable`` additionally flushes the parent directory, so the file is present
    by name after a crash. Use it for write-ahead records another step depends on.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".megamind-tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        if durable:
            sync_directory(target.parent)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    return target


def atomic_write(root: Path, target: str | Path, content: str, *, durable: bool = False) -> Path:
    """Write content atomically to a root-contained path, creating parent dirs."""
    resolved = resolve_contained(root, target)
    return atomic_write_path(resolved, content, durable=durable)


def backup_existing(root: Path, target: str | Path, *, durable: bool = False) -> Path | None:
    """Copy an existing file into the audit backup area before mutating it.

    Returns the backup path, or None when the target does not exist yet.
    Backup names are derived from the original name plus a content hash, so
    re-applying an identical change is idempotent and never destroys history.
    """
    resolved = resolve_contained(root, target)
    if not resolved.is_file():
        return None
    text = resolved.read_text(encoding="utf-8")
    digest = content_hash(text)
    backup_rel = Path(MEGAMIND_DIR) / BACKUP_DIR / f"{resolved.name}.{digest}.bak"
    backup_path = resolve_contained(root, backup_rel)
    if not backup_path.exists():
        atomic_write(root, backup_rel, text, durable=durable)
    elif durable:
        sync_directory(backup_path.parent)
    return backup_path


def remove_contained(root: Path, target: str | Path) -> None:
    """Remove a contained file or tree, failing closed on path escapes."""
    resolved = resolve_contained(root, target)
    if resolved.is_dir() and not resolved.is_symlink():
        shutil.rmtree(resolved)
    else:
        with contextlib.suppress(FileNotFoundError):
            resolved.unlink()


def append_audit(
    root: Path,
    action: str,
    details: dict[str, AuditValue],
    now: datetime | None = None,
) -> Path:
    """Append a JSON line to the audit log. Never raises on missing directories."""
    timestamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    record: dict[str, AuditValue] = {"ts": timestamp, "action": action}
    record.update(details)
    log_path = resolve_contained(root, Path(MEGAMIND_DIR) / AUDIT_LOG)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
    return log_path
