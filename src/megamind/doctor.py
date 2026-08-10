"""Doctor: integrity validation for a Megamind-managed vault.

Doctor validates structure and safety invariants: registry schema, path
containment, routing card health, generated router consistency, page metadata,
link integrity, proposal hygiene, and unsafe symlinks. It is read-only and
reports findings with severities; any error makes the command exit non-zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .access import policy_findings
from .capture import list_proposals
from .fsops import MEGAMIND_DIR, PathEscapeError, resolve_contained
from .links import extract_links, page_name_table, resolve_link
from .models import KNOWLEDGE_TYPES, LIFECYCLE_STATUSES, FrontmatterError, parse_document
from .registry import (
    ROUTER_FILENAME,
    ROUTER_HEADER,
    Registry,
    RegistryError,
    generate_router,
    load_registry,
)

PROPOSAL_STATUSES = ("proposed", "applied", "rejected")


@dataclass
class Finding:
    check: str
    severity: str  # "error" | "warning"
    path: str
    message: str


def _error(check: str, path: str, message: str) -> Finding:
    return Finding(check=check, severity="error", path=path, message=message)


def _warning(check: str, path: str, message: str) -> Finding:
    return Finding(check=check, severity="warning", path=path, message=message)


def _check_dates(frontmatter: dict[str, object], rel: str, findings: list[Finding]) -> None:
    for key in ("created", "updated", "captured", "applied_on"):
        raw = frontmatter.get(key)
        if raw is None:
            continue
        try:
            date.fromisoformat(str(raw))
        except ValueError:
            findings.append(_error("dates", rel, f"frontmatter '{key}' is not an ISO date: {raw}"))


def _check_symlinks(root: Path, findings: list[Finding]) -> None:
    root_resolved = root.resolve()
    for path in sorted(root_resolved.rglob("*")):
        if path.is_symlink():
            rel = path.relative_to(root_resolved).as_posix()
            try:
                target = path.resolve(strict=False)
                target.relative_to(root_resolved)
            except ValueError:
                findings.append(_error("symlinks", rel, "symlink resolves outside the vault root"))


def _check_router(root: Path, registry: Registry, findings: list[Finding]) -> None:
    router_path = root / ROUTER_FILENAME
    if not router_path.is_file():
        findings.append(
            _warning(
                "router", ROUTER_FILENAME, "generated router missing; run 'megamind-axi init .'"
            )
        )
        return
    actual = router_path.read_text(encoding="utf-8")
    if ROUTER_HEADER not in actual:
        findings.append(
            _warning(
                "router",
                ROUTER_FILENAME,
                "router lacks the generated-file header; it may be hand-edited",
            )
        )
    if actual != generate_router(registry):
        findings.append(
            _error(
                "router",
                ROUTER_FILENAME,
                "router is out of sync with the registry; regenerate it",
            )
        )


def _check_wikis(root: Path, registry: Registry, findings: list[Finding]) -> None:
    for wiki in registry.wikis:
        try:
            wiki_dir = resolve_contained(root, wiki.path)
        except PathEscapeError:
            findings.append(_error("containment", wiki.path, f"wiki {wiki.name} escapes the root"))
            continue
        if not wiki_dir.is_dir():
            findings.append(_error("registry", wiki.path, f"wiki {wiki.name} path does not exist"))
            continue
        for label, rel in (("card", wiki.card), ("digest", wiki.digest), ("index", wiki.index)):
            if not rel:
                continue
            try:
                artifact = resolve_contained(root, rel)
            except PathEscapeError:
                findings.append(_error("containment", rel, f"{wiki.name} {label} escapes the root"))
                continue
            if not artifact.is_file():
                findings.append(_warning("registry", rel, f"{wiki.name} {label} file missing"))
        if wiki.card:
            card_path = root.resolve() / wiki.card
            if card_path.is_file():
                try:
                    card_doc = parse_document(card_path.read_text(encoding="utf-8"))
                except FrontmatterError as error:
                    findings.append(
                        _error("cards", wiki.card, f"card frontmatter invalid: {error}")
                    )
                    continue
                if card_doc.frontmatter.get("megamind") != "routing-card":
                    findings.append(
                        _warning("cards", wiki.card, "card lacks 'megamind: routing-card' marker")
                    )
        if not wiki.keywords:
            findings.append(
                _warning("cards", wiki.path, f"wiki {wiki.name} has no routing keywords")
            )


def _check_pages(root: Path, registry: Registry, findings: list[Finding]) -> None:
    root_resolved = root.resolve()
    names = page_name_table(root_resolved)
    for wiki in registry.wikis:
        wiki_dir = root_resolved / wiki.path
        if not wiki_dir.is_dir():
            continue
        for path in sorted(wiki_dir.rglob("*.md")):
            rel = path.relative_to(root_resolved).as_posix()
            try:
                document = parse_document(path.read_text(encoding="utf-8"))
            except FrontmatterError as error:
                findings.append(_error("pages", rel, f"frontmatter invalid: {error}"))
                continue
            frontmatter = document.frontmatter
            if not frontmatter:
                continue  # plain pages without metadata are allowed
            page_type = frontmatter.get("type")
            if page_type is not None and str(page_type) not in KNOWLEDGE_TYPES:
                findings.append(_error("pages", rel, f"unknown knowledge type: {page_type}"))
            status = frontmatter.get("status")
            if status is not None and str(status) not in LIFECYCLE_STATUSES:
                findings.append(_error("pages", rel, f"unknown lifecycle status: {status}"))
            if str(status) == "superseded":
                successor = str(frontmatter.get("superseded_by", ""))
                if not successor:
                    findings.append(_error("pages", rel, "superseded page lacks 'superseded_by'"))
                elif not (root_resolved / successor).is_file():
                    findings.append(
                        _error("pages", rel, f"superseded_by target missing: {successor}")
                    )
            if str(status) in {"confirmed", "active"} and not frontmatter.get("provenance"):
                findings.append(
                    _warning("provenance", rel, "confirmed/active page has no provenance")
                )
            _check_dates(dict(frontmatter), rel, findings)
            for link in extract_links(document.body):
                if not resolve_link(root_resolved, path, link, names):
                    findings.append(_error("links", rel, f"dead link: {link.target}"))


def _check_proposals(root: Path, findings: list[Finding]) -> None:
    root_resolved = root.resolve()
    for proposal_file in list_proposals(root):
        rel = proposal_file.relative_to(root_resolved).as_posix()
        try:
            document = parse_document(proposal_file.read_text(encoding="utf-8"))
        except FrontmatterError as error:
            findings.append(_error("proposals", rel, f"frontmatter invalid: {error}"))
            continue
        frontmatter = document.frontmatter
        if frontmatter.get("megamind") != "proposal":
            findings.append(_error("proposals", rel, "missing 'megamind: proposal' marker"))
            continue
        proposal_id = str(frontmatter.get("id", ""))
        if proposal_id != proposal_file.stem:
            findings.append(_error("proposals", rel, f"id '{proposal_id}' does not match filename"))
        status = str(frontmatter.get("status", ""))
        if status not in PROPOSAL_STATUSES:
            findings.append(_error("proposals", rel, f"unknown proposal status: {status}"))
        if not frontmatter.get("source"):
            findings.append(_error("proposals", rel, "proposal lacks a source"))
        knowledge_type = str(frontmatter.get("type", ""))
        if knowledge_type not in KNOWLEDGE_TYPES:
            findings.append(_error("proposals", rel, f"unknown knowledge type: {knowledge_type}"))
        _check_dates(dict(frontmatter), rel, findings)


def _check_budgets(registry: Registry, findings: list[Finding]) -> None:
    budgets = registry.budgets
    if budgets.max_context_chars < 500:
        findings.append(
            _warning(
                "budgets",
                "registry",
                f"max_context_chars={budgets.max_context_chars} is too small to be useful",
            )
        )
    if budgets.max_candidates > 50:
        findings.append(
            _warning(
                "budgets",
                "registry",
                f"max_candidates={budgets.max_candidates} defeats the point of routing",
            )
        )


def _check_registration(root: Path, registry: Registry, findings: list[Finding]) -> None:
    """Flag top-level directories that look like wikis but are not registered.

    A wiki-shaped directory (it contains Markdown pages) outside every
    registered wiki path is invisible to routing, review, and catalog until it
    is registered, so doctor says so instead of staying green.
    """
    root_resolved = root.resolve()
    registered = [wiki.path.rstrip("/") for wiki in registry.wikis]
    for child in sorted(root_resolved.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        name = child.name
        if name.startswith(".") or name == MEGAMIND_DIR:
            continue
        if any(name == path or path.startswith(name + "/") for path in registered):
            continue
        if any(path.suffix == ".md" for path in child.rglob("*.md")):
            findings.append(
                _warning(
                    "registration",
                    name,
                    f"{name} looks like a wiki but is not registered; it is invisible "
                    "to route, review, and catalog until it is added to the registry",
                )
            )


def _check_schema_version(registry: Registry, findings: list[Finding]) -> None:
    if registry.version < 2:
        findings.append(
            _warning(
                "registry",
                f"{MEGAMIND_DIR}/registry.json",
                "registry is schema v1; run 'megamind-axi migrate' to upgrade to v2 "
                "(adds the access-policy card fields with restrictive defaults)",
            )
        )


def _check_access_policy(registry: Registry, findings: list[Finding]) -> None:
    for wiki in registry.wikis:
        for finding in policy_findings(wiki):
            entry = (
                _error("access", wiki.path, finding.message)
                if finding.severity == "error"
                else _warning("access", wiki.path, finding.message)
            )
            findings.append(entry)


def run_doctor(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        registry = load_registry(root)
    except RegistryError as error:
        return [_error("registry", f"{MEGAMIND_DIR}/registry.json", str(error))]
    _check_schema_version(registry, findings)
    _check_budgets(registry, findings)
    _check_wikis(root, registry, findings)
    _check_access_policy(registry, findings)
    _check_registration(root, registry, findings)
    _check_router(root, registry, findings)
    _check_pages(root, registry, findings)
    _check_proposals(root, findings)
    _check_symlinks(root, findings)
    findings.sort(key=lambda f: (f.severity != "error", f.check, f.path, f.message))
    return findings


def has_errors(findings: list[Finding]) -> bool:
    return any(finding.severity == "error" for finding in findings)
