"""Governed explicit selection of one offer from a preflight result.

Selection is a narrow authorization bridge, not another routing pass. It
validates a complete original ``preflight-result/v2`` packet, recomputes that
packet against the current catalog and original request/model identity, and
then re-runs access, governance, and path-containment checks for exactly one
wiki that was in ``offers[]``. It never raises confidence or trust and never
reads page content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import Catalog, RootRef
from .fsops import PathEscapeError, content_hash, resolve_contained
from .preflight import (
    PreflightResult,
    _access_level,
    _context_budget,
    _follow_up,
    allowed_paths,
    run_preflight,
)
from .registry import load_registry
from .semantic import NgramBackend

Doc = dict[str, Any]

# `catalog_visibility` is a projection control, not an access control: a
# `redacted` wiki is only redacted in the rendered catalog, and preflight
# already routes it and hands it load paths. Everything this module does not
# recognize as projection-only is withheld, so unknown values fail closed.
_PROJECTION_ONLY_VISIBILITIES = frozenset({"full", "redacted"})

# Selection hands out a load surface, so it only accepts the access levels that
# have a defined one. Anything else, including a level added later, fails closed.
_LOADABLE_ACCESS = frozenset({"full", "digest-only"})


class SelectionError(ValueError):
    """Original evidence or the selected current offer is not loadable."""

    code = "selection_invalid"


@dataclass(frozen=True)
class SelectionResult:
    preflight_id: str
    request_hash: str
    catalog_hash: str
    model_class: str
    selection_id: str
    root_facts_hash: str
    selected: Doc
    selection: Doc


def read_preflight_result(path: Path) -> Doc:
    """Read one host-recorded JSON preflight packet without leaking its path.

    ``str(OSError)`` embeds the filename it failed on, and this refusal is
    emitted as a document on stdout, so filesystem failures are reported by
    their reason alone. Decode and JSON errors are already path-free.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SelectionError(
            f"preflight evidence is not readable: {error.strerror or type(error).__name__}"
        ) from error
    except UnicodeDecodeError as error:
        raise SelectionError(f"preflight evidence is not valid UTF-8: {error}") from error
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise SelectionError(f"preflight evidence is not readable JSON: {error}") from error
    if not isinstance(raw, dict):
        raise SelectionError("preflight evidence must be a JSON object")
    return raw


def _list(document: Doc, key: str) -> list[Doc]:
    value = document.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise SelectionError(f"preflight evidence {key} must be a list of objects")
    return value


def _names(entries: list[Doc], key: str) -> list[str]:
    names: list[str] = []
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise SelectionError(f"preflight evidence {key} contains an invalid wiki identity")
        names.append(name)
    return names


def _proof_id(document: Doc, matches: list[Doc], offers: list[Doc], filtered: list[Doc]) -> str:
    redacted_count = document.get("redacted_count")
    if isinstance(redacted_count, bool) or not isinstance(redacted_count, int):
        raise SelectionError("preflight evidence redacted_count must be an integer")
    proof = {
        "request_hash": document.get("request_hash"),
        "catalog_hash": document.get("catalog_hash"),
        "model_class": document.get("model_class"),
        "status": document.get("status"),
        "matches": _names(matches, "matches"),
        "offers": _names(offers, "offers"),
        "filtered": _names(filtered, "filtered"),
        "redacted_count": redacted_count,
    }
    return content_hash(json.dumps(proof, sort_keys=True))


def _validate_entries(label: str, original: list[Doc], current: list[Doc]) -> None:
    if len(original) != len(current):
        raise SelectionError(
            f"preflight evidence {label} is incomplete or stale; record preflight with --full"
        )
    current_by_identity: dict[tuple[object, object], Doc] = {}
    for entry in current:
        identity = (entry.get("name"), entry.get("root"))
        if identity in current_by_identity:
            raise SelectionError(f"current catalog has duplicate {label} identity")
        current_by_identity[identity] = entry
    for entry in original:
        identity = (entry.get("name"), entry.get("root"))
        expected = current_by_identity.get(identity)
        if expected is None or entry != expected:
            raise SelectionError(f"preflight evidence {label} is malformed, stale, or swapped")


def _validate_original(
    document: Doc,
    request: str,
    model_class: str,
    current: PreflightResult,
) -> tuple[list[Doc], list[Doc]]:
    required = {
        "schema_version",
        "request",
        "request_hash",
        "model_class",
        "status",
        "confidence",
        "thresholds",
        "semantic",
        "preflight_id",
        "catalog_hash",
        "matches",
        "offers",
        "filtered",
        "declined",
        "root_issues",
        "redacted_count",
        "notes",
    }
    missing = sorted(required - set(document))
    if missing:
        raise SelectionError(f"preflight evidence is incomplete; missing: {', '.join(missing)}")
    if document.get("schema_version") != "megamind/preflight-result/v2":
        raise SelectionError("preflight evidence schema must be megamind/preflight-result/v2")
    stored_request = document.get("request")
    if not isinstance(stored_request, str) or stored_request != request:
        raise SelectionError("selection request does not match the original preflight request")
    if document.get("request_hash") != content_hash(request):
        raise SelectionError("preflight evidence request hash is malformed or stale")
    if document.get("model_class") != model_class:
        raise SelectionError("selection model class does not match the original preflight")
    if document.get("catalog_hash") != current.catalog_hash:
        raise SelectionError("catalog changed since the original preflight; run preflight again")

    matches = _list(document, "matches")
    offers = _list(document, "offers")
    filtered = _list(document, "filtered")
    if document.get("preflight_id") != _proof_id(document, matches, offers, filtered):
        raise SelectionError("preflight evidence proof identity is malformed")
    if document.get("preflight_id") != current.preflight_id:
        raise SelectionError("preflight evidence does not match the current request/catalog/model")
    if document.get("status") != current.status:
        raise SelectionError("preflight evidence status is malformed or stale")

    _validate_entries("matches", matches, current.matches)
    _validate_entries("offers", offers, current.offers)
    if filtered != current.filtered:
        raise SelectionError("preflight evidence filtered entries are malformed, stale, or swapped")
    declined = _list(document, "declined")
    root_issues = _list(document, "root_issues")
    if declined != current.declined or root_issues != current.root_issues:
        raise SelectionError("preflight evidence refusal entries are malformed, stale, or swapped")
    if document.get("redacted_count") != current.redacted_count:
        raise SelectionError("preflight evidence redaction count is malformed or stale")
    if (
        document.get("confidence") != current.confidence
        or document.get("thresholds") != current.thresholds
    ):
        raise SelectionError("preflight evidence confidence fields are malformed or stale")
    if document.get("semantic") != current.semantic:
        raise SelectionError("preflight evidence semantic outcome is malformed or stale")
    notes = document.get("notes")
    if not isinstance(notes, list) or not all(isinstance(note, str) for note in notes):
        raise SelectionError("preflight evidence notes must be a list of strings")
    if notes != current.notes:
        raise SelectionError("preflight evidence notes are malformed, truncated, or stale")
    return matches, offers


def _selected_row(catalog: Catalog, wiki: str, offer: Doc) -> Doc:
    """Return the one current catalog row this offer may still be selected from.

    Visibility only decides what the rendered catalog shows, so a `redacted`
    wiki stays selectable exactly as preflight already treats it; whether it may
    actually be loaded is settled by the independent access, trust, follow-up,
    artifact, and containment checks that own those decisions.
    """
    rows = [row for row in catalog.rows if row.get("name") == wiki]
    if len(rows) != 1:
        raise SelectionError(
            "selected wiki identity is absent or duplicated in the current catalog"
        )
    row = rows[0]
    if row.get("root") != offer.get("root"):
        raise SelectionError("selected wiki root does not match the original offer")
    if row.get("status") != "ok":
        raise SelectionError("selected wiki is currently broken or unavailable")
    if row.get("catalog_visibility") not in _PROJECTION_ONLY_VISIBILITIES:
        raise SelectionError("selected wiki is withheld by catalog visibility and cannot be loaded")
    if bool(row.get("provisional")):
        raise SelectionError("selected wiki is provisional and cannot be loaded")
    if row.get("routing_mode") == "pointer":
        raise SelectionError("selected wiki is pointer-only and cannot be loaded")
    return row


def _selected_ref(refs: list[RootRef], row: Doc) -> RootRef:
    found = [ref for ref in refs if ref.label == row.get("root")]
    if len(found) != 1:
        raise SelectionError("selected wiki root identity is absent or duplicated")
    return found[0]


def _validate_root_and_paths(ref: RootRef, row: Doc, allows: list[str]) -> str:
    """Validate containment and hash privacy-safe resolved root facts.

    The hash records root-relative resolution, never machine-specific absolute
    paths or artifact bytes. Card facts are already in ``row`` and therefore in
    the catalog hash; these facts additionally bind the concrete current root
    and every handed-out path to where it resolves inside that root.
    """
    try:
        root = ref.path.resolve()
        if row.get("source") == "registry":
            registry = load_registry(ref.path)
            wiki = registry.wiki_by_name(str(row.get("name")))
            if wiki is None:
                raise SelectionError("selected wiki is absent from its current registry")
            wiki_root = resolve_contained(ref.path, wiki.path)
            if not wiki_root.is_dir():
                raise SelectionError("selected wiki root is absent")
        else:
            wiki_root = resolve_contained(ref.path, ".")
        resolved_allows: list[dict[str, str]] = []
        for path in allows:
            target = resolve_contained(ref.path, path)
            if not target.is_file():
                raise SelectionError(f"selected wiki declares an absent load artifact: {path}")
            resolved_allows.append(
                {"declared": path, "resolved": target.relative_to(root).as_posix()}
            )
    except PathEscapeError as error:
        raise SelectionError("selected wiki path or symlink escapes its declared root") from error
    facts = {
        "card": row,
        "root_label": ref.label,
        "wiki_root": wiki_root.relative_to(root).as_posix(),
        "allows": resolved_allows,
    }
    return content_hash(json.dumps(facts, sort_keys=True))


def select_offer(
    refs: list[RootRef],
    request: str,
    model_class: str,
    wiki: str,
    original: Doc,
    catalog: Catalog,
) -> SelectionResult:
    """Authorize exactly one eligible original offer, or refuse without widening."""
    semantic = original.get("semantic")
    if not isinstance(semantic, dict):
        raise SelectionError("preflight evidence semantic outcome must be an object")
    status, backend_name = semantic.get("status"), semantic.get("backend")
    if status == "disabled" and backend_name == "none":
        backend = None
    elif status == "ok" and backend_name == "char-ngram":
        backend = NgramBackend()
    else:
        raise SelectionError("preflight semantic outcome cannot be deterministically replayed")
    current = run_preflight(refs, request, model_class, catalog, semantic=backend)
    _matches, offers = _validate_original(original, request, model_class, current)
    offered = [entry for entry in offers if entry.get("name") == wiki]
    if len(offered) != 1:
        raise SelectionError("selected wiki must appear exactly once in the original offers")
    offer = offered[0]
    row = _selected_row(catalog, wiki, offer)
    access = _access_level(row, model_class)
    if access == "none":
        raise SelectionError("selected wiki is not accessible to this model class")

    if not isinstance(row.get("paths"), dict):
        raise SelectionError("selected wiki card paths are malformed")
    if access not in _LOADABLE_ACCESS:
        raise SelectionError("selected wiki has an unknown effective access level")
    allows = allowed_paths(row, access)
    if access == "digest-only" and not allows:
        raise SelectionError("selected digest-only wiki declares no approved digest")

    follow_up = _follow_up(row, request, access)
    if not follow_up.loadable:
        raise SelectionError("selected wiki has no current loadable follow-up")
    ref = _selected_ref(refs, row)
    root_facts_hash = _validate_root_and_paths(ref, row, allows)

    selected = dict(offer)
    selected.update(
        {
            "access": access,
            "routing_mode": row.get("routing_mode"),
            "allows": allows,
            "follow_up": follow_up.text,
        }
    )
    budget = _context_budget(row)
    if budget is not None:
        selected["context_budget"] = budget

    provenance: Doc = {
        "status": "explicit-user-selection",
        "basis": "selected-current-offer",
        "source_disposition": "offer",
        "source_status": original["status"],
        "preflight_id": original["preflight_id"],
        "confidence_changed": False,
    }
    proof = {
        "preflight_id": original["preflight_id"],
        "request_hash": current.request_hash,
        "catalog_hash": current.catalog_hash,
        "model_class": model_class,
        "wiki": wiki,
        "root_facts_hash": root_facts_hash,
        "access": access,
        "allows": allows,
        "follow_up": follow_up.text,
        "context_budget": budget,
        "confidence": offer.get("confidence"),
        "evidence": offer.get("evidence"),
        "provenance": provenance,
    }
    return SelectionResult(
        preflight_id=current.preflight_id,
        request_hash=current.request_hash,
        catalog_hash=current.catalog_hash,
        model_class=model_class,
        selection_id=content_hash(json.dumps(proof, sort_keys=True)),
        root_facts_hash=root_facts_hash,
        selected=selected,
        selection=provenance,
    )
