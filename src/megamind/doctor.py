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
from typing import Any

from .access import policy_findings
from .capture import list_proposals
from .card import CardError, load_wiki_card
from .evidence import STORE_KINDS, EvidenceStore, correction_head
from .fsops import MEGAMIND_DIR, PathEscapeError, resolve_contained
from .gardening import validate_gap_journal
from .links import extract_links, page_name_table, resolve_link
from .models import KNOWLEDGE_TYPES, LIFECYCLE_STATUSES, FrontmatterError, parse_document
from .registry import (
    ROUTER_FILENAME,
    ROUTER_HEADER,
    Registry,
    RegistryError,
    RegistryNotInitialized,
    generate_router,
    load_registry,
)
from .research import ResearchStore

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


def _check_gap_journal(root: Path, findings: list[Finding]) -> None:
    """The gap journal lives at whatever root the gap commands were given, so
    both root shapes have to validate it."""
    for message in validate_gap_journal(root):
        findings.append(_error("gaps", f"{MEGAMIND_DIR}/gaps.jsonl", message))


def _evidence_rel(kind: str, identifier: str) -> str:
    return f"{MEGAMIND_DIR}/evidence/{kind}/{identifier}.json"


def _check_references(
    records: dict[str, dict[str, dict[str, Any]]], findings: list[Finding]
) -> None:
    """Every cross-record reference must name a record this vault holds.

    Each validator can only prove its own document, so the identities that
    span documents (claim to quotation, claim to evidence, quotation to
    evidence, packet to claim) have no owner until the whole set is readable.
    Doctor is that owner.
    """
    for identifier, quotation in sorted(records["quotations"].items()):
        artifact = records["evidence"].get(quotation["evidence_id"])
        if artifact is None:
            findings.append(
                _error(
                    "evidence",
                    _evidence_rel("quotations", identifier),
                    f"quotation cites unknown evidence record {quotation['evidence_id']}",
                )
            )
        elif str(artifact["snapshot"]["normalized_sha256"]) != quotation["against_hash"]:
            findings.append(
                _error(
                    "evidence",
                    _evidence_rel("quotations", identifier),
                    f"quotation against_hash {quotation['against_hash']} is not the normalized "
                    f"snapshot of evidence record {quotation['evidence_id']}",
                )
            )
    for identifier, claim in sorted(records["claims"].items()):
        for support in claim["supported_by"]:
            cited = records["quotations"].get(support["quotation_id"])
            if cited is None:
                findings.append(
                    _error(
                        "evidence",
                        _evidence_rel("claims", identifier),
                        f"claim cites unknown quotation {support['quotation_id']}",
                    )
                )
            elif str(cited["evidence_id"]) != support["evidence_id"]:
                findings.append(
                    _error(
                        "evidence",
                        _evidence_rel("claims", identifier),
                        f"claim pairs quotation {support['quotation_id']} with evidence record "
                        f"{support['evidence_id']}, but that span is bound to "
                        f"{cited['evidence_id']}",
                    )
                )
            if support["evidence_id"] not in records["evidence"]:
                findings.append(
                    _error(
                        "evidence",
                        _evidence_rel("claims", identifier),
                        f"claim cites unknown evidence record {support['evidence_id']}",
                    )
                )
    for identifier, contradiction in sorted(records["contradictions"].items()):
        for claim_id in contradiction["claim_ids"]:
            if claim_id not in records["claims"]:
                findings.append(
                    _error(
                        "evidence",
                        _evidence_rel("contradictions", identifier),
                        f"contradiction cites unknown claim {claim_id}",
                    )
                )
    _check_correction_chains(records, findings)


def _check_correction_chains(
    records: dict[str, dict[str, dict[str, Any]]], findings: list[Finding]
) -> None:
    """A correction history must stay a single ordered chain per artifact.

    Only one notice can be the current posture. A dangling or forked chain
    would make an artifact's correction status undecidable, which is exactly
    the state that must never be reached silently.
    """
    notices = records["corrections"]
    for identifier, notice in sorted(notices.items()):
        if notice["evidence_id"] not in records["evidence"]:
            findings.append(
                _error(
                    "evidence",
                    _evidence_rel("corrections", identifier),
                    f"correction notice cites unknown evidence record {notice['evidence_id']}",
                )
            )
        supersedes = str(notice["supersedes"])
        prior = notices.get(supersedes) if supersedes else None
        if supersedes and prior is None:
            findings.append(
                _error(
                    "evidence",
                    _evidence_rel("corrections", identifier),
                    f"correction notice supersedes unknown notice {supersedes}",
                )
            )
        elif prior is not None and str(prior["evidence_id"]) != str(notice["evidence_id"]):
            findings.append(
                _error(
                    "evidence",
                    _evidence_rel("corrections", identifier),
                    f"correction notice supersedes {supersedes}, which belongs to a different "
                    f"evidence record {prior['evidence_id']}",
                )
            )
    for evidence_id in sorted({str(notice["evidence_id"]) for notice in notices.values()}):
        chain = [notice for notice in notices.values() if str(notice["evidence_id"]) == evidence_id]
        if correction_head(evidence_id, chain) is None:
            findings.append(
                _error(
                    "evidence",
                    _evidence_rel("evidence", evidence_id),
                    "correction chain has no single current notice",
                )
            )


def _check_research_records(root: Path, findings: list[Finding]) -> None:
    """Validate immutable evidence and research records without reading prose."""
    evidence_store = EvidenceStore(root)
    records: dict[str, dict[str, dict[str, Any]]] = {}
    for kind in STORE_KINDS:
        valid: dict[str, dict[str, Any]] = {}
        for identifier, record, problem in evidence_store.scan(kind):
            if record is None:
                findings.append(_error("evidence", _evidence_rel(kind, identifier), problem))
                continue
            valid[identifier] = record
        records[kind] = valid
    _check_references(records, findings)
    research_store = ResearchStore(root)
    research_records: dict[str, dict[str, dict[str, Any]]] = {}
    for kind in ("plans", "jobs", "packets", "outcomes", "candidates"):
        suffix = "jsonl" if kind == "jobs" else "json"
        research_valid: dict[str, dict[str, Any]] = {}
        for identifier, record, problem in research_store.scan(kind):
            rel = f"{MEGAMIND_DIR}/research/{kind}/{identifier}.{suffix}"
            if record is None:
                findings.append(_error("research", rel, problem))
                continue
            research_valid[identifier] = record
        research_records[kind] = research_valid
    for identifier, job in sorted(research_records["jobs"].items()):
        rel = f"{MEGAMIND_DIR}/research/jobs/{identifier}.jsonl"
        plan = research_records["plans"].get(str(job["plan_id"]))
        if plan is None:
            findings.append(_error("research", rel, f"job cites unknown plan {job['plan_id']}"))
        elif str(plan["gap_id"]) != str(job["gap_id"]):
            findings.append(_error("research", rel, "job and plan have different gap ids"))
    for identifier, packet in sorted(research_records["packets"].items()):
        rel = f"{MEGAMIND_DIR}/research/packets/{identifier}.json"
        for claim_id in packet["claim_ids"]:
            if claim_id not in records["claims"]:
                findings.append(_error("research", rel, f"packet cites unknown claim {claim_id}"))
        for contradiction_id in packet["contradiction_ids"]:
            if contradiction_id not in records["contradictions"]:
                findings.append(
                    _error(
                        "research", rel, f"packet cites unknown contradiction {contradiction_id}"
                    )
                )


def run_doctor(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        registry = load_registry(root)
    except RegistryError as error:
        # A canonical single-wiki root has a card instead of a registry.
        try:
            card = load_wiki_card(root)
        except CardError:
            return [_error("registry", f"{MEGAMIND_DIR}/registry.json", str(error))]
        if not isinstance(error, RegistryNotInitialized):
            # A readable card never excuses a registry that exists and is broken.
            findings.append(_error("registry", f"{MEGAMIND_DIR}/registry.json", str(error)))
        for required in ("raw", "wiki", f"{MEGAMIND_DIR}/proposals", f"{MEGAMIND_DIR}/audit"):
            if not (root / required).is_dir():
                findings.append(
                    _error("canonical-root", required, "required canonical directory is missing")
                )
        _check_gap_journal(root, findings)
        _check_research_records(root, findings)
        if not card.name:
            findings.append(
                _error("card", f"{MEGAMIND_DIR}/wiki-card.json", "card has no wiki name")
            )
        return findings

    _check_schema_version(registry, findings)
    _check_budgets(registry, findings)
    _check_wikis(root, registry, findings)
    _check_access_policy(registry, findings)
    _check_registration(root, registry, findings)
    _check_router(root, registry, findings)
    _check_pages(root, registry, findings)
    _check_proposals(root, findings)
    _check_symlinks(root, findings)
    _check_gap_journal(root, findings)
    _check_research_records(root, findings)
    findings.sort(key=lambda f: (f.severity != "error", f.check, f.path, f.message))
    return findings


def has_errors(findings: list[Finding]) -> bool:
    return any(finding.severity == "error" for finding in findings)
