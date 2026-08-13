"""Governed explicit selection of a preflight offer or eligible existing wiki.

Selection is a narrow authorization bridge, not another routing pass. It
validates a complete original ``preflight-result/v2`` packet, recomputes that
packet against the current catalog and original request/model identity, and
then re-runs access, governance, and path-containment checks. The separate
existing-wiki path atomically consumes an exact-request list derived from the
current catalog. Neither path raises confidence or trust or reads page content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from .catalog import Catalog, RootRef
from .fsops import (
    AuditValue,
    PathEscapeError,
    append_audit,
    atomic_write,
    backup_existing,
    content_hash,
    create_file,
    resolve_contained,
)
from .preflight import (
    MODEL_CLASSES,
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
class ExistingSelectionResult:
    request_hash: str
    catalog_hash: str
    model_class: str
    owner_id: str
    session_id: str
    today: str
    selection_id: str
    root_facts_hash: str
    selected: Doc
    selection: Doc


@dataclass(frozen=True)
class ExistingSelectionList:
    request_hash: str
    catalog_hash: str
    model_class: str
    owner_id: str
    session_id: str
    today: str
    selection_id: str
    wikis: list[Doc]
    total_wikis: int


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


def _existing_state_path(state_root: Path, selection_id: str) -> Path:
    return Path(".megamind") / "audit" / "existing-selection" / f"{selection_id}.json"


def _existing_claim_path(selection_id: str) -> Path:
    return Path(".megamind") / "audit" / "existing-selection" / "claims" / f"{selection_id}.json"


def _existing_binding(
    request: str,
    model_class: str,
    owner_id: str,
    session_id: str,
    today: date,
    catalog: Catalog,
    eligible: list[Doc],
    state_root: Path,
) -> Doc:
    """Return the privacy-safe binding for one host-side picker decision."""
    return {
        "request_hash": content_hash(request),
        "catalog_hash": catalog.catalog_hash,
        "model_class": model_class,
        "owner_id_hash": content_hash(owner_id),
        "session_id_hash": content_hash(session_id),
        "today": today.isoformat(),
        "home_id": content_hash(str(state_root.resolve())),
        "eligible": eligible,
    }


def _existing_state_error(message: str) -> SelectionError:
    return SelectionError(f"existing selection authorization is {message}")


def _read_existing_state(state_root: Path, selection_id: str) -> Doc:
    path = _existing_state_path(state_root, selection_id)
    try:
        raw = resolve_contained(state_root, path).read_text(encoding="utf-8")
    except (OSError, PathEscapeError) as error:
        raise _existing_state_error("unknown or not issued") from error
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise _existing_state_error("malformed") from error
    if not isinstance(document, dict):
        raise _existing_state_error("malformed")
    payload = document.get("payload")
    state_hash = document.get("state_hash")
    if not isinstance(payload, dict) or not isinstance(state_hash, str):
        raise _existing_state_error("malformed")
    if content_hash(json.dumps(payload, sort_keys=True)) != state_hash:
        raise _existing_state_error("tampered")
    if payload.get("selection_id") != selection_id:
        raise _existing_state_error("identity is malformed")
    if payload.get("status") not in {"listed", "consumed"}:
        raise _existing_state_error("status is malformed")
    audit_event = payload.get("audit_event")
    if not isinstance(audit_event, dict):
        raise _existing_state_error("audit record is malformed")
    if audit_event.get("action") not in {
        "existing_selection_listed",
        "existing_selection_consumed",
    } or not isinstance(audit_event.get("event_id"), str):
        raise _existing_state_error("audit record is malformed")
    if audit_event.get("backup") is not None and not isinstance(audit_event.get("backup"), str):
        raise _existing_state_error("audit record is malformed")
    return payload


def _state_document(payload: Doc) -> str:
    state_hash = content_hash(json.dumps(payload, sort_keys=True))
    document = {
        "schema": "megamind/existing-selection-state/v1",
        "payload": payload,
        "state_hash": state_hash,
    }
    return json.dumps(document, sort_keys=True) + "\n"


def _state_audit_time(payload: Doc) -> datetime:
    binding = payload.get("binding")
    if not isinstance(binding, dict) or not isinstance(binding.get("today"), str):
        raise _existing_state_error("malformed")
    try:
        return datetime.combine(date.fromisoformat(binding["today"]), time(), timezone.utc)
    except ValueError as error:
        raise _existing_state_error("malformed") from error


def _audit_event(selection_id: str, status: str) -> Doc:
    action = f"existing_selection_{status}"
    return {
        "action": action,
        "event_id": content_hash(f"{selection_id}:{action}"),
        "backup": None,
    }


def _audit_details(payload: Doc) -> dict[str, AuditValue]:
    binding = payload.get("binding")
    audit_event = payload.get("audit_event")
    if not isinstance(binding, dict) or not isinstance(audit_event, dict):
        raise _existing_state_error("audit record is malformed")
    return {
        "selection_id": str(payload["selection_id"]),
        "request_hash": str(binding["request_hash"]),
        "catalog_hash": str(binding["catalog_hash"]),
        "event_id": str(audit_event["event_id"]),
        "backup": audit_event.get("backup") if isinstance(audit_event.get("backup"), str) else None,
    }


def _audit_event_exists(state_root: Path, event_id: str) -> bool:
    try:
        raw = resolve_contained(state_root, Path(".megamind") / "audit" / "log.jsonl").read_text(
            encoding="utf-8"
        )
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _existing_state_error("audit could not be read") from error
    for line in raw.splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise _existing_state_error("audit is malformed") from error
        if isinstance(entry, dict) and entry.get("event_id") == event_id:
            return True
    return False


def _ensure_existing_audit(state_root: Path, payload: Doc) -> None:
    audit_event = payload.get("audit_event")
    if not isinstance(audit_event, dict):
        raise _existing_state_error("audit record is malformed")
    event_id = audit_event.get("event_id")
    action = audit_event.get("action")
    if not isinstance(event_id, str) or not isinstance(action, str):
        raise _existing_state_error("audit record is malformed")
    if _audit_event_exists(state_root, event_id):
        return
    try:
        append_audit(
            state_root,
            action,
            _audit_details(payload),
            now=_state_audit_time(payload),
        )
    except OSError as error:
        raise _existing_state_error("could not be audited") from error


def _write_existing_state(state_root: Path, payload: Doc) -> None:
    target = _existing_state_path(state_root, str(payload["selection_id"]))
    try:
        backup = backup_existing(state_root, target, durable=True)
        audit_event = payload.get("audit_event")
        if not isinstance(audit_event, dict):
            raise _existing_state_error("audit record is malformed")
        audit_event["backup"] = backup.name if backup else None
        atomic_write(
            state_root,
            target,
            _state_document(payload),
            durable=True,
        )
    except OSError as error:
        raise _existing_state_error("could not be recorded") from error
    _ensure_existing_audit(state_root, payload)


def _create_existing_state(state_root: Path, payload: Doc) -> bool:
    try:
        create_file(
            state_root,
            _existing_state_path(state_root, str(payload["selection_id"])),
            _state_document(payload),
            durable=True,
        )
    except FileExistsError:
        return False
    except OSError as error:
        raise _existing_state_error("could not be recorded") from error
    _ensure_existing_audit(state_root, payload)
    return True


def _claim_existing_state(state_root: Path, state: Doc, wiki: str) -> None:
    selection_id = str(state["selection_id"])
    claim = {
        "selection_id": selection_id,
        "state_hash": content_hash(json.dumps(state, sort_keys=True)),
        "choice_hash": content_hash(wiki),
        "audit_event": _audit_event(selection_id, "claimed"),
    }
    try:
        create_file(
            state_root,
            _existing_claim_path(selection_id),
            json.dumps(claim, sort_keys=True) + "\n",
            durable=True,
        )
    except FileExistsError as error:
        existing = _read_existing_claim(state_root, selection_id, state)
        recovered = dict(state)
        recovered["status"] = "consumed"
        recovered["audit_event"] = _audit_event(selection_id, "consumed")
        _ensure_existing_audit(state_root, _claim_audit_payload(state, existing))
        _write_existing_state(state_root, recovered)
        raise _existing_state_error("already consumed") from error
    except OSError as error:
        raise _existing_state_error("could not be claimed") from error
    _ensure_existing_audit(state_root, _claim_audit_payload(state, claim))


def _claim_audit_payload(state: Doc, claim: Doc) -> Doc:
    audit_event = claim.get("audit_event")
    if not isinstance(audit_event, dict):
        raise _existing_state_error("claim is malformed")
    payload = dict(state)
    payload["audit_event"] = audit_event
    return payload


def _read_existing_claim(state_root: Path, selection_id: str, state: Doc) -> Doc:
    try:
        raw = resolve_contained(state_root, _existing_claim_path(selection_id)).read_text(
            encoding="utf-8"
        )
        claim = json.loads(raw)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise _existing_state_error("claim is malformed") from error
    if not isinstance(claim, dict):
        raise _existing_state_error("claim is malformed")
    audit_event = claim.get("audit_event")
    if (
        claim.get("selection_id") != selection_id
        or claim.get("state_hash") != content_hash(json.dumps(state, sort_keys=True))
        or not isinstance(claim.get("choice_hash"), str)
        or not isinstance(audit_event, dict)
        or audit_event != _audit_event(selection_id, "claimed")
    ):
        raise _existing_state_error("claim is malformed")
    return claim


def _existing_candidates(refs: list[RootRef], catalog: Catalog, model_class: str) -> list[Doc]:
    """Derive the picker set from validated current rows, never host input."""
    rows_by_name: dict[str, list[Doc]] = {}
    for row in catalog.rows:
        name = row.get("name")
        if isinstance(name, str):
            rows_by_name.setdefault(name, []).append(row)

    candidates: list[Doc] = []
    for row in catalog.rows:
        name = row.get("name")
        if not isinstance(name, str) or len(rows_by_name.get(name, [])) != 1:
            continue
        if row.get("status") != "ok":
            continue
        # Hidden rows remain internal catalog facts but are never enumerable.
        if row.get("catalog_visibility") not in _PROJECTION_ONLY_VISIBILITIES:
            continue
        if bool(row.get("provisional")) or row.get("routing_mode") == "pointer":
            continue
        if row.get("stale") is True:
            continue
        access = _access_level(row, model_class)
        if access not in _LOADABLE_ACCESS:
            continue
        if not isinstance(row.get("paths"), dict):
            continue
        allows = allowed_paths(row, access)
        # Unlike a router ladder, this operation authorizes a declared existing
        # artifact only. An empty declaration is not an eligible existing wiki.
        if not allows or (access == "digest-only" and len(allows) != 1):
            continue
        matching_refs = [ref for ref in refs if ref.label == row.get("root")]
        if len(matching_refs) != 1 or matching_refs[0].path.is_symlink():
            continue
        try:
            root_facts_hash = _validate_root_and_paths(matching_refs[0], row, allows)
        except SelectionError:
            continue
        follow_up = _follow_up(row, "", access)
        if not follow_up.loadable:
            continue
        budget = _context_budget(row)
        entry: Doc = {
            "name": name,
            "root": row.get("root"),
            "access": access,
            "root_facts_hash": root_facts_hash,
        }
        if budget is not None:
            entry["context_budget"] = budget
        candidates.append(entry)
    candidates.sort(key=lambda item: (str(item.get("root")), str(item.get("name"))))
    return candidates


def list_existing(
    refs: list[RootRef],
    request: str,
    model_class: str,
    owner_id: str,
    session_id: str,
    today: date,
    catalog: Catalog,
    state_root: Path,
    full: bool = False,
) -> ExistingSelectionList:
    """Issue a one-time, exact-request list of eligible existing wikis."""
    if not request:
        raise SelectionError("existing selection request must not be empty")
    if model_class not in MODEL_CLASSES:
        raise SelectionError("existing selection model class is invalid")
    if not owner_id or not session_id:
        raise SelectionError("existing selection requires owner and session identities")
    candidates = _existing_candidates(refs, catalog, model_class)
    eligible = candidates if full else candidates[:20]
    binding = _existing_binding(
        request, model_class, owner_id, session_id, today, catalog, eligible, state_root
    )
    for issuance in range(10000):
        nonce = content_hash(json.dumps({"binding": binding, "issuance": issuance}, sort_keys=True))
        path = resolve_contained(state_root, _existing_state_path(state_root, nonce))
        if path.is_file():
            try:
                prior = _read_existing_state(state_root, nonce)
            except SelectionError:
                raise
            if prior.get("binding") == binding and prior.get("status") == "listed":
                _ensure_existing_audit(state_root, prior)
                return ExistingSelectionList(
                    content_hash(request),
                    catalog.catalog_hash,
                    model_class,
                    owner_id,
                    session_id,
                    today.isoformat(),
                    nonce,
                    eligible,
                    len(candidates),
                )
            continue
        payload: Doc = {
            "schema": "megamind/existing-selection-state/v1",
            "status": "listed",
            "selection_id": nonce,
            "issuance": issuance,
            "binding": binding,
            "audit_event": _audit_event(nonce, "listed"),
        }
        if _create_existing_state(state_root, payload):
            return ExistingSelectionList(
                content_hash(request),
                catalog.catalog_hash,
                model_class,
                owner_id,
                session_id,
                today.isoformat(),
                nonce,
                eligible,
                len(candidates),
            )
    raise _existing_state_error("nonce space is exhausted")


def select_existing(
    refs: list[RootRef],
    request: str,
    model_class: str,
    owner_id: str,
    session_id: str,
    today: date,
    selection_id: str,
    wiki: str,
    catalog: Catalog,
    state_root: Path,
) -> ExistingSelectionResult:
    """Consume one issued list and authorize one current eligible wiki."""
    if not wiki:
        raise SelectionError("existing selection requires one wiki choice")
    if model_class not in MODEL_CLASSES:
        raise SelectionError("existing selection model class is invalid")
    state = _read_existing_state(state_root, selection_id)
    _ensure_existing_audit(state_root, state)
    if state.get("status") != "listed":
        raise _existing_state_error("already consumed")
    candidates = _existing_candidates(refs, catalog, model_class)
    stored_binding = state.get("binding")
    if not isinstance(stored_binding, dict):
        raise _existing_state_error("malformed")
    listed_eligible = stored_binding.get("eligible")
    if not isinstance(listed_eligible, list) or not all(
        isinstance(entry, dict) for entry in listed_eligible
    ):
        raise _existing_state_error("malformed")
    if listed_eligible != candidates and listed_eligible != candidates[:20]:
        raise _existing_state_error("stale, drifted, or malformed")
    eligible = listed_eligible
    binding = _existing_binding(
        request, model_class, owner_id, session_id, today, catalog, eligible, state_root
    )
    if state.get("binding") != binding:
        raise _existing_state_error("stale, drifted, or bound to another session")
    issuance = state.get("issuance")
    expected = content_hash(json.dumps({"binding": binding, "issuance": issuance}, sort_keys=True))
    if expected != selection_id:
        raise _existing_state_error("nonce is malformed")
    selected_entries = [entry for entry in eligible if entry.get("name") == wiki]
    if len(selected_entries) != 1:
        raise SelectionError("chosen wiki is not in the current eligible existing set")
    entry = selected_entries[0]
    rows = [
        row
        for row in catalog.rows
        if row.get("name") == wiki and row.get("root") == entry.get("root")
    ]
    if len(rows) != 1:
        raise SelectionError("chosen wiki identity is absent or duplicated")
    row = rows[0]
    access = _access_level(row, model_class)
    allows = allowed_paths(row, access)
    ref = _selected_ref(refs, row)
    root_facts_hash = _validate_root_and_paths(ref, row, allows)
    follow_up = _follow_up(row, request, access)
    if not follow_up.loadable:
        raise SelectionError("chosen wiki has no current loadable follow-up")
    budget = _context_budget(row)
    selected: Doc = {
        "name": wiki,
        "root": row.get("root"),
        "access": access,
        "routing_mode": row.get("routing_mode"),
        "allows": allows,
        "follow_up": follow_up.text,
        "threshold_matched": False,
        "confidence": None,
    }
    if budget is not None:
        selected["context_budget"] = budget
    provenance: Doc = {
        "status": "explicit-user-selection",
        "basis": "selected-eligible-existing",
        "source_disposition": "eligible-existing",
        "threshold_matched": False,
        "confidence_changed": False,
        "request_hash": content_hash(request),
        "catalog_hash": catalog.catalog_hash,
        "model_class": model_class,
        "owner_id": owner_id,
        "session_id": session_id,
        "selection_id": selection_id,
        "today": today.isoformat(),
    }
    _claim_existing_state(state_root, state, wiki)
    consumed = dict(state)
    consumed["status"] = "consumed"
    consumed["audit_event"] = _audit_event(selection_id, "consumed")
    _write_existing_state(state_root, consumed)
    return ExistingSelectionResult(
        content_hash(request),
        catalog.catalog_hash,
        model_class,
        owner_id,
        session_id,
        today.isoformat(),
        selection_id,
        root_facts_hash,
        selected,
        provenance,
    )


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
