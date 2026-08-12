"""Evolution: plan and apply approved knowledge changes.

Evolve merges or supersedes existing knowledge instead of appending duplicates.
Dry-run is the default: it prints a unified diff and a plan id. Applying requires
passing that plan id back (the approval token), which proves a human or reviewing
agent saw exactly the change being approved. Creating a new top-level wiki
additionally requires an explicit ``--approve-new-wiki`` flag.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .capture import PROPOSALS_DIR
from .card import CANONICAL_COMPILED_DIR, CANONICAL_RAW_DIR, compiled_page_dir
from .fsops import (
    MEGAMIND_DIR,
    append_audit,
    atomic_write,
    backup_existing,
    content_hash,
    remove_contained,
    resolve_contained,
)
from .models import Document, parse_document
from .registry import (
    REGISTRY_PATH,
    ROUTER_FILENAME,
    ROUTER_HEADER,
    Registry,
    WikiEntry,
    generate_router,
    serialize_registry,
)

PROPOSAL_MARKER = "<!-- megamind:proposal:{id} -->"
EVOLVE_TRANSACTION_SCHEMA = "megamind/evolve-rollback/v1"


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
        return f"{compiled_page_dir(wiki)}/topics/{slug}.md"
    if not hint or hint == "uncategorized":
        raise EvolveError("proposal has no destination; pass --dest <page.md> or --dest <WikiName>")
    return f"{hint}/topics/{slug}.md"


def _is_inside_registered_wiki(registry: Registry, page: str) -> bool:
    return any(
        wiki.path == "." or page == wiki.path or page.startswith(wiki.path.rstrip("/") + "/")
        for wiki in registry.wikis
    )


def _new_wiki_identity(page_rel: str, destination_hint: str) -> tuple[str, str]:
    """Derive the new wiki's (name, path): the hint, else the page's first segment.

    A wiki is a directory, so a destination that leaves no directory above the
    page (a bare top-level ``note.md``) is refused here: its wiki path would be
    the page path itself, which cannot be both a file and a parent directory.
    """
    hint = destination_hint.rstrip("/")
    path = hint if hint and not hint.endswith(".md") else page_rel.split("/", 1)[0]
    if path.endswith(".md"):
        raise EvolveError(
            f"destination {page_rel} leaves no directory for a new wiki; pass "
            "--dest <WikiName> or --dest <WikiName>/<page>.md"
        )
    return path.rsplit("/", 1)[-1], path


def _name_keywords(name: str) -> list[str]:
    """Seed routing keywords from a CamelCase wiki name ('LegalWiki' -> ['legal'])."""
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", name)
    keywords = [word.lower() for word in words if word.lower() != "wiki"]
    return keywords


def _new_card(name: str, privacy: str, keywords: list[str]) -> str:
    keyword_text = ", ".join(keywords)
    return (
        "---\n"
        "megamind: routing-card\n"
        f"wiki: {name}\n"
        f"privacy: {privacy}\n"
        f"keywords: [{keyword_text}]\n"
        "---\n"
        "\n"
        f"# {name} routing card\n"
        "\n"
        "Answers: describe the questions this wiki answers.\n"
        "Does not answer: describe what belongs elsewhere.\n"
    )


def _new_index(name: str, page_link: str, page_label: str) -> str:
    return (
        "---\n"
        "megamind: index\n"
        f"wiki: {name}\n"
        "---\n"
        "\n"
        f"# {name} index\n"
        "\n"
        f"- [{page_label}]({page_link})\n"
    )


def _registration_changes(
    root: Path,
    registry: Registry,
    page_rel: str,
    destination_hint: str,
    notes: list[str],
) -> list[FileChange]:
    """Changes that register a newly approved top-level wiki in the same apply.

    The registry entry, routing card and index skeletons, and the regenerated
    router all become plan changes, so the dry-run diff shows them and the
    plan id covers them. Applying then leaves the wiki visible to route,
    review, catalog, and doctor in one step. New wikis start with the
    restrictive company-private posture: content is routable locally, cloud
    access stays none until an owner sets an explicit policy.
    """
    name, wiki_path = _new_wiki_identity(page_rel, destination_hint)
    existing = registry.wiki_by_name(name)
    if existing is not None and existing.path != wiki_path:
        raise EvolveError(
            f"wiki name {name} is already registered at {existing.path}; "
            "choose a different destination"
        )
    keywords = _name_keywords(name)
    entry = WikiEntry(
        name=name,
        path=wiki_path,
        privacy="company-private",
        description="",
        keywords=keywords,
        card=f"{wiki_path}/CARD.md",
        digest="",
        index=f"{wiki_path}/INDEX.md",
    )
    new_registry = Registry(
        version=registry.version, budgets=registry.budgets, wikis=[*registry.wikis, entry]
    )

    changes: list[FileChange] = []
    page_label = page_rel.rsplit("/", 1)[-1].removesuffix(".md").replace("-", " ")
    skeletons = {
        entry.card: _new_card(name, entry.privacy, keywords),
        entry.index: _new_index(name, page_rel[len(wiki_path) + 1 :], page_label),
    }
    for rel, content in skeletons.items():
        if resolve_contained(root, rel).exists():
            notes.append(f"{rel} already exists: kept as is")
            continue
        changes.append(FileChange(path=rel, old=None, new=content))

    registry_text = serialize_registry(new_registry)
    registry_path = resolve_contained(root, REGISTRY_PATH)
    old_registry_text = (
        registry_path.read_text(encoding="utf-8") if registry_path.is_file() else None
    )
    if old_registry_text != registry_text:
        changes.append(
            FileChange(path=REGISTRY_PATH.as_posix(), old=old_registry_text, new=registry_text)
        )

    router_text = generate_router(new_registry)
    router_path = resolve_contained(root, ROUTER_FILENAME)
    old_router_text = router_path.read_text(encoding="utf-8") if router_path.is_file() else None
    if old_router_text is not None and ROUTER_HEADER not in old_router_text:
        notes.append(f"{ROUTER_FILENAME} is hand-edited: not regenerated; doctor will flag it")
    elif old_router_text != router_text:
        changes.append(FileChange(path=ROUTER_FILENAME, old=old_router_text, new=router_text))

    notes.append(
        f"registers new wiki {name} with card/index skeletons and the restrictive "
        "company-private default (cloud access none until an explicit policy is set)"
    )
    return changes


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
    canonical_root = any(wiki.path == "." for wiki in registry.wikis)
    page_path = resolve_contained(root, page_rel)
    raw_root = resolve_contained(root, CANONICAL_RAW_DIR)
    compiled_root = resolve_contained(root, CANONICAL_COMPILED_DIR)
    if canonical_root and (page_path == raw_root or raw_root in page_path.parents):
        raise EvolveError("raw/ is immutable; evolve may write only compiled wiki/ pages")
    if canonical_root and compiled_root not in page_path.parents:
        raise EvolveError(
            "canonical evolution may write only compiled wiki/ pages; pass --dest wiki/<page>.md"
        )
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
        if canonical_root and (old_path == raw_root or raw_root in old_path.parents):
            raise EvolveError("raw/ is immutable and cannot be superseded")
        if canonical_root and compiled_root not in old_path.parents:
            raise EvolveError("canonical evolution may supersede only compiled wiki/ pages")
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

    if creates_new_wiki:
        # Registration and skeletons are plan changes too: they appear in the
        # reviewed diff and the plan id covers them, so an approved apply wires
        # the new wiki into routing in the same step.
        changes.extend(_registration_changes(root, registry, page_rel, hint, notes))

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


def _tree_digest(entries: list[dict[str, object]], value_key: str) -> str:
    state = [
        {
            "path": str(entry["path"]),
            "sha256": (
                hashlib.sha256(str(entry[value_key]).encode("utf-8")).hexdigest()
                if entry[value_key] is not None
                else None
            ),
        }
        for entry in entries
        if entry["role"] != "proposal"
    ]
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _current_tree_digest(root: Path, entries: list[dict[str, object]]) -> str:
    state = []
    for entry in entries:
        if entry["role"] == "proposal":
            continue
        current = _entry_current(root, entry)
        state.append(
            {
                "path": str(entry["path"]),
                "sha256": (
                    hashlib.sha256(current.encode("utf-8")).hexdigest()
                    if current is not None
                    else None
                ),
            }
        )
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evolve_manifest_rel(plan_id: str) -> Path:
    return Path(MEGAMIND_DIR) / "audit" / f"evolve-{plan_id}.json"


def _read_evolve_manifest(root: Path, plan_id: str) -> dict[str, object] | None:
    path = resolve_contained(root, _evolve_manifest_rel(plan_id))
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvolveError("evolution transaction manifest is unreadable") from error
    if (
        not isinstance(value, dict)
        or value.get("schema") != EVOLVE_TRANSACTION_SCHEMA
        or value.get("plan_id") != plan_id
        or value.get("state") not in {"pending", "applied", "rolled_back"}
        or not isinstance(value.get("proposal_id"), str)
        or not isinstance(value.get("action"), str)
        or not isinstance(value.get("destination"), str)
        or not isinstance(value.get("pre_change_tree_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("pre_change_tree_sha256", "")))
        or not isinstance(value.get("applied_tree_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(value.get("applied_tree_sha256", "")))
        or not isinstance(value.get("entries"), list)
    ):
        raise EvolveError("evolution transaction manifest is invalid")
    for entry in value["entries"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or (entry.get("old") is not None and not isinstance(entry.get("old"), str))
            or not isinstance(entry.get("new"), str)
            or not isinstance(entry.get("backup", ""), str)
            or entry.get("role") not in {"compiled", "proposal", "registration"}
        ):
            raise EvolveError("evolution transaction manifest is malformed")
    return value


def _write_evolve_manifest(root: Path, manifest: dict[str, object]) -> None:
    atomic_write(
        root,
        _evolve_manifest_rel(str(manifest["plan_id"])),
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        durable=True,
    )


def _manifest_matches_proposal(manifest: dict[str, object], proposal_ref: str) -> None:
    proposal_id = str(manifest["proposal_id"])
    if proposal_ref not in {
        proposal_id,
        f"{proposal_id}.md",
        (PROPOSALS_DIR / f"{proposal_id}.md").as_posix(),
    }:
        raise PlanMismatch("plan id belongs to a different proposal")


def _entry_current(root: Path, entry: dict[str, object]) -> str | None:
    path = resolve_contained(root, str(entry["path"]))
    if path.exists() and not path.is_file():
        raise EvolveError(
            f"evolution transaction refused: {entry['path']} changed outside the plan"
        )
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _resume_evolve_manifest(root: Path, manifest: dict[str, object]) -> list[str]:
    if manifest["state"] == "rolled_back":
        raise PlanMismatch("evolution plan was already rolled back")
    entries = manifest["entries"]
    assert isinstance(entries, list)
    typed_entries = [entry for entry in entries if isinstance(entry, dict)]
    current = [_entry_current(root, entry) for entry in typed_entries]
    for entry, value in zip(typed_entries, current, strict=True):
        if value not in {entry["old"], entry["new"]}:
            raise EvolveError(
                f"evolution recovery refused: {entry['path']} changed outside the plan"
            )
    changed: list[str] = []
    for entry, value in zip(typed_entries, current, strict=True):
        if value == entry["new"]:
            continue
        atomic_write(root, str(entry["path"]), str(entry["new"]), durable=True)
        changed.append(str(entry["path"]))
    if manifest["state"] != "applied":
        for entry in typed_entries:
            action = (
                "evolve-apply-proposal-status" if entry["role"] == "proposal" else "evolve-apply"
            )
            append_audit(
                root,
                action,
                {
                    "plan_id": str(manifest["plan_id"]),
                    "proposal_id": str(manifest["proposal_id"]),
                    "plan_action": str(manifest["action"]),
                    "path": str(entry["path"]),
                    "backup": str(entry.get("backup") or "") or None,
                },
            )
        manifest["state"] = "applied"
        _write_evolve_manifest(root, manifest)
    return [
        str(entry["path"])
        for entry in typed_entries
        if entry["role"] != "proposal" and str(entry["path"]) in changed
    ]


def resume_evolution(root: Path, plan_id: str, proposal_ref: str) -> dict[str, object] | None:
    """Resume an interrupted apply from its durable write-ahead record."""
    manifest = _read_evolve_manifest(root, plan_id)
    if manifest is None:
        return None
    _manifest_matches_proposal(manifest, proposal_ref)
    was_applied = manifest["state"] == "applied"
    changed = _resume_evolve_manifest(root, manifest)
    return {
        "status": "noop" if was_applied and not changed else "applied",
        "action": str(manifest["action"]),
        "pre_change_tree_sha256": str(manifest["pre_change_tree_sha256"]),
        "applied_tree_sha256": str(manifest["applied_tree_sha256"]),
        "proposal_id": str(manifest["proposal_id"]),
        "destination": str(manifest["destination"]),
        "plan_id": plan_id,
        "applied": changed,
    }


def evolution_tree_hashes(root: Path, plan_id: str) -> dict[str, str]:
    manifest = _read_evolve_manifest(root, plan_id)
    if manifest is None:
        return {}
    return {
        "pre_change_tree_sha256": str(manifest["pre_change_tree_sha256"]),
        "applied_tree_sha256": str(manifest["applied_tree_sha256"]),
    }


def rollback_evolution(root: Path, plan_id: str, proposal_ref: str) -> dict[str, object]:
    """Restore exactly the bytes an evolution transaction replaced or created."""
    manifest = _read_evolve_manifest(root, plan_id)
    if manifest is None:
        raise PlanMismatch("evolution plan has no rollback transaction")
    _manifest_matches_proposal(manifest, proposal_ref)
    if manifest["state"] == "rolled_back":
        raise PlanMismatch("evolution plan was already rolled back")
    entries = manifest["entries"]
    assert isinstance(entries, list)
    typed_entries = [entry for entry in entries if isinstance(entry, dict)]
    current = [_entry_current(root, entry) for entry in typed_entries]
    for entry, value in zip(typed_entries, current, strict=True):
        if value not in {entry["old"], entry["new"]}:
            raise EvolveError(
                f"rollback refused: {entry['path']} changed outside the approved plan"
            )
    restored: list[str] = []
    for entry, value in reversed(list(zip(typed_entries, current, strict=True))):
        if value == entry["old"]:
            continue
        old = entry["old"]
        if old is None:
            remove_contained(root, str(entry["path"]), durable=True)
        else:
            atomic_write(root, str(entry["path"]), str(old), durable=True)
        if entry["role"] != "proposal":
            restored.append(str(entry["path"]))
    manifest["state"] = "rolled_back"
    _write_evolve_manifest(root, manifest)
    restored.sort()
    append_audit(
        root,
        "evolve-rollback",
        {
            "plan_id": plan_id,
            "proposal_id": str(manifest["proposal_id"]),
            "files": restored,
            "manifest": _evolve_manifest_rel(plan_id).name,
        },
    )
    restored_digest = _current_tree_digest(root, typed_entries)
    return {
        "status": "rolled_back",
        "action": str(manifest["action"]),
        "pre_change_tree_sha256": str(manifest["pre_change_tree_sha256"]),
        "applied_tree_sha256": str(manifest["applied_tree_sha256"]),
        "restored_tree_sha256": restored_digest,
        "proposal_id": str(manifest["proposal_id"]),
        "destination": str(manifest["destination"]),
        "plan_id": plan_id,
        "rolled_back": restored,
    }


def apply_plan(
    root: Path,
    registry: Registry,
    computed: EvolutionPlan,
    approved_plan_id: str,
    approve_new_wiki: bool = False,
    today: date | None = None,
) -> list[str]:
    """Apply a reviewed plan through a durable, rollback-capable transaction."""
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
    existing = _read_evolve_manifest(root, computed.plan_id)
    if existing is not None:
        _manifest_matches_proposal(existing, computed.proposal_id)
        return _resume_evolve_manifest(root, existing)

    entries: list[dict[str, object]] = []
    registration_paths = {REGISTRY_PATH.as_posix(), ROUTER_FILENAME}
    for change in computed.changes:
        backup = backup_existing(root, change.path, durable=True)
        entries.append(
            {
                "path": change.path,
                "old": change.old,
                "new": change.new,
                "backup": backup.name if backup else "",
                "role": "registration" if change.path in registration_paths else "compiled",
            }
        )

    proposal_rel = (PROPOSALS_DIR / f"{computed.proposal_id}.md").as_posix()
    proposal_file = resolve_contained(root, proposal_rel)
    if proposal_file.is_file():
        proposal_old = proposal_file.read_text(encoding="utf-8")
        proposal_doc = parse_document(proposal_old)
        proposal_doc.frontmatter["status"] = "applied"
        proposal_doc.frontmatter["applied_to"] = computed.destination
        proposal_doc.frontmatter["applied_on"] = (today or date.today()).isoformat()
        backup = backup_existing(root, proposal_rel, durable=True)
        entries.append(
            {
                "path": proposal_rel,
                "old": proposal_old,
                "new": proposal_doc.render(),
                "backup": backup.name if backup else "",
                "role": "proposal",
            }
        )
    manifest: dict[str, object] = {
        "schema": EVOLVE_TRANSACTION_SCHEMA,
        "plan_id": computed.plan_id,
        "proposal_id": computed.proposal_id,
        "action": computed.action,
        "destination": computed.destination,
        "state": "pending",
        "pre_change_tree_sha256": _tree_digest(entries, "old"),
        "applied_tree_sha256": _tree_digest(entries, "new"),
        "entries": entries,
    }
    _write_evolve_manifest(root, manifest)
    return _resume_evolve_manifest(root, manifest)
