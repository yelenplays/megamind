"""Adopt: non-destructive onboarding of an existing wiki directory.

Adoption follows the same proposal-first contract as evolve: discovery is
read-only (a dirty repository is fine), the dry run prints every file that
would be created plus a plan id, and only ``--apply --plan-id <id>`` writes.
Adoption only ever *adds* the canonical sidecar state (``.megamind/``) and
missing scaffold files. It never moves, renames, rewrites, or normalizes
existing source pages: legacy index/hub pages and surrogate digests are
referenced by the card, not replaced.

Rollback removes exactly the files and directories an adoption apply created,
and only while their content is unchanged since creation; anything a human
touched since is left alone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .card import CARD_PATH, serialize_wiki_card
from .evolve import FileChange
from .fsops import MEGAMIND_DIR, append_audit, atomic_write, content_hash, resolve_contained
from .registry import WikiEntry, registry_file
from .scaffold import canonical_agents_md, canonical_index_md, canonical_log_md

# Deterministic candidate lists for legacy shapes, most canonical first.
INDEX_CANDIDATES = (
    "wiki/index.md",
    "INDEX.md",
    "index.md",
    "Home.md",
    "home.md",
    "README.md",
    "_index.md",
)
DIGEST_CANDIDATES = (
    "DIGEST.md",
    "digest.md",
    "OVERVIEW.md",
    "overview.md",
    "MEMORY.md",
    "SUMMARY.md",
)

AUDIT_LOG_REL = Path(MEGAMIND_DIR) / "audit" / "log.jsonl"


class AdoptError(ValueError):
    """The adoption request is invalid or cannot be applied safely."""

    code = "adopt_invalid"


class AdoptPlanMismatch(AdoptError):
    """The approved plan id no longer matches the computed adoption plan."""

    code = "plan_mismatch"


@dataclass
class AdoptionPlan:
    plan_id: str
    target: str
    wiki_name: str
    status: str  # "planned" | "noop"
    changes: list[FileChange] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def render_diff(self) -> str:
        return "\n".join(change.diff() for change in self.changes if change.diff())


def _detect_existing(target: Path, candidates: tuple[str, ...]) -> str | None:
    for rel in candidates:
        if (target / rel).is_file():
            return rel
    return None


def plan_adoption(target: Path, name: str | None = None) -> AdoptionPlan:
    """Compute the adoption plan for an existing wiki directory. Read-only."""
    if not target.is_dir():
        raise AdoptError(f"adoption target is not a directory: {target}")
    if registry_file(target).is_file():
        raise AdoptError(
            "target already has a Megamind registry; it is a registry vault, "
            "not a canonical wiki root"
        )
    if (target / CARD_PATH).is_file():
        raise AdoptError("target already has a wiki card; adoption is a no-op")

    wiki_name = name or target.name
    notes: list[str] = []
    changes: list[FileChange] = []
    directories: list[str] = []

    index = _detect_existing(target, INDEX_CANDIDATES)
    digest = _detect_existing(target, DIGEST_CANDIDATES)
    if index:
        notes.append(f"existing index/hub page adopted as the index: {index}")
    if digest:
        notes.append(f"existing surrogate digest adopted as the digest: {digest}")

    card = WikiEntry(
        name=wiki_name,
        path=".",
        privacy="",
        index=index or "wiki/index.md",
        digest=digest or "",
    )

    def add_file(rel: str, content: str) -> None:
        if (target / rel).exists():
            notes.append(f"{rel} exists: kept as is (adoption never overwrites)")
            return
        changes.append(FileChange(path=rel, old=None, new=content))

    def add_dir(rel: str) -> None:
        if not (target / rel).is_dir():
            directories.append(rel)

    add_file("AGENTS.md", canonical_agents_md(wiki_name))
    if not index:
        add_file("wiki/index.md", canonical_index_md(wiki_name))
    add_file("wiki/log.md", canonical_log_md(wiki_name))
    add_file(CARD_PATH.as_posix(), serialize_wiki_card(card))
    add_file(f"{MEGAMIND_DIR}/gaps.jsonl", "")
    add_dir("raw")
    add_dir("wiki")
    add_dir(f"{MEGAMIND_DIR}/proposals")
    add_dir(f"{MEGAMIND_DIR}/audit")

    if not changes and not directories:
        return AdoptionPlan(
            plan_id=content_hash(f"noop:{wiki_name}"),
            target=str(target),
            wiki_name=wiki_name,
            status="noop",
            notes=["nothing to add: the canonical scaffold is already present"],
        )
    notes.append(
        "adoption adds sidecar and scaffold files only; existing pages are "
        "never moved, renamed, or rewritten"
    )
    payload = json.dumps(
        {
            "target": wiki_name,
            "changes": [{"path": c.path, "new": c.new} for c in changes],
            "directories": directories,
        },
        sort_keys=True,
    )
    return AdoptionPlan(
        plan_id=content_hash(payload),
        target=str(target),
        wiki_name=wiki_name,
        status="planned",
        changes=changes,
        directories=directories,
        notes=notes,
    )


def apply_adoption(computed: AdoptionPlan, approved_plan_id: str) -> list[str]:
    """Apply an adoption plan. The approved plan id must match exactly."""
    if computed.status == "noop":
        return []
    if approved_plan_id != computed.plan_id:
        raise AdoptPlanMismatch(
            "plan id mismatch: the target changed since the dry run "
            f"(expected {computed.plan_id}). Re-run the dry run and review the new plan."
        )
    target = Path(computed.target)
    created: list[str] = []
    for rel in computed.directories:
        directory = resolve_contained(target, rel)
        directory.mkdir(parents=True, exist_ok=True)
        created.append(rel + "/")
    created_records = []
    for change in computed.changes:
        atomic_write(target, change.path, change.new)
        created.append(change.path)
        created_records.append({"path": change.path, "sha": content_hash(change.new)})
    append_audit(
        target,
        "adopt-apply",
        {
            "plan_id": computed.plan_id,
            "wiki": computed.wiki_name,
            "created_files": [str(record["path"]) for record in created_records],
        },
    )
    # Rollback material: one JSON record the rollback can verify content against.
    rollback_record = {
        "plan_id": computed.plan_id,
        "wiki": computed.wiki_name,
        "files": created_records,
        "directories": [rel + "/" for rel in computed.directories],
    }
    atomic_write(
        target,
        Path(MEGAMIND_DIR) / "audit" / f"adoption-{computed.plan_id}.json",
        json.dumps(rollback_record, indent=2, sort_keys=True) + "\n",
    )
    created.append(f"{MEGAMIND_DIR}/audit/adoption-{computed.plan_id}.json")
    return created


def _load_rollback_record(record_file: Path) -> dict[str, object]:
    """Parse an adoption record. The record is on-disk input, so never trusted."""
    try:
        payload = json.loads(record_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise AdoptError(
            f"adoption record {record_file.name} is not valid JSON: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise AdoptError(f"adoption record {record_file.name} is not a JSON object")
    return payload


def _record_files(record: dict[str, object], name: str) -> list[tuple[str, str]]:
    entries = record.get("files", [])
    if not isinstance(entries, list):
        raise AdoptError(f"adoption record {name} has a malformed files list")
    files: list[tuple[str, str]] = []
    for entry in entries:
        rel = entry.get("path") if isinstance(entry, dict) else None
        sha = entry.get("sha") if isinstance(entry, dict) else None
        if not isinstance(rel, str) or not rel or not isinstance(sha, str) or not sha:
            raise AdoptError(f"adoption record {name} has a file entry without a path and sha")
        files.append((rel, sha))
    return files


def _record_directories(record: dict[str, object], name: str) -> list[str]:
    entries = record.get("directories", [])
    if not isinstance(entries, list):
        raise AdoptError(f"adoption record {name} has a malformed directories list")
    directories: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry:
            raise AdoptError(f"adoption record {name} has a malformed directory entry")
        directories.append(entry)
    return directories


def rollback_adoption(target: Path) -> tuple[list[str], list[str]]:
    """Remove exactly what the latest adoption created. Returns (removed, kept).

    A created file is removed only while its content still matches the hash
    recorded at apply time; a file anyone edited since is kept and reported, as
    is one that can no longer be read back as text, since an unverifiable file
    is never assumed to be untouched. Directories are removed only when empty.
    The audit log itself always stays: it is the record that the adoption and
    the rollback happened.

    The record is validated and every path in it is resolved and contained
    before anything is removed, so a corrupt, truncated, or tampered record
    fails as a typed error with nothing deleted.
    """
    audit_dir = target / MEGAMIND_DIR / "audit"
    records = sorted(audit_dir.glob("adoption-*.json")) if audit_dir.is_dir() else []
    if not records:
        raise AdoptError("no adoption record found; nothing to roll back")
    record_file = records[-1]
    record = _load_rollback_record(record_file)
    files = [
        (rel, sha, resolve_contained(target, rel))
        for rel, sha in _record_files(record, record_file.name)
    ]
    directories = [
        (rel, resolve_contained(target, rel))
        for rel in _record_directories(record, record_file.name)
    ]
    removed: list[str] = []
    kept: list[str] = []
    for rel, sha, path in files:
        if not path.is_file():
            kept.append(f"{rel} (already gone)")
            continue
        try:
            current = content_hash(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            kept.append(f"{rel} (unreadable since adoption: kept)")
            continue
        if current != sha:
            kept.append(f"{rel} (modified since adoption: kept)")
            continue
        path.unlink()
        removed.append(rel)
    for rel, directory in directories:
        try:
            directory.rmdir()
            removed.append(rel)
        except OSError:
            kept.append(f"{rel} (not empty: kept)")
    # The rollback record itself is generated material; remove it too.
    record_file.unlink()
    removed.append(f"{MEGAMIND_DIR}/audit/{record_file.name}")
    append_audit(
        target,
        "adopt-rollback",
        {"plan_id": str(record.get("plan_id", "")), "removed": removed},
    )
    return removed, kept
