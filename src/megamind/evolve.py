"""Evolution: plan and apply approved knowledge changes.

Evolve merges or supersedes existing knowledge instead of appending duplicates.
Dry-run is the default: it prints a unified diff and a plan id. Applying requires
passing that plan id back (the approval token), which proves a human or reviewing
agent saw exactly the change being approved. Creating a new top-level wiki
additionally requires an explicit ``--approve-new-wiki`` flag.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .capture import PROPOSALS_DIR
from .fsops import append_audit, atomic_write, backup_existing, content_hash, resolve_contained
from .models import Document, parse_document
from .registry import Registry

PROPOSAL_MARKER = "<!-- megamind:proposal:{id} -->"


class EvolveError(ValueError):
    """The evolution request is invalid or cannot be applied safely."""

    code = "evolve_invalid"


class ProposalNotFound(EvolveError):
    """The referenced proposal does not exist."""

    code = "proposal_not_found"


class PlanMismatch(EvolveError):
    """The approved plan id no longer matches the computed plan."""

    code = "plan_mismatch"


class ApprovalRequired(EvolveError):
    """The change needs an explicit human approval flag."""

    code = "approval_required"


@dataclass
class FileChange:
    path: str
    old: str | None
    new: str

    def diff(self) -> str:
        old_lines = (self.old or "").splitlines(keepends=True)
        new_lines = self.new.splitlines(keepends=True)
        from_label = self.path if self.old is not None else "/dev/null"
        return "".join(
            difflib.unified_diff(old_lines, new_lines, fromfile=from_label, tofile=self.path)
        )


@dataclass
class EvolutionPlan:
    plan_id: str
    proposal_id: str
    action: str  # "create" | "merge" | "supersede" | "noop"
    destination: str
    supersedes: str | None
    creates_new_wiki: bool
    changes: list[FileChange] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        for change in data["changes"]:
            change["diff"] = FileChange(**change).diff()
            del change["old"]
            del change["new"]
        return data

    def render_diff(self) -> str:
        return "\n".join(change.diff() for change in self.changes if change.diff())


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "note"


def _load_proposal(root: Path, ref: str) -> tuple[str, Document]:
    """Accept a proposal id or a path to a proposal file."""
    candidate = ref if ref.endswith(".md") else (PROPOSALS_DIR / f"{ref}.md").as_posix()
    path = resolve_contained(root, candidate)
    if not path.is_file():
        raise ProposalNotFound(f"proposal not found: {ref}")
    document = parse_document(path.read_text(encoding="utf-8"))
    proposal_id = str(document.frontmatter.get("id", ""))
    if document.frontmatter.get("megamind") != "proposal" or not proposal_id:
        raise EvolveError(f"not a megamind proposal: {ref}")
    return proposal_id, document


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped
    return "note"


def _destination_page(registry: Registry, destination: str, proposal_body: str) -> str:
    """Resolve a destination hint into a concrete page path."""
    if destination.endswith(".md"):
        return destination
    hint = destination.rstrip("/")
    wiki = registry.wiki_by_name(hint) or next((w for w in registry.wikis if w.path == hint), None)
    slug = _slugify(_first_heading(proposal_body))
    if wiki is not None:
        return f"{wiki.path}/topics/{slug}.md"
    if not hint or hint == "uncategorized":
        raise EvolveError("proposal has no destination; pass --dest <page.md> or --dest <WikiName>")
    return f"{hint}/topics/{slug}.md"


def _is_inside_registered_wiki(registry: Registry, page: str) -> bool:
    return any(
        page == wiki.path or page.startswith(wiki.path.rstrip("/") + "/") for wiki in registry.wikis
    )


def _merge_section(proposal_id: str, document: Document) -> str:
    captured = str(document.frontmatter.get("captured", "unknown-date"))
    knowledge_type = str(document.frontmatter.get("type", "fact"))
    source = str(document.frontmatter.get("source", "unknown"))
    marker = PROPOSAL_MARKER.format(id=proposal_id)
    body = document.body.strip()
    return (
        f"\n{marker}\n"
        f"## Update {captured} ({knowledge_type})\n\n"
        f"{body}\n\n"
        f"Provenance: proposal {proposal_id}, source: {source}\n"
    )


def _new_page(proposal_id: str, document: Document, title: str) -> str:
    captured = str(document.frontmatter.get("captured", "unknown-date"))
    knowledge_type = str(document.frontmatter.get("type", "fact"))
    source = str(document.frontmatter.get("source", "unknown"))
    page = Document(
        frontmatter={
            "title": title,
            "type": knowledge_type,
            "status": "active",
            "created": captured,
            "updated": captured,
            "provenance": [f"proposal {proposal_id}", f"source: {source}"],
        },
        body=(f"\n# {title}\n{_merge_section(proposal_id, document)}"),
    )
    return page.render()


def plan(
    root: Path,
    registry: Registry,
    proposal_ref: str,
    destination: str | None = None,
    supersedes: str | None = None,
) -> EvolutionPlan:
    """Compute a deterministic evolution plan. Read-only."""
    proposal_id, document = _load_proposal(root, proposal_ref)
    notes: list[str] = []
    if str(document.frontmatter.get("status")) == "applied":
        return EvolutionPlan(
            plan_id=content_hash(f"noop:{proposal_id}"),
            proposal_id=proposal_id,
            action="noop",
            destination=str(document.frontmatter.get("applied_to", "")),
            supersedes=None,
            creates_new_wiki=False,
            notes=["proposal already applied: nothing to do"],
        )

    hint = destination or str(document.frontmatter.get("suggested_destination", ""))
    page_rel = _destination_page(registry, hint, document.body)
    page_path = resolve_contained(root, page_rel)
    creates_new_wiki = not _is_inside_registered_wiki(registry, page_rel)
    if creates_new_wiki:
        notes.append(
            "destination is outside every registered wiki: applying creates a new "
            "top-level wiki and requires --approve-new-wiki"
        )

    changes: list[FileChange] = []
    marker = PROPOSAL_MARKER.format(id=proposal_id)
    if page_path.is_file():
        old_text = page_path.read_text(encoding="utf-8")
        if marker in old_text:
            return EvolutionPlan(
                plan_id=content_hash(f"noop:{proposal_id}:{page_rel}"),
                proposal_id=proposal_id,
                action="noop",
                destination=page_rel,
                supersedes=None,
                creates_new_wiki=False,
                notes=["destination already contains this proposal: nothing to do"],
            )
        action = "merge"
        new_text = old_text.rstrip("\n") + "\n" + _merge_section(proposal_id, document)
        changes.append(FileChange(path=page_rel, old=old_text, new=new_text))
    else:
        action = "create"
        title = _first_heading(document.body)
        changes.append(
            FileChange(path=page_rel, old=None, new=_new_page(proposal_id, document, title))
        )

    if supersedes:
        old_rel = supersedes
        old_path = resolve_contained(root, old_rel)
        if not old_path.is_file():
            raise EvolveError(f"page to supersede not found: {old_rel}")
        if old_path == page_path:
            raise EvolveError("a page cannot supersede itself; omit --supersedes to merge")
        action = "supersede"
        old_doc = parse_document(old_path.read_text(encoding="utf-8"))
        old_doc.frontmatter["status"] = "superseded"
        old_doc.frontmatter["superseded_by"] = page_rel
        changes.append(
            FileChange(path=old_rel, old=old_path.read_text(encoding="utf-8"), new=old_doc.render())
        )
        notes.append(f"{old_rel} will be marked superseded by {page_rel}")

    payload = json.dumps(
        {
            "proposal": proposal_id,
            "action": action,
            "changes": [{"path": c.path, "new": c.new} for c in changes],
        },
        sort_keys=True,
    )
    return EvolutionPlan(
        plan_id=content_hash(payload),
        proposal_id=proposal_id,
        action=action,
        destination=page_rel,
        supersedes=supersedes,
        creates_new_wiki=creates_new_wiki,
        changes=changes,
        notes=notes,
    )


def apply_plan(
    root: Path,
    registry: Registry,
    computed: EvolutionPlan,
    approved_plan_id: str,
    approve_new_wiki: bool = False,
    today: date | None = None,
) -> list[str]:
    """Apply a previously reviewed plan. The approved plan id must match exactly."""
    if computed.action == "noop":
        return []
    if approved_plan_id != computed.plan_id:
        raise PlanMismatch(
            "plan id mismatch: the vault changed since the dry run "
            f"(expected {computed.plan_id}). Re-run the dry run and review the new diff."
        )
    if computed.creates_new_wiki and not approve_new_wiki:
        raise ApprovalRequired(
            "applying would create a new top-level wiki; re-run with --approve-new-wiki "
            "after human approval"
        )
    applied: list[str] = []
    for change in computed.changes:
        backup = backup_existing(root, change.path)
        atomic_write(root, change.path, change.new)
        applied.append(change.path)
        append_audit(
            root,
            "evolve-apply",
            {
                "plan_id": computed.plan_id,
                "proposal_id": computed.proposal_id,
                "plan_action": computed.action,
                "path": change.path,
                "backup": backup.name if backup else None,
            },
        )
    # Mark the proposal applied (outside the plan hash so apply day never shifts the id).
    proposal_rel = (PROPOSALS_DIR / f"{computed.proposal_id}.md").as_posix()
    proposal_file = resolve_contained(root, proposal_rel)
    if proposal_file.is_file():
        proposal_doc = parse_document(proposal_file.read_text(encoding="utf-8"))
        proposal_doc.frontmatter["status"] = "applied"
        proposal_doc.frontmatter["applied_to"] = computed.destination
        proposal_doc.frontmatter["applied_on"] = (today or date.today()).isoformat()
        backup = backup_existing(root, proposal_rel)
        atomic_write(root, proposal_rel, proposal_doc.render())
        append_audit(
            root,
            "evolve-apply-proposal-status",
            {
                "plan_id": computed.plan_id,
                "proposal_id": computed.proposal_id,
                "plan_action": computed.action,
                "path": proposal_rel,
                "backup": backup.name if backup else None,
            },
        )
    return applied
