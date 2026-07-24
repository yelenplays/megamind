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
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, NoReturn

from . import __version__, toon
from .capture import CaptureError, capture, list_proposals
from .doctor import run_doctor
from .evolve import EvolveError, apply_plan, plan
from .fsops import PathEscapeError
from .models import FrontmatterError, parse_document
from .registry import REGISTRY_PATH, Registry, RegistryError, load_registry
from .review import ReviewReport, review
from .routing import RouteResult, route
from .scaffold import init_vault
from .skillpack import skill_files, write_skill

EXECUTABLE = "megamind-axi"
PURPOSE = (
    "Deterministic knowledge gardener for Markdown wikis: route questions to "
    "the smallest useful context, capture proposals, evolve pages with approval."
)

ROUTE_FIELDS_DEFAULT = ["path", "kind", "score", "reason"]
ROUTE_FIELDS_ALL = ["path", "kind", "score", "wiki", "privacy", "chars", "reason", "reasons"]
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


def cmd_init(target: str, starter: bool) -> tuple[Doc, int]:
    result = init_vault(Path(target), starter=starter)
    doc: Doc = {
        "schema_version": "megamind/init-result/v1",
        "status": "already_initialized" if result.already_initialized else "initialized",
        "root": target,
        "created": result.created,
        "skipped": result.skipped,
        "help": _help(
            f"Run `{EXECUTABLE} --root {target}` for the vault home view",
            f"Run `{EXECUTABLE} --root {target} doctor` to validate the vault",
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
        "reason": reasons[0] if reasons else "",
        "reasons": " | ".join(reasons),
    }
    return {field: values[field] for field in fields}


def cmd_route(root: Path, registry: Registry, query: str, fields: list[str]) -> tuple[Doc, int]:
    result: RouteResult = route(root, registry, query)
    doc: Doc = {
        "schema_version": "megamind/route-result/v1",
        "query": query,
        "matched": result.matched,
        "candidates": [_route_row(asdict(c), fields) for c in result.candidates],
        "context_chars": result.context_chars,
        "max_context_chars": result.max_context_chars,
        "max_candidates": result.max_candidates,
        "notes": result.notes,
    }
    if result.matched:
        best = result.candidates[0]
        doc["help"] = _help(
            f"Open `{best.path}` first; it scored highest",
            f'Run `{EXECUTABLE} route "{query}" --fields path,kind,score,privacy,reasons` '
            "for detail",
        )
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
            f"Run `{EXECUTABLE} evolve {result.proposal_id}` to plan applying it (dry run)",
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
    plan_id: str | None,
    approve_new_wiki: bool,
    full: bool,
    today: date | None,
) -> tuple[Doc, int]:
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
            f"{EXECUTABLE} evolve {computed.proposal_id} --apply --plan-id {computed.plan_id}"
        )
        if destination:
            apply_cmd += f" --dest {destination}"
        if supersedes:
            apply_cmd += f" --supersedes {supersedes}"
        if computed.creates_new_wiki:
            apply_cmd += " --approve-new-wiki"
        steps.append(f"Run `{apply_cmd}` after human review of this diff")
    if truncated:
        steps.append(f"Run `{EXECUTABLE} evolve {computed.proposal_id} --full` for the whole diff")
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
            f"Run `{EXECUTABLE} evolve {first} --dest <WikiName>` to categorize a proposal"
        )
    elif report.open_proposals:
        first = report.open_proposals[0].rsplit("/", 1)[-1].removesuffix(".md")
        steps.append(f"Run `{EXECUTABLE} evolve {first}` to plan applying a proposal")
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
            f"  {EXECUTABLE} evolve <proposal-id> --apply --plan-id <plan-id>"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _common_flags(p_evolve)
    p_evolve.add_argument("proposal", help="proposal id or path")
    p_evolve.add_argument("--dest", default=None, help="destination page or wiki")
    p_evolve.add_argument("--supersedes", default=None, help="existing page the change supersedes")
    p_evolve.add_argument("--apply", action="store_true", help="apply instead of dry run")
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

    p_config = sub.add_parser("config", help="inspect configuration")
    _common_flags(p_config)
    config_sub = p_config.add_subparsers(dest="config_command")
    p_config_show = config_sub.add_parser(
        "show", help="show registry path, budgets, and registered wikis"
    )
    _common_flags(p_config_show)

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


def _parse_today(raw: str | None) -> date | None:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise UsageError(f"--today must be an ISO date (YYYY-MM-DD): {raw}") from error


def _dispatch(args: argparse.Namespace, root: Path, root_label: str) -> tuple[Doc, int]:
    command = args.command
    if command is None:
        return cmd_home(root, root_label, _parse_today(getattr(args, "today", None)))
    if command == "init":
        return cmd_init(args.target, starter=not args.no_starter)
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
        registry = load_registry(root)
        return cmd_route(root, registry, " ".join(args.query), fields)
    if command == "capture":
        registry = load_registry(root)
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
        if args.apply and not args.plan_id:
            raise UsageError("--apply requires --plan-id from the dry run")
        registry = load_registry(root)
        return cmd_evolve(
            root,
            registry,
            args.proposal,
            destination=args.dest,
            supersedes=args.supersedes,
            apply=args.apply,
            plan_id=args.plan_id,
            approve_new_wiki=args.approve_new_wiki,
            full=args.full,
            today=_parse_today(getattr(args, "today", None)),
        )
    if command == "review":
        registry = load_registry(root)
        return cmd_review(
            root, registry, today=_parse_today(getattr(args, "today", None)), full=args.full
        )
    if command == "doctor":
        return cmd_doctor(root, full=args.full)
    if command == "config":
        if getattr(args, "config_command", None) != "show":
            raise UsageError("usage: megamind-axi config show")
        return cmd_config_show(root, root_label)
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
    "capture_invalid": [
        f'Run `{EXECUTABLE} capture --text "<content>" --type '
        "fact|decision|hypothesis|procedure|example|guidance`"
    ],
    "frontmatter_invalid": [
        f"Run `{EXECUTABLE} doctor` to locate the file with unsupported frontmatter",
        "Fix the frontmatter block by hand; Megamind parses a small YAML subset",
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
    except (RegistryError, CaptureError, EvolveError, PathEscapeError) as error:
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
