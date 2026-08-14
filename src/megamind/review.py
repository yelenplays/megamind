"""Review: the gardening report.

Surfaces work a human (or reviewing agent) should look at: open and uncategorized
proposals, duplicates, stale or superseded knowledge, dead links, and promotion
candidates. Review never changes anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .capture import list_proposals
from .card import compiled_page_dir, is_canonical_page
from .confidence import CLEAN_CORRECTION
from .evidence import EvidenceStore, resolve_corrections
from .evolve import PROPOSAL_MARKER
from .fsops import resolve_contained
from .links import extract_links, link_target_path, page_name_table, resolve_link
from .models import Document, parse_document
from .registry import Registry, WikiEntry


@dataclass
class ReviewReport:
    open_proposals: list[str] = field(default_factory=list)
    uncategorized_proposals: list[str] = field(default_factory=list)
    already_merged_proposals: list[str] = field(default_factory=list)
    duplicate_titles: list[dict[str, object]] = field(default_factory=list)
    stale_pages: list[dict[str, object]] = field(default_factory=list)
    superseded_still_linked: list[dict[str, object]] = field(default_factory=list)
    dead_links: list[dict[str, object]] = field(default_factory=list)
    promotion_candidates: list[dict[str, object]] = field(default_factory=list)
    pending_evidence: list[dict[str, object]] = field(default_factory=list)
    contradictions: list[dict[str, object]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def is_clean(self) -> bool:
        return not any(asdict(self).values())


def _iter_wiki_page_dirs(root: Path, registry: Registry) -> list[tuple[WikiEntry, Path]]:
    """The compiled page directory of every wiki review is allowed to walk."""
    directories: list[tuple[WikiEntry, Path]] = []
    for wiki in registry.wikis:
        try:
            wiki_dir = resolve_contained(root, compiled_page_dir(wiki))
        except ValueError:
            continue
        if not wiki_dir.is_dir():
            continue
        directories.append((wiki, wiki_dir))
    return directories


def _walk_pages(root: Path, wiki: WikiEntry, wiki_dir: Path, pattern: str) -> list[Path]:
    """Entries under a wiki's page tree, minus what is never a page.

    The exclusion only ever removes anything when the compiled directory is the
    root itself, which is the shape an adopted wiki keeps.
    """
    root_resolved = root.resolve()
    entries: list[Path] = []
    for path in sorted(wiki_dir.rglob(pattern)):
        rel = path.relative_to(root_resolved).as_posix()
        if wiki.path == "." and not is_canonical_page(rel):
            continue
        entries.append(path)
    return entries


def _iter_wiki_pages(root: Path, registry: Registry) -> list[tuple[str, Path]]:
    pages: list[tuple[str, Path]] = []
    for wiki, wiki_dir in _iter_wiki_page_dirs(root, registry):
        for path in _walk_pages(root, wiki, wiki_dir, "*.md"):
            pages.append((wiki.name, path))
    return pages


def _page_title(document: Document, path: Path) -> str:
    title = document.frontmatter.get("title")
    if isinstance(title, str) and title:
        return title
    for line in document.body.splitlines():
        if line.strip().startswith("#"):
            return line.strip().lstrip("#").strip()
    return path.stem


def review(root: Path, registry: Registry, today: date | None = None) -> ReviewReport:
    report = ReviewReport()
    reference_day = today or date.today()
    # Both sides of every relative_to stay on the resolved root: `root` is often
    # `.` or a symlinked path, and mixing the two forms raises instead of
    # emitting a report.
    root_resolved = root.resolve()
    all_page_bodies: dict[str, str] = {}
    page_docs: dict[str, Document] = {}
    titles: dict[str, list[str]] = {}
    for _wiki_name, path in _iter_wiki_pages(root, registry):
        rel = path.relative_to(root_resolved).as_posix()
        try:
            document = parse_document(path.read_text(encoding="utf-8"))
        except ValueError:
            continue  # doctor reports malformed pages; review skips them
        all_page_bodies[rel] = document.body
        page_docs[rel] = document
        titles.setdefault(_page_title(document, path).lower(), []).append(rel)

    # Proposals
    for proposal_file in list_proposals(root):
        rel = proposal_file.relative_to(root_resolved).as_posix()
        try:
            document = parse_document(proposal_file.read_text(encoding="utf-8"))
        except ValueError:
            continue
        status = str(document.frontmatter.get("status", ""))
        if status != "proposed":
            continue
        report.open_proposals.append(rel)
        destination = str(document.frontmatter.get("suggested_destination", ""))
        if destination in {"", "uncategorized"}:
            report.uncategorized_proposals.append(rel)
        proposal_id = str(document.frontmatter.get("id", ""))
        marker = PROPOSAL_MARKER.format(id=proposal_id)
        if any(marker in body for body in all_page_bodies.values()):
            report.already_merged_proposals.append(rel)

    # Duplicate titles across pages
    for title, paths in sorted(titles.items()):
        if len(paths) > 1:
            report.duplicate_titles.append({"title": title, "pages": sorted(paths)})

    # Stale, superseded, dead links
    names = page_name_table(root_resolved)
    superseded_pages = {
        rel for rel, doc in page_docs.items() if doc.frontmatter.get("status") == "superseded"
    }
    for rel, document in sorted(page_docs.items()):
        status = str(document.frontmatter.get("status", ""))
        updated_raw = str(document.frontmatter.get("updated", ""))
        if status == "shaky":
            report.stale_pages.append({"page": rel, "reason": "status is shaky"})
        elif updated_raw:
            try:
                updated = date.fromisoformat(updated_raw)
            except ValueError:
                updated = None
            if updated is not None:
                age = (reference_day - updated).days
                if age > registry.budgets.stale_days:
                    report.stale_pages.append(
                        {"page": rel, "reason": f"not updated for {age} days"}
                    )
        source_path = root_resolved / rel
        for link in extract_links(document.body):
            if not resolve_link(root_resolved, source_path, link, names):
                report.dead_links.append({"page": rel, "target": link.target})
                continue
            if status != "superseded":
                target_rel = _link_target_rel(root_resolved, source_path, link, names)
                if target_rel in superseded_pages:
                    report.superseded_still_linked.append(
                        {"superseded": target_rel, "linked_from": rel}
                    )

    # Evidence review is metadata-only. Bodies and source prose are never
    # loaded by this projection. A malformed record is reported as work to do
    # rather than aborting review and the home document that points at doctor.
    evidence_store = EvidenceStore(root)
    notices = [
        notice for _, notice, _ in evidence_store.scan_readonly("corrections") if notice is not None
    ]
    for identifier, record, problem in evidence_store.scan_readonly("evidence"):
        if record is None:
            report.pending_evidence.append(
                {"evidence_id": identifier, "decision": "invalid", "failure": problem}
            )
            continue
        acceptance = record.get("acceptance", {})
        decision = str(acceptance.get("decision", ""))
        failure = str(acceptance.get("failure", ""))
        status = str(resolve_corrections(record, notices)["status"])
        pending = decision in {"deferred", "rejected"}
        if status != CLEAN_CORRECTION and decision == "accepted":
            # Support is already withdrawn by the correction posture; the stored
            # acceptance block is what still has to catch up.
            pending = True
            failure = f"superseded by a {status} correction notice; re-record the artifact"
        if pending:
            report.pending_evidence.append(
                {
                    "evidence_id": record.get("evidence_id", ""),
                    "decision": decision,
                    "failure": failure,
                }
            )
    for identifier, contradiction, problem in evidence_store.scan_readonly("contradictions"):
        if contradiction is None:
            report.contradictions.append(
                {"contradiction_id": identifier, "claim_ids": "", "problem": problem}
            )
            continue
        if contradiction.get("resolution") == "unresolved":
            report.contradictions.append(
                {
                    "contradiction_id": contradiction.get("contradiction_id", ""),
                    "claim_ids": ";".join(contradiction.get("claim_ids", [])),
                    "problem": "",
                }
            )

    # Promotion candidates
    for wiki, wiki_dir in _iter_wiki_page_dirs(root, registry):
        for sub in (p for p in _walk_pages(root, wiki, wiki_dir, "*") if p.is_dir()):
            if sub.parent == wiki_dir:
                # First-level dirs (like topics/) are covered by the wiki's own
                # index; only nested clusters are micro-wiki material.
                continue
            pages = sorted(sub.glob("*.md"))
            page_count = len([p for p in pages if p.name.upper() != "INDEX.MD"])
            has_index = any(p.name.upper() == "INDEX.MD" for p in pages)
            rel = sub.relative_to(root_resolved).as_posix()
            if not has_index and page_count >= registry.budgets.micro_wiki_pages:
                report.promotion_candidates.append(
                    {
                        "path": rel,
                        "kind": "micro-wiki",
                        "reason": f"{page_count} pages without an index: consider a micro-wiki",
                    }
                )
            elif (
                has_index
                and page_count
                >= registry.budgets.micro_wiki_pages * registry.budgets.top_level_topics
            ):
                report.promotion_candidates.append(
                    {
                        "path": rel,
                        "kind": "top-level-wiki",
                        "reason": (
                            f"{page_count} pages: consider proposing a top-level wiki "
                            "(requires explicit human approval)"
                        ),
                    }
                )
    return report


def _link_target_rel(
    root: Path, source_file: Path, link: object, names: dict[str, Path]
) -> str | None:
    from .links import Link

    assert isinstance(link, Link)
    if link.style == "wikilink":
        name = link.target.strip().removesuffix(".md")
        target = names.get(name)
        return target.relative_to(root).as_posix() if target else None
    raw = link_target_path(link.target)
    if not raw:
        return None
    candidate = (source_file.parent / raw).resolve()
    if not candidate.exists() and candidate.suffix == "":
        candidate = candidate.with_suffix(".md")
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:
        return None
