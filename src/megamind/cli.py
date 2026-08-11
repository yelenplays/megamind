"""megamind-axi: the agent-facing executable boundary of Megamind.

Contract (see docs/axi.md):

- stdout carries exactly one typed document per invocation, TOON by default,
  ``--format json`` renders the same object; diagnostics go to stderr only.
- Every document has a stable ``schema_version`` and a contextual ``help[]``
  of executable next commands (suppress with ``--no-help-hints``).
- Empty, no-match, and no-op states are definitive structured successes.
- Exit codes: 0 success/no-op, 1 operational failure (including doctor
  errors), 2 usage/validation failure before any side effect.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

from . import __version__, toon
from .adopt import AdoptError, apply_adoption, plan_adoption, rollback_adoption
from .capture import CaptureError, capture, list_proposals
from .card import CARD_PATH, CardError, load_wiki_card
from .catalog import (
    RootRef,
    build_catalog,
    check_projection,
    discover_roots,
    render_projection,
    visible_rows,
)
from .confidence import (
    LIFECYCLE_CAP,
    RELIANCE_FLOOR,
    SOURCE_QUALITIES,
    Confidence,
    Source,
    answer_confidence,
    claim_confidence,
)
from .doctor import run_doctor
from .evaluation import (
    EvaluationError,
    check_benchmark,
    evaluation_roots,
    guard_output_path,
    load_plan,
    load_unblinding_map,
    plan_experiment,
    read_blinding_key,
    read_json,
    record_evaluation,
    run_benchmark,
    score_sealed,
    seal_experiment,
    validate_outputs,
    write_blinding_key,
    write_document,
)
from .evolve import (
    EvolveError,
    apply_plan,
    evolution_tree_hashes,
    plan,
    resume_evolution,
    rollback_evolution,
)
from .fsops import PathEscapeError
from .gardening import (
    RESULT_SCHEMA,
    WAVE_SCHEMA,
    CapacityInput,
    GapRecord,
    GapStore,
    GardenError,
    PriorityInputs,
    ProvisionCriteria,
    apply_provision_plan,
    ingest_research_result,
    make_nomination,
    plan_provision_wiki,
    plan_research_wave,
    resume_provision,
    rollback_provision,
)
from .models import FrontmatterError, parse_document
from .preflight import MODEL_CLASSES, run_preflight
from .registry import (
    CURRENT_VERSION,
    REGISTRY_PATH,
    Budgets,
    Registry,
    RegistryError,
    RegistryNotInitialized,
    load_registry,
    migrate_registry,
)
from .review import ReviewReport, review
from .routing import RouteResult, route
from .scaffold import InitError, init_vault, init_wiki_root
from .semantic import NgramBackend, SemanticBackend
from .skillpack import skill_files, write_skill

EXECUTABLE = "megamind-axi"
PURPOSE = (
    "Deterministic knowledge gardener for Markdown wikis: route questions to "
    "the smallest useful context, capture proposals, evolve pages with approval."
)

ROUTE_FIELDS_DEFAULT = ["path", "kind", "score", "reason"]
ROUTE_FIELDS_ALL = [
    "path",
    "kind",
    "score",
    "wiki",
    "privacy",
    "chars",
    "confidence",
    "freshness",
    "semantic_score",
    "provisional",
    "reason",
    "reasons",
]
DIFF_LINE_LIMIT = 60
SECTION_ITEM_LIMIT = 20
FINDINGS_LIMIT = 50

Doc = dict[str, Any]


class UsageError(Exception):
    """Invalid flags/arguments; reported as a typed error document, exit 2."""


class AxiParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


def _help(*entries: str) -> list[str]:
    return list(entries)


def _resolve(value: Any, fallback: Any) -> Any:
    return fallback if value is None else value


# ---------------------------------------------------------------------------
# Command handlers: each returns (document, exit_code)
# ---------------------------------------------------------------------------


def _proposal_aggregates(root: Path) -> Doc:
    counts = {"open": 0, "applied": 0, "rejected": 0}
    for proposal_file in list_proposals(root):
        try:
            document = parse_document(proposal_file.read_text(encoding="utf-8"))
        except ValueError:
            continue
        status = str(document.frontmatter.get("status", ""))
        if status == "proposed":
            counts["open"] += 1
        elif status in counts:
            counts[status] += 1
    return counts


def _review_aggregates(report: ReviewReport) -> Doc:
    data = report.to_dict()
    return {key: len(items) for key, items in data.items()}  # type: ignore[arg-type]


def cmd_home(root: Path, root_label: str, today: date | None) -> tuple[Doc, int]:
    doc: Doc = {
        "schema_version": "megamind/home/v1",
        "executable": EXECUTABLE,
        "version": __version__,
        "purpose": PURPOSE,
        "root": root_label,
    }
    try:
        registry = load_registry(root)
    except RegistryError:
        doc["initialized"] = False
        doc["help"] = _help(
            f"Run `{EXECUTABLE} init <root>` to initialize a vault (non-destructive)",
            f"Run `{EXECUTABLE} --root <path>` to point at an existing vault",
        )
        return doc, 0
    doc["initialized"] = True
    wikis = []
    for wiki in registry.wikis:
        wiki_dir = root / wiki.path
        pages = len(list(wiki_dir.rglob("*.md"))) if wiki_dir.is_dir() else 0
        wikis.append({"name": wiki.name, "privacy": wiki.privacy, "pages": pages})
    doc["wikis"] = wikis
    doc["proposals"] = _proposal_aggregates(root)
    report = review(root, registry, today=today)
    doc["review"] = _review_aggregates(report)
    findings = run_doctor(root)
    doctor_counts = {
        "errors": sum(1 for f in findings if f.severity == "error"),
        "warnings": sum(1 for f in findings if f.severity == "warning"),
    }
    doc["doctor"] = doctor_counts
    steps: list[str] = []
    if doctor_counts["errors"]:
        steps.append(f"Run `{EXECUTABLE} doctor` to see vault integrity errors")
    if doc["proposals"]["open"]:
        steps.append(f"Run `{EXECUTABLE} review` to triage open proposals")
    steps.append(f'Run `{EXECUTABLE} route "<question>"` to find where knowledge lives')
    if not doc["proposals"]["open"]:
        steps.append(f'Run `{EXECUTABLE} capture --text "<note>" --type fact` to propose knowledge')
    doc["help"] = steps[:3]
    return doc, 0


def cmd_init(target: str, starter: bool, wiki_name: str | None) -> tuple[Doc, int]:
    if wiki_name is not None:
        result = init_wiki_root(Path(target), wiki_name)
        doc: Doc = {
            "schema_version": "megamind/init-result/v1",
            "status": "already_initialized" if result.already_initialized else "initialized",
            "layout": "canonical-wiki",
            "root": target,
            "created": result.created,
            "skipped": result.skipped,
            "help": _help(
                f"Edit `{target}/.megamind/wiki-card.json` to declare scope, owners, and access",
                f"Run `{EXECUTABLE} catalog --estate <dir>` to see this wiki in a fleet catalog",
            ),
        }
        return doc, 0
    result = init_vault(Path(target), starter=starter)
    doc = {
        "schema_version": "megamind/init-result/v1",
        "status": "already_initialized" if result.already_initialized else "initialized",
        "layout": "vault",
        "root": target,
        "created": result.created,
        "skipped": result.skipped,
        "help": _help(
            f"Run `{EXECUTABLE} --root {shlex.quote(target)}` for the vault home view",
            f"Run `{EXECUTABLE} --root {shlex.quote(target)} doctor` to validate the vault",
        ),
    }
    return doc, 0


def _load_routing_registry(root: Path) -> Registry:
    """Load either root shape into the registry-shaped routing interface."""
    try:
        return load_registry(root)
    except RegistryNotInitialized:
        if not (root / CARD_PATH).is_file():
            raise
        card = load_wiki_card(root)
        defaults = Budgets()
        budgets = Budgets(
            max_candidates=card.context_budget.max_candidates or defaults.max_candidates,
            max_context_chars=card.context_budget.max_context_chars or defaults.max_context_chars,
        )
        return Registry(version=CURRENT_VERSION, budgets=budgets, wikis=[card])


def _resolve_roots(estate: str | None, root: Path, root_label: str) -> list[RootRef]:
    if estate is not None:
        estate_path = Path(estate)
        if not estate_path.is_dir():
            raise UsageError(f"estate is not a directory: {estate}")
        return discover_roots(estate_path)
    return [RootRef(label=root_label, path=root)]


def _catalog_counts(rows: list[Doc]) -> Doc:
    counts: Doc = {"ok": 0, "broken": 0, "unreachable": 0, "redacted": 0, "stale": 0}
    for row in rows:
        status = str(row.get("status", "ok"))
        if status == "ok":
            counts["ok"] += 1
        elif status in counts:
            counts[status] += 1
        if row.get("stale"):
            counts["stale"] += 1
    return counts


def cmd_catalog(
    estate: str | None,
    root: Path,
    root_label: str,
    today: date | None,
    full: bool,
    emit_projection: bool,
    check_path: str | None,
) -> tuple[Doc, int]:
    refs = _resolve_roots(estate, root, root_label)
    catalog = build_catalog(refs, today=today)
    rows = visible_rows(catalog)
    notes = list(catalog.notes)
    shown = _capped(rows, full, notes, "wikis")
    doc: Doc = {
        "schema_version": "megamind/catalog/v1",
        "roots": [ref.label for ref in refs],
        "total": len(rows),
        "counts": _catalog_counts(rows),
        "catalog_hash": catalog.catalog_hash,
        "wikis": shown,
        "notes": notes,
    }
    exit_code = 0
    if emit_projection:
        doc["projection"] = render_projection(catalog)
    if check_path is not None:
        status = check_projection(catalog, Path(check_path))
        doc["projection_check"] = {"path": check_path, "status": status}
        if status != "current":
            exit_code = 1
            notes.append(
                f"projection {status}: regenerate it from the cards so they cannot diverge"
            )
    steps = [
        f'Run `{EXECUTABLE} preflight "<request>" --model-class local --estate <dir>` '
        "to route a request across these wikis",
        f"Run `{EXECUTABLE} catalog --estate <dir> --emit-projection` for the "
        "human-readable projection",
    ]
    doc["help"] = steps
    return doc, exit_code


def cmd_preflight(
    request: str,
    model_class: str,
    estate: str | None,
    root: Path,
    root_label: str,
    today: date | None,
    full: bool,
    semantic: SemanticBackend | None = None,
) -> tuple[Doc, int]:
    refs = _resolve_roots(estate, root, root_label)
    catalog = build_catalog(refs, today=today)
    result = run_preflight(refs, request, model_class, catalog, semantic=semantic)
    notes = list(result.notes)
    doc: Doc = {
        "schema_version": "megamind/preflight-result/v2",
        "request": result.request,
        "request_hash": result.request_hash,
        "model_class": result.model_class,
        "status": result.status,
        "confidence": result.confidence,
        "thresholds": result.thresholds,
        "semantic": result.semantic,
        "preflight_id": result.preflight_id,
        "catalog_hash": result.catalog_hash,
        "matches": _capped(result.matches, full, notes, "matches"),
        "offers": _capped(result.offers, full, notes, "offers"),
        "filtered": _capped(result.filtered, full, notes, "filtered"),
        "declined": _capped(result.declined, full, notes, "declined"),
        "root_issues": _capped(result.root_issues, full, notes, "root_issues"),
        "redacted_count": result.redacted_count,
        "notes": notes,
    }
    if result.status == "matched" and result.matches:
        doc["help"] = _help(
            str(result.matches[0]["follow_up"]),
            "Record the preflight_id with the task as proof that preflight ran",
        )
    elif result.status == "ambiguous":
        doc["help"] = _help(
            "Offer the listed wikis as choices; load nothing until the host picks one"
        )
    elif result.status == "privacy-filtered":
        doc["help"] = _help(
            "Do not load these wikis for this model class; say the wiki coverage is unavailable"
        )
    else:
        doc["help"] = _help(
            "Stay quiet about wikis on a no-match; answer without wiki context",
            f'Run `{EXECUTABLE} capture --text "<what you learn>" --type fact` afterwards',
        )
    return doc, 0


def cmd_adopt(
    target: str,
    name: str | None,
    apply: bool,
    plan_id: str | None,
    rollback: bool,
    full: bool,
) -> tuple[Doc, int]:
    target_path = Path(target)
    if rollback:
        removed, kept = rollback_adoption(target_path)
        doc: Doc = {
            "schema_version": "megamind/adopt-result/v1",
            "status": "rolled_back",
            "target": target,
            "removed": removed,
            "kept": kept,
            "notes": ["rollback removes only generated adoption material"],
            "help": _help(f"Run `{EXECUTABLE} catalog --estate <dir>` to confirm the fleet view"),
        }
        return doc, 0
    computed = plan_adoption(target_path, name=name)
    if apply:
        created = apply_adoption(computed, approved_plan_id=plan_id or "")
        doc = {
            "schema_version": "megamind/adopt-result/v1",
            "status": "applied" if created else "noop",
            "target": target,
            "wiki": computed.wiki_name,
            "plan_id": computed.plan_id,
            "created": created,
            "notes": computed.notes,
            "help": _help(
                f"Edit `{target}/.megamind/wiki-card.json` to declare scope, owners, and access",
                f"Run `{EXECUTABLE} catalog --estate <dir>` to see the adopted wiki",
            ),
        }
        return doc, 0
    files = [change.path for change in computed.changes]
    notes = list(computed.notes)
    shown = _capped(files, full, notes, "files")
    doc = {
        "schema_version": "megamind/adopt-plan/v1",
        "status": computed.status,
        "target": target,
        "wiki": computed.wiki_name,
        "plan_id": computed.plan_id,
        "files": shown,
        "directories": [rel + "/" for rel in computed.directories],
        "notes": notes,
    }
    steps: list[str] = []
    if computed.status != "noop":
        steps.append(
            f"Run `{EXECUTABLE} adopt {shlex.quote(target)} "
            f"--apply --plan-id {shlex.quote(computed.plan_id)}` "
            "after human review of this plan"
        )
    steps.append(
        "Adoption never modifies existing pages; rollback with "
        f"`{EXECUTABLE} adopt {shlex.quote(target)} --rollback`"
    )
    doc["help"] = steps
    return doc, 0


def cmd_migrate(root: Path) -> tuple[Doc, int]:
    registry, changed, notes = migrate_registry(root)
    doc: Doc = {
        "schema_version": "megamind/migrate-result/v1",
        "status": "migrated" if changed else "already_current",
        "version": registry.version,
        "wikis": len(registry.wikis),
        "notes": notes,
        "help": _help(
            f"Run `{EXECUTABLE} doctor` to validate the migrated registry",
            f"Run `{EXECUTABLE} config show` to review the wiki entries",
        ),
    }
    return doc, 0


def _route_row(candidate: Doc, fields: list[str]) -> Doc:
    reasons = list(candidate["reasons"])
    values: Doc = {
        "path": candidate["path"],
        "kind": candidate["kind"],
        "score": candidate["score"],
        "wiki": candidate["wiki"],
        "privacy": candidate["privacy"],
        "chars": candidate["chars"],
        "confidence": candidate["confidence"],
        "freshness": candidate["freshness"],
        "semantic_score": candidate["semantic_score"],
        "provisional": candidate["provisional"],
        "reason": reasons[0] if reasons else "",
        "reasons": " | ".join(reasons),
    }
    return {field: values[field] for field in fields}


def cmd_route(
    root: Path,
    registry: Registry,
    query: str,
    fields: list[str],
    today: date | None = None,
    semantic: SemanticBackend | None = None,
) -> tuple[Doc, int]:
    result: RouteResult = route(root, registry, query, today=today, semantic=semantic)
    doc: Doc = {
        "schema_version": "megamind/route-result/v2",
        "query": query,
        "matched": result.matched,
        "decision": result.decision,
        "confidence": result.confidence,
        "thresholds": result.thresholds,
        "semantic": result.semantic,
        "candidates": [_route_row(asdict(c), fields) for c in result.candidates],
        "governance": result.governance,
        # Typed beside the sidecar so a consumer never has to string-match the
        # prose in `notes` to tell a governance offer from a confidence one.
        "governance_downgrade": result.governance_downgrade,
        "context_chars": result.context_chars,
        "max_context_chars": result.max_context_chars,
        "max_candidates": result.max_candidates,
        "notes": result.notes,
    }
    provisional_offered = any(row["provisional"] for row in result.governance)
    if result.decision == "load" and result.candidates:
        best = result.candidates[0]
        doc["help"] = _help(
            f"Open `{best.path}` first; it scored highest",
            f"Run `{EXECUTABLE} route {shlex.quote(query)} "
            "--fields path,kind,score,confidence,reasons` for detail",
        )
    elif result.decision == "offer":
        entries = ["Offer the listed candidates as choices; load nothing until one is picked"]
        if result.governance_downgrade:
            entries.append(
                "Every candidate that cleared the reliance floor is provisional: provisional "
                "wikis stay offers until confidence coverage and a later evaluation pass"
            )
        else:
            entries.append(
                f"Route confidence stays below the {RELIANCE_FLOOR} reliance floor or "
                "inside the ambiguity band"
            )
            if provisional_offered:
                entries.append(
                    "Some offered candidates are also provisional; see `governance` for which"
                )
        doc["help"] = _help(*entries)
    else:
        doc["help"] = _help(
            f"Run `{EXECUTABLE} config show` to see registered wikis and their keywords",
            f'Run `{EXECUTABLE} capture --text "<answer>" --type fact` once you learn the answer',
        )
    return doc, 0


def cmd_capture(
    root: Path,
    registry: Registry,
    text: str,
    source: str,
    knowledge_type: str,
    today: date | None,
) -> tuple[Doc, int]:
    result = capture(
        root, registry, text, source=source, knowledge_type=knowledge_type, today=today
    )
    doc: Doc = {
        "schema_version": "megamind/capture-result/v1",
        "status": "captured" if result.created else "duplicate",
        "proposal_id": result.proposal_id,
        "path": result.path,
        "type": knowledge_type,
        "destination": result.suggested_destination,
        "reasons": result.route_reasons[:3],
        "help": _help(
            f"Run `{EXECUTABLE} evolve {shlex.quote(result.proposal_id)}` "
            "to plan applying it (dry run)",
            f"Run `{EXECUTABLE} review` to see all open proposals",
        ),
    }
    return doc, 0


def cmd_evolve(
    root: Path,
    registry: Registry,
    proposal: str,
    destination: str | None,
    supersedes: str | None,
    apply: bool,
    rollback: bool,
    plan_id: str | None,
    approve_new_wiki: bool,
    full: bool,
    today: date | None,
) -> tuple[Doc, int]:
    if rollback:
        outcome = rollback_evolution(root, plan_id or "", proposal)
        rollback_doc: Doc = {
            "schema_version": "megamind/evolve-result/v1",
            **outcome,
            "help": _help(
                f"Run `{EXECUTABLE} doctor` to validate the restored vault",
                f"Run `{EXECUTABLE} review` to inspect the retained proposal and audit trail",
            ),
        }
        return rollback_doc, 0
    if apply:
        resumed = resume_evolution(root, plan_id or "", proposal)
        if resumed is not None:
            return {
                "schema_version": "megamind/evolve-result/v1",
                **resumed,
                "notes": ["recovered from the durable evolution transaction"],
                "help": _help(
                    f"Run `{EXECUTABLE} doctor` to validate the vault after recovery",
                    f"Run `{EXECUTABLE} review` to see remaining open proposals",
                ),
            }, 0
    computed = plan(root, registry, proposal, destination=destination, supersedes=supersedes)
    if apply:
        applied = apply_plan(
            root,
            registry,
            computed,
            approved_plan_id=plan_id or "",
            approve_new_wiki=approve_new_wiki,
            today=today,
        )
        doc: Doc = {
            "schema_version": "megamind/evolve-result/v1",
            "status": "applied" if applied else "noop",
            "action": computed.action,
            "proposal_id": computed.proposal_id,
            "destination": computed.destination,
            "plan_id": computed.plan_id,
            "applied": applied,
            **evolution_tree_hashes(root, computed.plan_id),
            "notes": computed.notes,
            "help": _help(
                f"Run `{EXECUTABLE} doctor` to validate the vault after the change",
                f"Run `{EXECUTABLE} review` to see remaining open proposals",
            ),
        }
        return doc, 0
    diff_lines = computed.render_diff().splitlines()
    truncated = not full and len(diff_lines) > DIFF_LINE_LIMIT
    shown = diff_lines[:DIFF_LINE_LIMIT] if truncated else diff_lines
    doc = {
        "schema_version": "megamind/evolve-plan/v1",
        "status": "planned" if computed.action != "noop" else "noop",
        "action": computed.action,
        "proposal_id": computed.proposal_id,
        "destination": computed.destination,
        "supersedes": computed.supersedes,
        "creates_new_wiki": computed.creates_new_wiki,
        "plan_id": computed.plan_id,
        "files_changed": len(computed.changes),
        "diff_lines_total": len(diff_lines),
        "diff_truncated": truncated,
        "diff": shown,
        "notes": computed.notes,
    }
    steps: list[str] = []
    if computed.action != "noop":
        apply_cmd = (
            f"{EXECUTABLE} evolve {shlex.quote(computed.proposal_id)} "
            f"--apply --plan-id {shlex.quote(computed.plan_id)}"
        )
        if destination:
            apply_cmd += f" --dest {shlex.quote(destination)}"
        if supersedes:
            apply_cmd += f" --supersedes {shlex.quote(supersedes)}"
        if computed.creates_new_wiki:
            apply_cmd += " --approve-new-wiki"
        steps.append(f"Run `{apply_cmd}` after human review of this diff")
    if truncated:
        steps.append(
            f"Run `{EXECUTABLE} evolve {shlex.quote(computed.proposal_id)} --full` "
            "for the whole diff"
        )
    if not steps:
        steps.append(f"Run `{EXECUTABLE} review` to see what still needs attention")
    doc["help"] = steps
    return doc, 0


def _capped(items: list[Any], full: bool, notes: list[str], label: str) -> list[Any]:
    if full or len(items) <= SECTION_ITEM_LIMIT:
        return items
    notes.append(f"{label} truncated to {SECTION_ITEM_LIMIT} of {len(items)}; re-run with --full")
    return items[:SECTION_ITEM_LIMIT]


def cmd_review(root: Path, registry: Registry, today: date | None, full: bool) -> tuple[Doc, int]:
    report = review(root, registry, today=today)
    aggregates = _review_aggregates(report)
    notes: list[str] = []
    doc: Doc = {
        "schema_version": "megamind/review-report/v1",
        "status": "clean" if report.is_clean() else "attention",
        "aggregates": aggregates,
    }
    sections: dict[str, list[Any]] = {
        "open_proposals": list(report.open_proposals),
        "uncategorized_proposals": list(report.uncategorized_proposals),
        "already_merged_proposals": list(report.already_merged_proposals),
        "duplicate_titles": [
            {"title": d["title"], "pages": "; ".join(d["pages"])}  # type: ignore[arg-type]
            for d in report.duplicate_titles
        ],
        "stale_pages": list(report.stale_pages),
        "superseded_still_linked": list(report.superseded_still_linked),
        "dead_links": list(report.dead_links),
        "promotion_candidates": list(report.promotion_candidates),
    }
    for key, items in sections.items():
        if items:
            doc[key] = _capped(items, full, notes, key)
    doc["notes"] = notes
    steps: list[str] = []
    if report.uncategorized_proposals:
        first = report.uncategorized_proposals[0].rsplit("/", 1)[-1].removesuffix(".md")
        steps.append(
            f"Run `{EXECUTABLE} evolve {shlex.quote(first)} --dest <WikiName>` "
            "to categorize a proposal"
        )
    elif report.open_proposals:
        first = report.open_proposals[0].rsplit("/", 1)[-1].removesuffix(".md")
        steps.append(f"Run `{EXECUTABLE} evolve {shlex.quote(first)}` to plan applying a proposal")
    if report.dead_links or report.superseded_still_linked:
        steps.append(f"Run `{EXECUTABLE} doctor` for the full integrity picture")
    if not steps:
        steps.append(f'Run `{EXECUTABLE} capture --text "<note>" --type fact` to add knowledge')
    doc["help"] = steps
    return doc, 0


def cmd_doctor(root: Path, full: bool) -> tuple[Doc, int]:
    findings = run_doctor(root)
    errors = sum(1 for f in findings if f.severity == "error")
    warnings = len(findings) - errors
    status = "errors" if errors else ("warnings" if warnings else "healthy")
    notes: list[str] = []
    shown = [asdict(f) for f in findings]
    if not full and len(shown) > FINDINGS_LIMIT:
        notes.append(f"findings truncated to {FINDINGS_LIMIT} of {len(shown)}; re-run with --full")
        shown = shown[:FINDINGS_LIMIT]
    doc: Doc = {
        "schema_version": "megamind/doctor-report/v1",
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "findings": shown,
        "notes": notes,
    }
    if errors:
        doc["help"] = _help(
            "Fix the errors above; the path column names the offending file",
            f"Run `{EXECUTABLE} init .` to refresh the generated router when it is out of sync",
        )
    else:
        doc["help"] = _help(f"Run `{EXECUTABLE} review` to see gardening work beyond integrity")
    return doc, 1 if errors else 0


def _confidence_doc(kind: str, inputs: Doc, result: Confidence) -> tuple[Doc, int]:
    doc: Doc = {
        "schema_version": "megamind/confidence-report/v1",
        "kind": kind,
        "score": result.render(),
        "meets_floor": result.meets_floor,
        "reliance_floor": RELIANCE_FLOOR,
        "input": inputs,
        "components": result.components,
    }
    if result.meets_floor:
        doc["help"] = _help(
            f"This {kind} reaches the {RELIANCE_FLOOR} reliance floor: it may be relied upon"
        )
    elif result.known:
        doc["help"] = _help(
            f"This {kind} stays below the {RELIANCE_FLOOR} reliance floor: keep it as raw "
            "material, hypothesis, or proposal until stronger evidence lifts it"
        )
    else:
        doc["help"] = _help(
            "Confidence is unknown: there is no evidence to score, and unknown is "
            "never fabricated into a number"
        )
    return doc, 0


def _parse_source_spec(spec: str, eligible: bool) -> Source:
    quality, separator, origin = spec.partition(":")
    if not separator or not origin.strip() or not quality.strip():
        raise UsageError(f"source must be <quality>:<origin>, got: {spec!r}")
    quality = quality.strip()
    if quality not in SOURCE_QUALITIES:
        raise UsageError(
            f"unknown source quality: {quality} (expected one of {', '.join(SOURCE_QUALITIES)})"
        )
    return Source(quality=quality, origin=origin.strip(), eligible=eligible)


def cmd_assess_claim(
    sources: list[str],
    ineligible_sources: list[str],
    lifecycle: str,
    freshness: str,
    contradicted: bool,
) -> tuple[Doc, int]:
    parsed = [_parse_source_spec(spec, True) for spec in sources]
    parsed += [_parse_source_spec(spec, False) for spec in ineligible_sources]
    result = claim_confidence(
        parsed,
        lifecycle="" if lifecycle == "unknown" else lifecycle,
        freshness=freshness,
        contradicted=contradicted,
    )
    inputs: Doc = {
        "sources": sources,
        "ineligible_sources": ineligible_sources,
        "lifecycle": lifecycle,
        "freshness": freshness,
        "contradicted": contradicted,
    }
    return _confidence_doc("claim", inputs, result)


def cmd_assess_answer(claim_values: list[str]) -> tuple[Doc, int]:
    claims: list[Confidence] = []
    for raw in claim_values:
        if raw == "unknown":
            claims.append(Confidence(score=None))
            continue
        try:
            score = float(raw)
        except ValueError:
            raise UsageError(
                f"--claim must be a number in [0, 1] or 'unknown', got: {raw!r}"
            ) from None
        if not 0.0 <= score <= 1.0:
            raise UsageError(f"--claim must be a number in [0, 1] or 'unknown', got: {raw!r}")
        claims.append(Confidence(score=round(score, 4)))
    result = answer_confidence(claims)
    return _confidence_doc("answer", {"claims": claim_values}, result)


def cmd_config_show(root: Path, root_label: str) -> tuple[Doc, int]:
    registry = load_registry(root)
    doc: Doc = {
        "schema_version": "megamind/config/v1",
        "root": root_label,
        "registry": REGISTRY_PATH.as_posix(),
        "budgets": asdict(registry.budgets),
        "wikis": [{"name": w.name, "path": w.path, "privacy": w.privacy} for w in registry.wikis],
        "help": _help(
            f"Edit `{REGISTRY_PATH.as_posix()}` to register wikis, then run `{EXECUTABLE} init .`",
            f"Run `{EXECUTABLE} doctor` to validate the registry after edits",
        ),
    }
    return doc, 0


def _gap_row(record: GapRecord, full: bool) -> Doc:
    """List rows carry the attempt count; the history itself needs `--full`."""
    data = record.to_data()
    attempts = data.pop("attempts")
    row: Doc = {**data, "attempt_count": len(attempts)}
    if full:
        row["attempts"] = attempts
    return row


def cmd_gap(args: argparse.Namespace, root: Path, today: str) -> tuple[Doc, int]:
    store = GapStore(root)
    cooldown = _garden_cooldown(args)
    if args.gap_action == "list":
        notes: list[str] = []
        records = store.records()
        shown = _capped(list(records), args.full, notes, "gaps")
        return {
            "schema_version": "megamind/gaps-result/v1",
            "status": "ok",
            "gaps": [_gap_row(record, args.full) for record in shown],
            "count": len(shown),
            "total": len(records),
            "notes": notes,
            "help": _help(
                "Use `megamind-axi gap transition <id> --status planned` to update a gap",
                f"Run `{EXECUTABLE} gap list --full` for every gap and its attempt history",
            ),
        }, 0
    if args.gap_action == "create":
        priority = PriorityInputs(
            args.impact, args.urgency, args.repeat_demand, args.coverage, args.confidence_risk
        )
        candidate = GapRecord.new(
            args.wiki,
            args.topic,
            args.kind,
            priority=priority,
            related_topics=args.related,
            today=today,
        )
        existing = next(
            (item for item in store.records() if item.identity == candidate.identity), None
        )
        record = store.create(candidate)
        return {
            "schema_version": "megamind/gap-result/v1",
            "status": "deduplicated" if existing else "created",
            "gap": record.to_data(),
            "help": _help(
                f"Run `{EXECUTABLE} research-wave {record.gap_id} "
                "--capacity-known` to plan a bounded wave"
            ),
        }, 0
    if args.gap_action == "transition":
        before = store.get(args.gap_id)
        record = store.transition(
            args.gap_id,
            args.status,
            today=today,
            reason=args.reason,
            cooldown_until=cooldown,
            superseded_by=args.superseded_by,
        )
        return {
            "schema_version": "megamind/gap-transition/v1",
            # An exact repeat appends nothing, so it reports the no-op rather
            # than claiming a lifecycle move that never happened.
            "status": "unchanged" if record == before else "transitioned",
            "gap": record.to_data(),
            "help": _help(f"Run `{EXECUTABLE} gap list` to inspect durable gap state"),
        }, 0
    record = store.attempt(
        args.gap_id,
        args.outcome,
        correlation_id=args.correlation_id,
        today=today,
        cooldown_until=cooldown,
    )
    return {
        "schema_version": "megamind/gap-attempt/v1",
        "status": "recorded",
        "gap": record.to_data(),
        "help": _help("Retry only after the recorded cooldown, or transition the gap to rejected"),
    }, 0


def _parse_active_wikis(values: list[str]) -> dict[str, int]:
    """Parse repeated ``--active-wiki WIKI=N`` facts into a typed mapping.

    Host-supplied capacity arrives as raw text, so every malformed form has to
    become a typed usage document rather than an exception out of the parser.
    """
    parsed: dict[str, int] = {}
    for item in values:
        wiki, separator, count = item.partition("=")
        if not separator or not wiki.strip():
            raise UsageError(f"--active-wiki expects WIKI=N, got: {item}")
        try:
            parsed[wiki.strip()] = int(count)
        except ValueError:
            raise UsageError(
                f"--active-wiki worker count must be an integer, got: {item}"
            ) from None
    return parsed


def _json_object(raw: str, flag: str) -> dict[str, Any]:
    """Decode a host-supplied bridge payload that must be a JSON object."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise UsageError(f"{flag} is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise UsageError(f"{flag} must be a JSON object")
    return value


def cmd_research_wave(args: argparse.Namespace, root: Path, today: str) -> tuple[Doc, int]:
    capacity = CapacityInput(
        args.capacity_known,
        args.active_workers,
        _parse_active_wikis(args.active_wiki),
        args.applicable_quota,
        args.reserve_quota,
        args.wave_units,
        args.available_units,
        args.captain_work,
    )
    result = plan_research_wave(GapStore(root).records(), args.gap_id, capacity, today=today)
    return {
        "schema_version": WAVE_SCHEMA,
        **result.to_data(),
        "help": _help(
            "Dispatch nothing from Megamind; the host may dispatch only the emitted nominations",
            "Replay each nomination with its stable correlation_id",
        ),
    }, 0


def cmd_research_result(args: argparse.Namespace, root: Path) -> tuple[Doc, int]:
    nomination_data = _json_object(args.nomination_json, "--nomination-json")
    result_data = _json_object(args.result_json, "--result-json")
    nomination = make_nomination(
        str(nomination_data.get("wave_id", "")),
        nomination_data,
        str(nomination_data.get("source_policy", "")),
    )
    result = ingest_research_result(root, nomination, result_data)
    return {
        "schema_version": RESULT_SCHEMA,
        **result.to_data(),
        "help": _help(
            "Review the immutable-source ingest proposal; Megamind does not "
            "fetch or publish the source"
        ),
    }, 0


# dest -> flag, in the order the help hint prints them, so the emitted apply
# command is deterministic and every criterion has one spelling.
PROVISION_CRITERIA_FLAGS: tuple[tuple[str, str], ...] = (
    ("domain", "--domain"),
    ("repeat_demand", "--repeat-demand"),
    ("multi_topic", "--multi-topic"),
    ("overlap", "--overlap"),
    ("scope", "--scope"),
    ("exclusions", "--exclusions"),
    ("owner", "--owner"),
    ("source_policy", "--source-policy"),
    ("privacy", "--privacy"),
    ("model_access", "--model-access"),
    ("seed_topic", "--seed-topic"),
    ("maintenance", "--maintenance"),
)


def _provision_criteria(args: argparse.Namespace) -> ProvisionCriteria:
    """Build the criteria a plan or an apply needs, refusing before any write."""
    missing = [flag for dest, flag in PROVISION_CRITERIA_FLAGS if getattr(args, dest, None) is None]
    if missing:
        raise UsageError(
            "provision-wiki needs every qualification criterion to plan or apply; missing: "
            + ", ".join(missing)
        )
    return ProvisionCriteria(
        args.domain,
        args.repeat_demand,
        args.multi_topic,
        args.overlap,
        args.scope,
        args.exclusions,
        args.owner,
        args.source_policy,
        args.privacy,
        args.model_access,
        tuple(args.seed_topic),
        args.maintenance,
    )


def _provision_command(args: argparse.Namespace, *tail: str, criteria: bool) -> str:
    """The exact command that re-runs this invocation with `tail` appended.

    An apply re-derives the plan from the criteria and from `--today`, so both
    have to travel with the approval token or the printed command would parse
    and then refuse as a plan-id mismatch. A rollback reads neither.
    """
    argv = [EXECUTABLE, "provision-wiki", shlex.quote(args.name), shlex.quote(args.path)]
    if criteria:
        for dest, flag in PROVISION_CRITERIA_FLAGS:
            value = getattr(args, dest)
            for item in value if isinstance(value, list) else [value]:
                argv += [flag, shlex.quote(str(item))]
        today = getattr(args, "today", None)
        if today:
            argv += ["--today", shlex.quote(str(today))]
    return " ".join([*argv, *tail])


def _applied_doc(name: str, path: str, plan_id: str, status: str, files: list[str]) -> Doc:
    return {
        "schema_version": "megamind/provisional-wiki-result/v1",
        "status": status,
        "wiki": name,
        "path": path,
        "plan_id": plan_id,
        "files": files,
        "trusted": False,
        "help": _help(
            "Populate and evaluate confidence coverage before treating this wiki "
            "as trusted knowledge",
            f"Run `{EXECUTABLE} provision-wiki {shlex.quote(name)} {shlex.quote(path)} "
            f"--rollback --plan-id {plan_id}` to undo it; a rollback needs no criteria flags",
        ),
    }


def cmd_provision_wiki(args: argparse.Namespace, root: Path, today: str) -> tuple[Doc, int]:
    if args.rollback:
        # The positionals are the identity the rollback is checked against, not
        # decoration: the plan id alone would let a pasted token undo a
        # different wiki under this name.
        outcome = rollback_provision(root, args.plan_id or "", args.name, args.path)
        entries = ["Re-run provision-wiki without --apply to inspect a fresh plan"]
        if outcome.preserved:
            entries.insert(
                0,
                "Content written after the apply was preserved, not deleted; review "
                "`preserved` and remove it yourself if it is no longer wanted",
            )
        return {
            "schema_version": "megamind/provisional-wiki-result/v1",
            "status": outcome.status,
            "wiki": outcome.wiki,
            "path": outcome.path,
            "plan_id": args.plan_id,
            "removed": outcome.removed,
            "preserved": outcome.preserved,
            "preserved_total": outcome.preserved_total,
            "notes": outcome.notes,
            "help": _help(*entries),
        }, 0
    # Every criterion is checked before a plan is computed and before an apply
    # can reach the transaction, so neither mode ever writes on partial input.
    criteria = _provision_criteria(args)
    if args.apply:
        if not args.plan_id:
            raise UsageError("provision-wiki --apply requires --plan-id from the dry run")
        # Replay reaches the verified no-op (and an interrupted apply reaches
        # recovery) from the transaction record alone, before planning can
        # refuse the wiki the first apply already registered.
        replay = resume_provision(root, args.plan_id, args.name, args.path)
        if replay is not None:
            status, files = replay
            return _applied_doc(args.name, args.path, args.plan_id, status, files), 0
    computed = plan_provision_wiki(root, args.name, args.path, criteria, today=today)
    if args.apply:
        applied = apply_provision_plan(root, computed, args.plan_id)
        return _applied_doc(
            args.name,
            args.path,
            computed.plan_id,
            "applied" if applied else "noop",
            applied,
        ), 0
    return {
        "schema_version": "megamind/provisional-wiki-result/v1",
        "status": "planned",
        "wiki": computed.name,
        "path": computed.path,
        "plan_id": computed.plan_id,
        "files": [change["path"] for change in computed.changes],
        "changes": list(computed.changes),
        "notes": list(computed.notes),
        "trusted": False,
        "help": _help(
            "Run `"
            + _provision_command(args, "--apply", "--plan-id", computed.plan_id, criteria=True)
            + "` after reviewing this local-only plan",
            "Run `"
            + _provision_command(args, "--rollback", "--plan-id", computed.plan_id, criteria=False)
            + "` to undo an applied plan; a rollback needs no criteria flags",
        ),
    }, 0


def cmd_setup_skill(dest: str | None) -> tuple[Doc, int]:
    files = [name for name, _ in skill_files()]
    if dest is None:
        plan_doc: Doc = {
            "schema_version": "megamind/setup-plan/v1",
            "status": "plan",
            "skill": "megamind",
            "files": files,
            "notes": [
                "setup writes only into the directory you pass; nothing else is touched",
                "uninstall by deleting <dest>/megamind; no other state is created",
            ],
            "help": _help(
                f"Run `{EXECUTABLE} setup skill --dest .claude/skills` for this project",
                f"Run `{EXECUTABLE} setup skill --dest $HOME/.claude/skills` for all projects",
            ),
        }
        return plan_doc, 0
    created, skipped = write_skill(Path(dest))
    doc: Doc = {
        "schema_version": "megamind/setup-result/v1",
        "status": "installed" if created else "already_installed",
        "dest": dest,
        "created": created,
        "skipped": skipped,
        "notes": [f"uninstall by deleting {dest}/megamind"],
        "help": _help(f"Run `{EXECUTABLE}` to confirm the executable resolves for the skill"),
    }
    return doc, 0


REPEAT_HASH_SEED = "524287"


def _guard_evaluation_outs(raw: Sequence[str | None], forbidden: Sequence[Path] = ()) -> None:
    """Refuse an unsafe destination before any work runs or any artifact lands.

    Checking every destination up front keeps a partially written artifact set
    and a recorded audit event out of the failure path.
    """
    for value in raw:
        if value:
            guard_output_path(Path(value), forbidden)


def _unsettled_guard_map(plan: Doc, map_path: Path, *, required: bool) -> Doc | None:
    """The map, read only to derive the roots an unsettled result must avoid.

    With nothing to write there is nothing to contain, so an unreadable map
    still yields the typed unsettled document. With a destination to protect,
    the map is the only artifact naming the arm snapshots, so a map that cannot
    be validated is refused rather than written around.
    """
    try:
        return load_unblinding_map(plan, map_path)
    except EvaluationError:
        if required:
            raise
        return None


def _evaluation_out(document: Doc, raw: str | None, forbidden: Sequence[Path] = ()) -> Doc:
    if raw:
        write_document(Path(raw), document, forbidden_roots=forbidden)
    return document


def cmd_bench(args: argparse.Namespace) -> tuple[Doc, int]:
    if args.bench_command == "run":
        fixtures = Path(args.fixtures)
        _guard_evaluation_outs([args.out], [fixtures])
        result = run_benchmark(fixtures, Path(args.queries), Path(args.thresholds))
        if args.repeat:
            # The measured pass inherits the caller's hash seed; the repeat forces
            # a different one, so an identical document proves the ladder itself
            # is seed-independent rather than merely pinned.
            repeated = run_benchmark(
                fixtures, Path(args.queries), Path(args.thresholds), hash_seed=REPEAT_HASH_SEED
            )
            if json.dumps(result, sort_keys=True) != json.dumps(repeated, sort_keys=True):
                raise EvaluationError("benchmark repeatability check failed")
        return _evaluation_out(result, args.out, [fixtures]), 0
    _guard_evaluation_outs([args.out])
    result = check_benchmark(Path(args.results), Path(args.thresholds))
    return _evaluation_out(result, args.out), 0 if result["status"] == "passed" else 1


def cmd_experiment(args: argparse.Namespace) -> tuple[Doc, int]:
    action = args.experiment_command
    if action == "keygen":
        return write_blinding_key(Path(args.out)), 0
    if action == "plan":
        arm_roots = [Path(args.no_wiki), Path(args.current_wiki), Path(args.updated_wiki)]
        evaluated = [*arm_roots, Path(args.output_root)]
        _guard_evaluation_outs([args.out, args.grader_out, args.map_out], evaluated)
        blinding_key = read_blinding_key(Path(args.blinding_key_file))
        result, grader, unblinding = plan_experiment(
            Path(args.tasks),
            arm_roots[0],
            arm_roots[1],
            arm_roots[2],
            Path(args.rubric),
            Path(args.thresholds),
            args.model,
            args.tools,
            args.effort,
            args.seed,
            Path(args.output_root),
            blinding_key,
        )
        # The grader packet and the unblinding map are separate artifacts on
        # purpose: whoever grades the arms must never hold the map.
        write_document(Path(args.grader_out), grader, forbidden_roots=evaluated)
        write_document(Path(args.map_out), unblinding, forbidden_roots=evaluated, mode=0o600)
        return _evaluation_out(result, args.out, evaluated), 0
    if action == "record":
        audit_root = Path(args.audit_root)
        _guard_evaluation_outs([args.out], [audit_root])
        score = read_json(Path(args.score), "evaluation score")
        if not isinstance(score, dict):
            raise EvaluationError("evaluation score must be an object")
        result = record_evaluation(audit_root, score)
        return _evaluation_out(result, args.out, [audit_root]), 0
    plan_path = Path(args.plan)
    outputs = [Path(path) for path in args.outputs]
    if action == "validate":
        plan = load_plan(plan_path)
        forbidden = evaluation_roots(plan)
        _guard_evaluation_outs([args.out], forbidden)
        result = validate_outputs(plan, outputs)
        return _evaluation_out(result, args.out, forbidden), 0 if result["status"] == "valid" else 1
    if action == "score":
        # Sealing runs first and touches no unblinding artifact. Only once the
        # blind grades are fixed is the map opened, the protected roots derived
        # from it, the destination checked, and the result unblinded and written.
        sealed = seal_experiment(plan_path, outputs)
        map_path = Path(args.unblinding_map)
        if sealed.unsettled is not None:
            # An incomplete set stays typed and blind, but still may not write
            # into an arm snapshot, and only the map names those roots.
            partial = _unsettled_guard_map(sealed.plan, map_path, required=bool(args.out))
            forbidden = evaluation_roots(sealed.plan, partial)
            _guard_evaluation_outs([args.out], forbidden)
            return _evaluation_out(sealed.unsettled, args.out, forbidden), 1
        unblinding = load_unblinding_map(sealed.plan, map_path)
        forbidden = evaluation_roots(sealed.plan, unblinding)
        _guard_evaluation_outs([args.out], forbidden)
        result = score_sealed(sealed, unblinding)
        return (
            _evaluation_out(result, args.out, forbidden),
            0 if result["status"] == "promoted" else 1,
        )
    raise UsageError("unknown experiment action")


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _common_flags(parser: argparse.ArgumentParser, top_level: bool = False) -> None:
    # Subcommand copies use SUPPRESS so they never clobber a value already
    # parsed from before the subcommand (argparse merges into one namespace).
    default: Any = None if top_level else argparse.SUPPRESS
    parser.add_argument(
        "--format", choices=["toon", "json"], default=default, help="output format (default: toon)"
    )
    parser.add_argument("--root", default=default, help="vault root (default: current directory)")
    parser.add_argument(
        "--no-help-hints",
        action="store_const",
        const=True,
        default=default,
        help="omit the help[] block from the document",
    )


def build_parser() -> AxiParser:
    parser = AxiParser(
        prog=EXECUTABLE,
        description=PURPOSE,
        epilog=(
            "examples:\n"
            f"  {EXECUTABLE}                          local home: wikis, proposals, doctor state\n"
            f'  {EXECUTABLE} route "release process"  bounded candidate paths plus reasons\n'
            f'  {EXECUTABLE} capture --text "..."     proposal draft, never a direct publish\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"{EXECUTABLE} {__version__}")
    _common_flags(parser, top_level=True)
    parser.add_argument(
        "--today", default=None, help="override today's date (ISO) for reproducible aggregates"
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser(
        "init",
        help="initialize a vault (non-destructive, never overwrites)",
        epilog=f"example: {EXECUTABLE} init my-vault",
    )
    _common_flags(p_init)
    p_init.add_argument("target", help="directory to initialize")
    p_init.add_argument("--no-starter", action="store_true", help="skip the synthetic starter wiki")
    p_init.add_argument(
        "--wiki",
        default=None,
        metavar="NAME",
        help="scaffold a canonical single-wiki root (Karpathy layout) instead of a vault",
    )

    p_migrate = sub.add_parser(
        "migrate",
        help="upgrade a v1 registry to schema v2 in place (backup + audit)",
        epilog=f"example: {EXECUTABLE} migrate",
    )
    _common_flags(p_migrate)

    p_catalog = sub.add_parser(
        "catalog",
        help="generated read-only fleet catalog over separate wiki roots",
        epilog=(
            f"examples:\n  {EXECUTABLE} catalog --estate ~/Wikis\n"
            f"  {EXECUTABLE} catalog --estate ~/Wikis --emit-projection\n"
            f"  {EXECUTABLE} catalog --estate ~/Wikis --check-projection ~/Wikis/CATALOG.md"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common_flags(p_catalog)
    p_catalog.add_argument("--estate", default=None, help="directory of wiki roots to aggregate")
    p_catalog.add_argument("--today", default=argparse.SUPPRESS, help="override today's date (ISO)")
    p_catalog.add_argument("--full", action="store_true", help="never truncate the wiki list")
    p_catalog.add_argument(
        "--emit-projection",
        action="store_true",
        help="include the byte-stable human-readable projection in the document",
    )
    p_catalog.add_argument(
        "--check-projection",
        default=None,
        metavar="PATH",
        help="drift-check a checked-in projection file against the cards (exit 1 on drift)",
    )

    p_preflight = sub.add_parser(
        "preflight",
        help="catalog-level route for a substantive request under a declared model class",
        epilog=(
            f'example: {EXECUTABLE} preflight "how do we price cleanup offers" '
            "--estate ~/Wikis --model-class cloud"
        ),
    )
    _common_flags(p_preflight)
    p_preflight.add_argument("request", nargs="+", help="privacy-safe request representation")
    p_preflight.add_argument(
        "--model-class",
        required=True,
        choices=list(MODEL_CLASSES),
        help="the host's declared model class",
    )
    p_preflight.add_argument("--estate", default=None, help="directory of wiki roots to consult")
    p_preflight.add_argument(
        "--today", default=argparse.SUPPRESS, help="override today's date (ISO)"
    )
    p_preflight.add_argument("--full", action="store_true", help="never truncate result lists")
    p_preflight.add_argument(
        "--semantic",
        action="store_true",
        help="rerank authorized matches with the local char-ngram backend (lexical stays default)",
    )

    p_adopt = sub.add_parser(
        "adopt",
        help="non-destructively adopt an existing wiki directory (dry-run first)",
        epilog=(
            f"examples:\n  {EXECUTABLE} adopt existing-wiki/\n"
            f"  {EXECUTABLE} adopt existing-wiki/ --apply --plan-id <plan-id>\n"
            f"  {EXECUTABLE} adopt existing-wiki/ --rollback"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common_flags(p_adopt)
    p_adopt.add_argument("target", help="existing wiki directory to adopt")
    p_adopt.add_argument("--name", default=None, help="wiki name (default: directory name)")
    p_adopt.add_argument("--apply", action="store_true", help="apply instead of dry run")
    p_adopt.add_argument("--plan-id", default=None, help="approval token from the dry run")
    p_adopt.add_argument(
        "--rollback",
        action="store_true",
        help="remove exactly what the latest adoption apply created",
    )
    p_adopt.add_argument("--full", action="store_true", help="never truncate the file list")

    p_route = sub.add_parser(
        "route",
        help="route a query to the smallest useful context",
        epilog=(
            f'example: {EXECUTABLE} route "how does pricing work" --fields path,kind,score,reasons'
        ),
    )
    _common_flags(p_route)
    p_route.add_argument("query", nargs="+", help="the question to route")
    p_route.add_argument(
        "--fields",
        default=None,
        help=f"candidate fields (default {','.join(ROUTE_FIELDS_DEFAULT)}; "
        f"available {','.join(ROUTE_FIELDS_ALL)})",
    )
    p_route.add_argument("--today", default=argparse.SUPPRESS, help="override today's date (ISO)")
    p_route.add_argument(
        "--semantic",
        action="store_true",
        help="rerank surfaced candidates with the local char-ngram backend (lexical stays default)",
    )

    p_capture = sub.add_parser(
        "capture",
        help="capture text as a reviewable proposal (never publishes)",
        epilog=f'example: {EXECUTABLE} capture --text "Pricing has two tiers." --type decision',
    )
    _common_flags(p_capture)
    source = p_capture.add_mutually_exclusive_group()
    source.add_argument("--text", help="capture this text")
    source.add_argument("--file", help="capture the contents of this file")
    p_capture.add_argument("--source", default="", help="provenance label")
    p_capture.add_argument(
        "--type",
        dest="knowledge_type",
        default="fact",
        help="fact|decision|hypothesis|procedure|example|guidance",
    )
    p_capture.add_argument("--today", default=argparse.SUPPRESS, help="override today's date (ISO)")

    p_evolve = sub.add_parser(
        "evolve",
        help="plan (default, dry-run) or apply an approved knowledge change",
        epilog=(
            f"examples:\n  {EXECUTABLE} evolve <proposal-id>\n"
            f"  {EXECUTABLE} evolve <proposal-id> --apply --plan-id <plan-id>\n"
            f"  {EXECUTABLE} evolve <proposal-id> --rollback --plan-id <plan-id>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common_flags(p_evolve)
    p_evolve.add_argument("proposal", help="proposal id or path")
    p_evolve.add_argument("--dest", default=None, help="destination page or wiki")
    p_evolve.add_argument("--supersedes", default=None, help="existing page the change supersedes")
    p_evolve.add_argument("--apply", action="store_true", help="apply instead of dry run")
    p_evolve.add_argument(
        "--rollback", action="store_true", help="restore an applied plan from its transaction"
    )
    p_evolve.add_argument("--plan-id", default=None, help="approval token from the dry run")
    p_evolve.add_argument(
        "--approve-new-wiki",
        action="store_true",
        help="explicitly approve creating a new top-level wiki",
    )
    p_evolve.add_argument("--full", action="store_true", help="never truncate the diff")
    p_evolve.add_argument("--today", default=argparse.SUPPRESS, help="override today's date (ISO)")

    p_review = sub.add_parser(
        "review",
        help="report proposals, duplicates, staleness, dead links, promotions",
        epilog=f"example: {EXECUTABLE} review --today 2026-06-01",
    )
    _common_flags(p_review)
    p_review.add_argument("--today", default=argparse.SUPPRESS, help="override today's date (ISO)")
    p_review.add_argument("--full", action="store_true", help="never truncate section lists")

    p_doctor = sub.add_parser(
        "doctor",
        help="validate registry, cards, links, privacy classes, budgets (exit 1 on errors)",
        epilog=f"example: {EXECUTABLE} doctor --format json",
    )
    _common_flags(p_doctor)
    p_doctor.add_argument("--full", action="store_true", help="never truncate findings")

    p_assess = sub.add_parser(
        "assess",
        help="deterministic claim/answer confidence against the 0.75 reliance floor",
        epilog=(
            f"examples:\n"
            f"  {EXECUTABLE} assess claim --source primary:release-notes "
            "--source primary:changelog --lifecycle active --freshness fresh\n"
            f"  {EXECUTABLE} assess answer --claim 0.9 --claim 0.6\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common_flags(p_assess)
    assess_sub = p_assess.add_subparsers(dest="assess_command")
    p_claim = assess_sub.add_parser(
        "claim", help="score one claim from its sources, lifecycle, freshness, contradictions"
    )
    _common_flags(p_claim)
    p_claim.add_argument(
        "--source",
        action="append",
        default=[],
        metavar="QUALITY:ORIGIN",
        help=f"eligible source ({'|'.join(SOURCE_QUALITIES)}); repeat per source",
    )
    p_claim.add_argument(
        "--ineligible-source",
        action="append",
        default=[],
        metavar="QUALITY:ORIGIN",
        help="source the consuming context may not use; it counts for nothing",
    )
    p_claim.add_argument(
        "--lifecycle",
        default="unknown",
        choices=[*sorted(LIFECYCLE_CAP), "unknown"],
        help="lifecycle state of the claim (default: unknown)",
    )
    p_claim.add_argument(
        "--freshness",
        default="unknown",
        choices=["fresh", "stale", "unknown"],
        help="freshness of the evidence (default: unknown)",
    )
    p_claim.add_argument(
        "--contradicted",
        action="store_true",
        help="the claim has an unresolved contradiction (freezes it below the floor)",
    )
    p_answer = assess_sub.add_parser(
        "answer", help="cap an answer at its weakest materially relied-upon claim"
    )
    _common_flags(p_answer)
    p_answer.add_argument(
        "--claim",
        action="append",
        default=[],
        metavar="SCORE|unknown",
        help="confidence of one relied-upon claim; repeat per claim",
    )

    p_config = sub.add_parser("config", help="inspect configuration")
    _common_flags(p_config)
    config_sub = p_config.add_subparsers(dest="config_command")
    p_config_show = config_sub.add_parser(
        "show", help="show registry path, budgets, and registered wikis"
    )
    _common_flags(p_config_show)

    p_gap = sub.add_parser("gap", aliases=["gaps"], help="durable governed knowledge gaps")
    _common_flags(p_gap)
    p_gap.add_argument("gap_action", choices=["list", "create", "transition", "attempt"])
    p_gap.add_argument("gap_id", nargs="?", default="")
    p_gap.add_argument("--wiki", default="")
    p_gap.add_argument("--topic", default="")
    p_gap.add_argument(
        "--kind", choices=["missing", "weak", "stale", "contradictory"], default="missing"
    )
    p_gap.add_argument(
        "--status",
        choices=[
            "open",
            "nominated",
            "planned",
            "in_progress",
            "paused",
            "resolved",
            "rejected",
            "superseded",
        ],
        default=None,
    )
    p_gap.add_argument("--reason", default="")
    p_gap.add_argument("--outcome", default="")
    p_gap.add_argument("--correlation-id", default="")
    # No default: an omitted cooldown keeps whatever backoff the record carries.
    p_gap.add_argument("--cooldown-until", default=None)
    p_gap.add_argument("--superseded-by", default="")
    p_gap.add_argument("--related", action="append", default=[])
    for name in ("impact", "urgency", "repeat-demand", "coverage", "confidence-risk"):
        p_gap.add_argument("--" + name, dest=name.replace("-", "_"), type=int, default=0)
    p_gap.add_argument("--today", default=argparse.SUPPRESS)
    p_gap.add_argument(
        "--full", action="store_true", help="never truncate the gap list or its attempt histories"
    )

    p_wave = sub.add_parser("research-wave", help="plan a deterministic one-hop research wave")
    _common_flags(p_wave)
    p_wave.add_argument("gap_id")
    p_wave.add_argument("--capacity-known", action="store_true")
    p_wave.add_argument("--active-workers", type=int, default=0)
    p_wave.add_argument("--active-wiki", action="append", default=[], metavar="WIKI=N")
    p_wave.add_argument("--applicable-quota", type=float, default=None)
    p_wave.add_argument("--reserve-quota", type=float, default=None)
    p_wave.add_argument("--wave-units", type=int, default=1)
    p_wave.add_argument("--available-units", type=int, default=None)
    p_wave.add_argument("--captain-work", action="store_true")
    p_wave.add_argument("--today", default=argparse.SUPPRESS)

    p_result = sub.add_parser("research-result", help="ingest a host research result as a proposal")
    _common_flags(p_result)
    p_result.add_argument("--nomination-json", required=True)
    p_result.add_argument("--result-json", required=True)

    p_provision = sub.add_parser("provision-wiki", help="create a qualified provisional local wiki")
    _common_flags(p_provision)
    p_provision.add_argument("name")
    p_provision.add_argument("path")
    # The qualification criteria are required by mode, not by the parser: a plan
    # and an apply both refuse without every one of them, while a rollback is
    # driven by `--plan-id` alone and must stay runnable exactly as help[] prints
    # it. `None` is what "not supplied" means here, so an explicitly empty value
    # still reaches the criteria check and fails as an unmet criterion.
    for flag, kind in (
        ("--domain", str),
        ("--repeat-demand", int),
        ("--multi-topic", int),
        ("--overlap", str),
        ("--scope", str),
        ("--exclusions", str),
        ("--owner", str),
        ("--source-policy", str),
        ("--privacy", str),
        ("--model-access", str),
        ("--maintenance", str),
    ):
        p_provision.add_argument(flag, type=kind, default=None)
    p_provision.add_argument("--seed-topic", action="append", default=None)
    p_provision.add_argument("--apply", action="store_true", help="apply the reviewed plan")
    p_provision.add_argument("--plan-id", default=None, help="approval token from the dry run")
    p_provision.add_argument("--rollback", action="store_true", help="rollback an applied plan")
    p_provision.add_argument("--today", default=argparse.SUPPRESS)

    p_bench = sub.add_parser(
        "bench",
        help="run or check the frozen synthetic release benchmark",
        epilog=(
            f"example: {EXECUTABLE} bench run --fixtures evals/fixtures/release-mini "
            "--queries evals/queries.jsonl --thresholds evals/thresholds.toml --out results.json"
        ),
    )
    _common_flags(p_bench)
    bench_sub = p_bench.add_subparsers(dest="bench_command")
    p_bench_run = bench_sub.add_parser(
        "run", help="invoke the real public CLI over synthetic fixtures"
    )
    _common_flags(p_bench_run)
    p_bench_run.add_argument("--fixtures", required=True)
    p_bench_run.add_argument("--queries", required=True)
    p_bench_run.add_argument("--thresholds", required=True)
    p_bench_run.add_argument("--out", default=None)
    p_bench_run.add_argument(
        "--repeat", action="store_true", help="rerun and require byte-identical canonical output"
    )
    p_bench_check = bench_sub.add_parser("check", help="check frozen release gates")
    _common_flags(p_bench_check)
    p_bench_check.add_argument("--results", required=True)
    p_bench_check.add_argument("--thresholds", required=True)
    p_bench_check.add_argument("--out", default=None)

    p_experiment = sub.add_parser(
        "experiment",
        aliases=["eval", "evaluation"],
        help="plan, validate, score, and record a host-executed three-arm evaluation",
    )
    _common_flags(p_experiment)
    experiment_sub = p_experiment.add_subparsers(dest="experiment_command")
    p_exp_keygen = experiment_sub.add_parser(
        "keygen",
        help="write a new private 256-bit blinding key file (mode 0600)",
        epilog=f"example: {EXECUTABLE} experiment keygen --out blinding.key",
    )
    _common_flags(p_exp_keygen)
    p_exp_keygen.add_argument("--out", required=True, help="destination for the new key file")
    p_exp_plan = experiment_sub.add_parser(
        "plan", help="freeze task, model, rubric, threshold, and arm inputs"
    )
    _common_flags(p_exp_plan)
    p_exp_plan.add_argument("--tasks", required=True)
    p_exp_plan.add_argument("--no-wiki", required=True)
    p_exp_plan.add_argument("--current-wiki", required=True)
    p_exp_plan.add_argument("--updated-wiki", required=True)
    p_exp_plan.add_argument("--rubric", required=True)
    p_exp_plan.add_argument("--thresholds", required=True)
    p_exp_plan.add_argument("--model", required=True)
    p_exp_plan.add_argument("--tools", default="none")
    p_exp_plan.add_argument("--effort", default="fixed")
    p_exp_plan.add_argument("--seed", type=int, required=True)
    p_exp_plan.add_argument("--output-root", required=True)
    p_exp_plan.add_argument("--out", required=True)
    p_exp_plan.add_argument(
        "--grader-out", required=True, help="blind grader packet: labels and rubric only"
    )
    p_exp_plan.add_argument(
        "--map-out", required=True, help="host-only unblinding map, kept apart from the grader"
    )
    p_exp_plan.add_argument(
        "--blinding-key-file",
        required=True,
        help="file holding the host's private blinding key (never copied into the plan)",
    )
    p_exp_validate = experiment_sub.add_parser(
        "validate", help="validate blinded, isolated host arm outputs"
    )
    _common_flags(p_exp_validate)
    p_exp_validate.add_argument("--plan", required=True)
    p_exp_validate.add_argument("--outputs", nargs="+", required=True)
    p_exp_validate.add_argument("--out", default=None)
    p_exp_score = experiment_sub.add_parser(
        "score", help="score validated outputs and apply frozen promotion gates"
    )
    _common_flags(p_exp_score)
    p_exp_score.add_argument("--plan", required=True)
    p_exp_score.add_argument("--outputs", nargs="+", required=True)
    p_exp_score.add_argument(
        "--unblinding-map",
        required=True,
        help="host execution artifact mapping blind labels to conditions",
    )
    p_exp_score.add_argument("--out", default=None)
    p_exp_record = experiment_sub.add_parser(
        "record", help="append a bounded safe evaluation audit event"
    )
    _common_flags(p_exp_record)
    p_exp_record.add_argument("--score", required=True)
    p_exp_record.add_argument("--audit-root", required=True)
    p_exp_record.add_argument("--out", default=None)

    p_setup = sub.add_parser("setup", help="explicit opt-in integrations (local, zero-network)")
    _common_flags(p_setup)
    setup_sub = p_setup.add_subparsers(dest="setup_command")
    p_setup_skill = setup_sub.add_parser(
        "skill",
        help="install the Agent Skill into a directory you choose",
        epilog=f"example: {EXECUTABLE} setup skill --dest .claude/skills",
    )
    _common_flags(p_setup_skill)
    p_setup_skill.add_argument(
        "--dest", default=None, help="skills directory to install into (omit for a plan)"
    )
    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _emit(doc: Doc, output_format: str, no_help_hints: bool) -> None:
    if no_help_hints:
        doc.pop("help", None)
    if output_format == "json":
        sys.stdout.write(json.dumps(doc, indent=2) + "\n")
    else:
        sys.stdout.write(toon.encode(doc))


def _error_doc(code: str, message: str, operation: str, help_entries: list[str]) -> Doc:
    return {
        "schema_version": "megamind/error/v1",
        "code": code,
        "message": message,
        "operation": operation,
        "help": help_entries,
    }


def _parse_iso(raw: str | None, flag: str) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise UsageError(f"{flag} must be an ISO date (YYYY-MM-DD): {raw}") from error


def _parse_today(raw: str | None) -> date | None:
    return _parse_iso(raw, "--today")


def _garden_today(args: argparse.Namespace) -> str:
    """The gardening surfaces persist dates, so they use the same ISO validator."""
    parsed = _parse_today(getattr(args, "today", None))
    return parsed.isoformat() if parsed else ""


def _garden_cooldown(args: argparse.Namespace) -> str | None:
    """An omitted cooldown stays None; an explicit one is a date like any other.

    The empty string is the documented way to clear a backoff, so it is passed
    through rather than parsed.
    """
    raw = args.cooldown_until
    if raw is None:
        return None
    if raw == "":
        return ""
    parsed = _parse_iso(str(raw), "--cooldown-until")
    return parsed.isoformat() if parsed else ""


def _dispatch(args: argparse.Namespace, root: Path, root_label: str) -> tuple[Doc, int]:
    command = args.command
    if command is None:
        return cmd_home(root, root_label, _parse_today(getattr(args, "today", None)))
    if command == "init":
        return cmd_init(args.target, starter=not args.no_starter, wiki_name=args.wiki)
    if command == "migrate":
        return cmd_migrate(root)
    if command == "catalog":
        return cmd_catalog(
            args.estate,
            root,
            root_label,
            today=_parse_today(getattr(args, "today", None)),
            full=args.full,
            emit_projection=args.emit_projection,
            check_path=args.check_projection,
        )
    if command == "preflight":
        return cmd_preflight(
            " ".join(args.request),
            args.model_class,
            args.estate,
            root,
            root_label,
            today=_parse_today(getattr(args, "today", None)),
            full=args.full,
            semantic=NgramBackend() if args.semantic else None,
        )
    if command == "adopt":
        if args.apply and args.rollback:
            raise UsageError("--apply and --rollback are mutually exclusive")
        if args.apply and not args.plan_id:
            raise UsageError("--apply requires --plan-id from the dry run")
        return cmd_adopt(
            args.target,
            name=args.name,
            apply=args.apply,
            plan_id=args.plan_id,
            rollback=args.rollback,
            full=args.full,
        )
    if command == "route":
        fields = ROUTE_FIELDS_DEFAULT
        if args.fields:
            fields = [f.strip() for f in args.fields.split(",") if f.strip()]
            unknown = [f for f in fields if f not in ROUTE_FIELDS_ALL]
            if unknown:
                raise UsageError(
                    f"unknown fields: {', '.join(unknown)} "
                    f"(available: {', '.join(ROUTE_FIELDS_ALL)})"
                )
        registry = _load_routing_registry(root)
        return cmd_route(
            root,
            registry,
            " ".join(args.query),
            fields,
            today=_parse_today(getattr(args, "today", None)),
            semantic=NgramBackend() if args.semantic else None,
        )
    if command == "capture":
        registry = _load_routing_registry(root)
        if args.text is not None:
            text, default_source = args.text, "inline"
        elif args.file is not None:
            text = Path(args.file).read_text(encoding="utf-8")
            default_source = f"file: {Path(args.file).name}"
        else:
            text, default_source = sys.stdin.read(), "stdin"
        return cmd_capture(
            root,
            registry,
            text,
            source=args.source or default_source,
            knowledge_type=args.knowledge_type,
            today=_parse_today(getattr(args, "today", None)),
        )
    if command == "evolve":
        if args.apply and args.rollback:
            raise UsageError("--apply and --rollback are mutually exclusive")
        if args.apply and not args.plan_id:
            raise UsageError("--apply requires --plan-id from the dry run")
        if args.rollback and not args.plan_id:
            raise UsageError("--rollback requires --plan-id from the applied plan")
        registry = _load_routing_registry(root)
        return cmd_evolve(
            root,
            registry,
            args.proposal,
            destination=args.dest,
            supersedes=args.supersedes,
            apply=args.apply,
            rollback=args.rollback,
            plan_id=args.plan_id,
            approve_new_wiki=args.approve_new_wiki,
            full=args.full,
            today=_parse_today(getattr(args, "today", None)),
        )
    if command == "review":
        registry = _load_routing_registry(root)
        return cmd_review(
            root, registry, today=_parse_today(getattr(args, "today", None)), full=args.full
        )
    if command == "doctor":
        return cmd_doctor(root, full=args.full)
    if command == "assess":
        sub_command = getattr(args, "assess_command", None)
        if sub_command == "claim":
            return cmd_assess_claim(
                args.source,
                args.ineligible_source,
                args.lifecycle,
                args.freshness,
                args.contradicted,
            )
        if sub_command == "answer":
            return cmd_assess_answer(args.claim)
        raise UsageError("usage: megamind-axi assess claim|answer ...")
    if command == "config":
        if getattr(args, "config_command", None) != "show":
            raise UsageError("usage: megamind-axi config show")
        return cmd_config_show(root, root_label)
    if command == "gap" or command == "gaps":
        if args.gap_action == "create" and (not args.wiki or not args.topic):
            raise UsageError("gap create requires --wiki and --topic")
        if args.gap_action in {"transition", "attempt"} and not args.gap_id:
            raise UsageError(f"gap {args.gap_action} requires GAP_ID")
        # A lifecycle mutation is never implied: an omitted --status would
        # otherwise silently reopen a resolved or rejected gap.
        if args.gap_action == "transition" and not args.status:
            raise UsageError("gap transition requires --status")
        return cmd_gap(args, root, _garden_today(args))
    if command == "research-wave":
        return cmd_research_wave(args, root, _garden_today(args))
    if command == "research-result":
        return cmd_research_result(args, root)
    if command == "provision-wiki":
        if args.apply and args.rollback:
            raise UsageError("provision-wiki --apply and --rollback are mutually exclusive")
        if args.rollback and not args.plan_id:
            raise UsageError("provision-wiki --rollback requires --plan-id")
        return cmd_provision_wiki(args, root, _garden_today(args))
    if command == "bench":
        if getattr(args, "bench_command", None) not in {"run", "check"}:
            raise UsageError("usage: megamind-axi bench run|check ...")
        return cmd_bench(args)
    if command in {"experiment", "eval", "evaluation"}:
        if getattr(args, "experiment_command", None) not in {
            "keygen",
            "plan",
            "validate",
            "score",
            "record",
        }:
            raise UsageError("usage: megamind-axi experiment keygen|plan|validate|score|record ...")
        return cmd_experiment(args)
    if command == "setup":
        if getattr(args, "setup_command", None) != "skill":
            raise UsageError("usage: megamind-axi setup skill [--dest DIR]")
        return cmd_setup_skill(args.dest)
    raise UsageError(f"unknown command: {command}")


_ERROR_HELP: dict[str, list[str]] = {
    "not_initialized": [
        f"Run `{EXECUTABLE} init <root>` to initialize a vault",
        f"Run `{EXECUTABLE} --root <path>` if the vault lives elsewhere",
    ],
    "plan_mismatch": [f"Run `{EXECUTABLE} evolve <proposal-id>` again and review the fresh diff"],
    "approval_required": ["Re-run with `--approve-new-wiki` only after explicit human approval"],
    "proposal_not_found": [f"Run `{EXECUTABLE} review` to list existing proposals"],
    "init_invalid": [
        f"Run `{EXECUTABLE} init <path>` for a registry vault",
        f"Run `{EXECUTABLE} init <path> --wiki <Name>` for a canonical single-wiki root",
    ],
    "capture_invalid": [
        f'Run `{EXECUTABLE} capture --text "<content>" --type '
        "fact|decision|hypothesis|procedure|example|guidance`"
    ],
    "frontmatter_invalid": [
        f"Run `{EXECUTABLE} doctor` to locate the file with unsupported frontmatter",
        "Fix the frontmatter block by hand; Megamind parses a small YAML subset",
    ],
    "garden_invalid": [
        f"Run `{EXECUTABLE} doctor` to validate governed records",
        "Check the typed gap, capacity, or bridge fields and retry",
    ],
    "evaluation_invalid": [
        "Check the frozen fixture, query set, task set, rubric, and threshold digests",
        f"Run `{EXECUTABLE} bench run --help` or `{EXECUTABLE} experiment plan --help` for inputs",
    ],
    "gap_not_found": [f"Run `{EXECUTABLE} gap list` to inspect durable gap ids"],
    "gap_transition_invalid": [
        f"Run `{EXECUTABLE} gap list` and use an allowed lifecycle transition"
    ],
    "provision_recovery_required": [
        "Review the preserved paths named in the transaction record before deciding",
        f"Run `{EXECUTABLE} provision-wiki <name> <path> --rollback --plan-id <id>` to undo it",
    ],
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    output_format = "toon"
    no_help_hints = False
    operation = "home"
    try:
        args = parser.parse_args(argv)
        output_format = str(_resolve(getattr(args, "format", None), "toon"))
        no_help_hints = bool(_resolve(getattr(args, "no_help_hints", None), False))
        operation = args.command or "home"
        root_label = str(_resolve(getattr(args, "root", None), "."))
        root = Path(root_label)
        doc, exit_code = _dispatch(args, root, root_label)
        _emit(doc, output_format, no_help_hints)
        return exit_code
    except UsageError as error:
        help_command = (
            f"{EXECUTABLE} --help" if operation == "home" else f"{EXECUTABLE} {operation} --help"
        )
        doc = _error_doc(
            "usage_error", str(error), operation, [f"Run `{help_command}` for usage and examples"]
        )
        _emit(doc, output_format, no_help_hints)
        return 2
    except (
        RegistryError,
        CaptureError,
        EvolveError,
        AdoptError,
        CardError,
        InitError,
        PathEscapeError,
        GardenError,
        EvaluationError,
    ) as error:
        code = str(getattr(error, "code", "operation_failed"))
        exit_code = 2 if code in {"not_initialized", "registry_invalid"} else 1
        doc = _error_doc(code, str(error), operation, _ERROR_HELP.get(code, []))
        _emit(doc, output_format, no_help_hints)
        return exit_code
    except FrontmatterError as error:
        doc = _error_doc(
            "frontmatter_invalid", str(error), operation, _ERROR_HELP["frontmatter_invalid"]
        )
        _emit(doc, output_format, no_help_hints)
        return 1
    except (OSError, UnicodeDecodeError) as error:
        doc = _error_doc("io_error", str(error), operation, [])
        _emit(doc, output_format, no_help_hints)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
