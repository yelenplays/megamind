"""Provider-neutral, local-only host rollout proofs.

The rollout surface promotes one host/wiki binding at a time. It validates card
policy, two preflight outcomes, host capability evidence, wiki health, and a
successful value evaluation before writing a loadable proof. The proof is a
host-consumable artifact, not a host configuration change: this module has no
provider, network, scheduler, worker, account, repository, billing, or publish
adapter.

Rollout state lives in an explicit external directory, never in a wiki or
estate. Every apply is a write-ahead transaction. Replaying an interrupted
apply completes the same bytes; rollback disarms the binding without deleting
proof or transaction evidence.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .access import effective_policy
from .card import load_wiki_card
from .catalog import build_catalog, discover_roots
from .doctor import run_doctor
from .fsops import atomic_write_path, content_hash, resolve_contained
from .preflight import run_preflight
from .registry import RegistryNotInitialized, WikiEntry, load_registry

Doc = dict[str, Any]

HOST_EVIDENCE_SCHEMA = "megamind/host-rollout-evidence/v1"
PROMOTION_PROOF_SCHEMA = "megamind/host-wiki-promotion-proof/v1"
PLAN_SCHEMA = "megamind/rollout-plan/v1"
RESULT_SCHEMA = "megamind/rollout-result/v1"
HEALTH_SCHEMA = "megamind/rollout-health/v1"
STATUS_SCHEMA = "megamind/rollout-status/v1"
ROLLBACK_PLAN_SCHEMA = "megamind/rollout-rollback-plan/v1"
ROLLBACK_RECEIPT_SCHEMA = "megamind/rollout-rollback-receipt/v1"
TRANSACTION_SCHEMA = "megamind/rollout-transaction/v1"
PREFLIGHT_SCHEMA = "megamind/preflight-result/v2"
EVALUATION_SCHEMA = "megamind/evaluation-score/v1"

_HOST_CHECKS = (
    "mandatory_preflight",
    "privacy_enforcement",
    "quiet_no_match",
    "task_logging",
    "failure_disclosure",
    "local_only_activation",
)


class RolloutError(ValueError):
    """A malformed, unsafe, or stale rollout operation."""

    code = "rollout_invalid"


@dataclass(frozen=True)
class RolloutInputs:
    state_root: Path
    estate: Path
    wiki_root: Path
    wiki: str
    host_id: str
    model_class: str
    host_evidence: Path
    preflight_evidence: Path
    no_match_evidence: Path
    evaluation_evidence: Path
    governance_approval: str
    access_approval: str
    sequence: int
    prior_proofs: tuple[Path, ...] = ()
    today: date | None = None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path, label: str) -> Doc:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RolloutError(f"malformed {label}") from error
    if not isinstance(value, dict):
        raise RolloutError(f"{label} must be a JSON object")
    return value


def _reject_unknown(value: Doc, allowed: set[str], label: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RolloutError(f"{label} has unknown field(s): {', '.join(unknown)}")


def _state_root(path: Path, protected_roots: tuple[Path, ...] = ()) -> Path:
    resolved = path.expanduser().resolve()
    for protected in protected_roots:
        other = protected.expanduser().resolve()
        if resolved == other or resolved in other.parents or other in resolved.parents:
            raise RolloutError("rollout state must be isolated from every wiki and estate root")
    for parent in (resolved, *resolved.parents):
        if (parent / ".megamind").is_dir():
            raise RolloutError("rollout state must not be written inside a Megamind vault")
    return resolved


def _state_path(state_root: Path, relative: str) -> Path:
    return resolve_contained(state_root, relative)


def _write_state(state_root: Path, relative: str, document: Doc) -> Path:
    target = _state_path(state_root, relative)
    return atomic_write_path(target, _canonical(document) + "\n", durable=True)


def _target_entry(root: Path, name: str) -> WikiEntry:
    try:
        registry = load_registry(root)
    except RegistryNotInitialized:
        card = load_wiki_card(root)
        if card.name != name:
            raise RolloutError(
                "requested wiki does not match the authoritative wiki card"
            ) from None
        return card
    entry = registry.wiki_by_name(name)
    if entry is None:
        raise RolloutError("requested wiki is not present in the authoritative registry")
    return entry


def _card_sha256(entry: WikiEntry) -> str:
    return _sha256(_canonical(asdict(entry)))


def _privacy_rank(entry: WikiEntry, access: str) -> int:
    policy = effective_policy(entry)
    sensitivity_rank = {
        "public-reference": 0,
        "company-private": 2,
        "collaborative": 2,
        "personal-local": 3,
        "unclassified": 3,
    }.get(policy.sensitivity, 3)
    if access == "none" or policy.routing_mode == "pointer":
        return 4
    if access == "digest-only" or entry.privacy == "digest-only":
        return max(1, sensitivity_rank)
    return sensitivity_rank


def _check(name: str, passed: bool, evidence: str) -> Doc:
    return {"check": name, "status": "passed" if passed else "failed", "evidence": evidence}


def _valid_preflight_id(document: Doc) -> bool:
    request = document.get("request")
    if not isinstance(request, str) or content_hash(request) != document.get("request_hash"):
        return False
    for key in ("matches", "offers", "filtered"):
        if not isinstance(document.get(key), list):
            return False
    proof = {
        "request_hash": document.get("request_hash"),
        "catalog_hash": document.get("catalog_hash"),
        "model_class": document.get("model_class"),
        "status": document.get("status"),
        "matches": [str(row.get("name")) for row in document["matches"] if isinstance(row, dict)],
        "offers": [str(row.get("name")) for row in document["offers"] if isinstance(row, dict)],
        "filtered": [str(row.get("name")) for row in document["filtered"] if isinstance(row, dict)],
        "redacted_count": document.get("redacted_count"),
    }
    return content_hash(json.dumps(proof, sort_keys=True)) == document.get("preflight_id")


def _preflight_checks(
    document: Doc,
    *,
    label: str,
    model_class: str,
    catalog_hash: str,
    wiki: str,
    access: str,
    entry: WikiEntry,
    expect_match: bool,
) -> list[Doc]:
    schema_ok = document.get("schema_version") == PREFLIGHT_SCHEMA
    identity_ok = schema_ok and _valid_preflight_id(document)
    common = (
        identity_ok
        and document.get("model_class") == model_class
        and document.get("catalog_hash") == catalog_hash
    )
    checks = [_check(f"{label}-identity", common, str(document.get("preflight_id", "")))]
    raw_matches = document.get("matches")
    matches: list[Any] = raw_matches if isinstance(raw_matches, list) else []
    if not expect_match:
        quiet = document.get("status") == "no-match" and not matches
        checks.append(
            _check("quiet-no-match", common and quiet, str(document.get("request_hash", "")))
        )
        return checks

    target = next(
        (
            row
            for row in matches
            if isinstance(row, dict) and row.get("name") == wiki and row.get("access") == access
        ),
        None,
    )
    matched = document.get("status") == "matched" and target is not None
    checks.append(
        _check("matched-preflight", common and matched, str(document.get("preflight_id", "")))
    )
    if not isinstance(target, dict):
        checks.append(_check("authorized-surface", False, access))
        return checks
    raw_allows = target.get("allows")
    allows: list[Any] = raw_allows if isinstance(raw_allows, list) else []
    if access == "digest-only":
        digest = entry.digest.replace("\\", "/").lstrip("./")
        allowed = str(allows[0]).replace("\\", "/") if len(allows) == 1 else ""
        bounded = (
            bool(digest)
            and len(allows) == 1
            and (allowed == digest or allowed.endswith("/" + digest))
        )
    else:
        bounded = access == "full" and bool(target.get("follow_up"))
    bounded = bounded and target.get("provisional") is False
    checks.append(_check("authorized-surface", bounded, access))
    return checks


def _host_checks(document: Doc, host_id: str, model_class: str) -> tuple[list[Doc], str]:
    allowed = {"schema", "host_id", "model_class", "checks"}
    _reject_unknown(document, allowed, "host evidence")
    checks_value = document.get("checks")
    if not isinstance(checks_value, dict):
        raise RolloutError("host evidence checks must be a JSON object")
    _reject_unknown(checks_value, set(_HOST_CHECKS), "host evidence checks")
    identity = (
        document.get("schema") == HOST_EVIDENCE_SCHEMA
        and document.get("host_id") == host_id
        and document.get("model_class") == model_class
    )
    checks = [_check("host-identity", identity, _sha256(_canonical(document))[:16])]
    for name in _HOST_CHECKS:
        checks.append(
            _check(f"host-{name.replace('_', '-')}", checks_value.get(name) is True, name)
        )
    return checks, _sha256(_canonical(document))


def _evaluation_checks(document: Doc) -> list[Doc]:
    summary = document.get("summary")
    gates = document.get("gates")
    safe = isinstance(summary, dict) and all(
        summary.get(name) in (False, 0)
        for name in (
            "prompt_leak",
            "canary_leak",
            "privacy_violations",
            "model_access_violations",
        )
    )
    promoted = (
        document.get("schema_version") == EVALUATION_SCHEMA
        and document.get("status") == "promoted"
        and isinstance(gates, dict)
        and gates.get("passed") is True
        and document.get("rollback_ref") == ""
        and safe
    )
    return [_check("evaluation-promotion", promoted, str(document.get("plan_id", "")))]


def _prior_checks(
    paths: tuple[Path, ...], host_id: str, model_class: str, sequence: int, rank: int
) -> tuple[list[Doc], list[str]]:
    checks: list[Doc] = []
    promotion_ids: list[str] = []
    proofs = [_read_json(path, "prior promotion proof") for path in paths]
    ordered = sorted(proofs, key=lambda proof: int(proof.get("sequence", -1)))
    shape = len(ordered) == sequence and [proof.get("sequence") for proof in ordered] == list(
        range(sequence)
    )
    checks.append(_check("incremental-sequence", shape, str(sequence)))
    for proof in ordered:
        valid = (
            proof.get("schema_version") == PROMOTION_PROOF_SCHEMA
            and proof.get("status") == "promoted"
            and proof.get("loadable") is True
            and proof.get("host_id") == host_id
            and proof.get("model_class") == model_class
            and type(proof.get("privacy_rank")) is int
            and int(proof["privacy_rank"]) <= rank
            and isinstance(proof.get("promotion_id"), str)
        )
        promotion_id = str(proof.get("promotion_id", ""))
        promotion_ids.append(promotion_id)
        checks.append(_check("prior-promotion", valid, promotion_id))
    return checks, promotion_ids


def build_plan(inputs: RolloutInputs) -> Doc:
    if inputs.model_class not in {"local", "cloud"}:
        raise RolloutError("model class must be local or cloud")
    if not inputs.host_id.strip() or not inputs.wiki.strip():
        raise RolloutError("host id and wiki must be non-empty")
    if inputs.sequence < 0:
        raise RolloutError("rollout sequence must be non-negative")
    _state_root(inputs.state_root, (inputs.estate, inputs.wiki_root))

    entry = _target_entry(inputs.wiki_root, inputs.wiki)
    policy = effective_policy(entry)
    access = policy.access_for(inputs.model_class)
    rank = _privacy_rank(entry, access)
    card_sha256 = _card_sha256(entry)
    refs = discover_roots(inputs.estate)
    catalog = build_catalog(refs, today=inputs.today)

    host = _read_json(inputs.host_evidence, "host rollout evidence")
    positive = _read_json(inputs.preflight_evidence, "matched preflight evidence")
    negative = _read_json(inputs.no_match_evidence, "no-match preflight evidence")
    evaluation = _read_json(inputs.evaluation_evidence, "evaluation evidence")

    checks, host_sha256 = _host_checks(host, inputs.host_id, inputs.model_class)
    for label, document in (("matched", positive), ("no-match", negative)):
        request = document.get("request")
        recomputed_id = (
            run_preflight(refs, request, inputs.model_class, catalog).preflight_id
            if isinstance(request, str)
            else ""
        )
        checks.append(
            _check(
                f"{label}-preflight-current",
                recomputed_id == document.get("preflight_id"),
                recomputed_id,
            )
        )
    checks.extend(
        _preflight_checks(
            positive,
            label="matched-preflight",
            model_class=inputs.model_class,
            catalog_hash=catalog.catalog_hash,
            wiki=inputs.wiki,
            access=access,
            entry=entry,
            expect_match=True,
        )
    )
    checks.extend(
        _preflight_checks(
            negative,
            label="no-match-preflight",
            model_class=inputs.model_class,
            catalog_hash=catalog.catalog_hash,
            wiki=inputs.wiki,
            access=access,
            entry=entry,
            expect_match=False,
        )
    )
    checks.extend(_evaluation_checks(evaluation))
    findings = run_doctor(inputs.wiki_root)
    errors = sum(finding.severity == "error" for finding in findings)
    warnings = sum(finding.severity == "warning" for finding in findings)
    checks.append(_check("wiki-health", errors == 0, f"errors={errors};warnings={warnings}"))
    checks.append(_check("trusted-governance", not entry.provisional, "provisional=false"))
    checks.append(_check("loadable-access", access != "none" and rank < 4, access))
    checks.append(
        _check("governance-approval", bool(inputs.governance_approval.strip()), "present")
    )
    checks.append(_check("access-approval", bool(inputs.access_approval.strip()), "present"))
    prior_checks, prior_ids = _prior_checks(
        inputs.prior_proofs, inputs.host_id, inputs.model_class, inputs.sequence, rank
    )
    checks.extend(prior_checks)

    ready = all(check["status"] == "passed" for check in checks)
    body: Doc = {
        "schema_version": PLAN_SCHEMA,
        "status": "ready" if ready else "blocked",
        "host_id": inputs.host_id,
        "wiki": entry.name,
        "model_class": inputs.model_class,
        "effective_access": access,
        "sensitivity": policy.sensitivity,
        "privacy_rank": rank,
        "sequence": inputs.sequence,
        "prior_promotions": prior_ids,
        "wiki_card_sha256": card_sha256,
        "catalog_hash": catalog.catalog_hash,
        "evidence": {
            "host_sha256": host_sha256,
            "matched_preflight_id": str(positive.get("preflight_id", "")),
            "no_match_preflight_id": str(negative.get("preflight_id", "")),
            "evaluation_plan_id": str(evaluation.get("plan_id", "")),
            "evaluation_blind_scores_sha256": str(evaluation.get("blind_scores_sha256", "")),
            "governance_approval_sha256": _sha256(inputs.governance_approval),
            "access_approval_sha256": _sha256(inputs.access_approval),
            "doctor_errors": errors,
            "doctor_warnings": warnings,
        },
        "checks": checks,
        "boundaries": {
            "changes_host_configuration": False,
            "publishes_remotely": False,
            "changes_accounts_or_collaborators": False,
            "merges": False,
            "spends_money": False,
            "broadens_access": False,
        },
        "today": inputs.today.isoformat() if inputs.today else "",
    }
    plan_id = content_hash(_canonical(body))
    body["plan_id"] = plan_id
    body["help"] = (
        [
            "Re-run this exact rollout promote command with --apply and the returned --plan-id",
            "The host must opt in to consuming the proof; Megamind changes no host configuration",
        ]
        if ready
        else [
            "Do not promote: resolve every failed typed check and create a fresh plan",
            "A rollback-required or unsettled evaluation cannot be overridden by rollout",
        ]
    )
    return body


def _proof_from_plan(plan: Doc) -> Doc:
    body: Doc = {
        "schema_version": PROMOTION_PROOF_SCHEMA,
        "status": "promoted",
        "loadable": True,
        "host_id": plan["host_id"],
        "wiki": plan["wiki"],
        "model_class": plan["model_class"],
        "effective_access": plan["effective_access"],
        "sensitivity": plan["sensitivity"],
        "privacy_rank": plan["privacy_rank"],
        "sequence": plan["sequence"],
        "prior_promotions": plan["prior_promotions"],
        "wiki_card_sha256": plan["wiki_card_sha256"],
        "catalog_hash": plan["catalog_hash"],
        "evidence": plan["evidence"],
        "boundaries": plan["boundaries"],
        "promoted_on": plan["today"],
        "plan_id": plan["plan_id"],
    }
    body["promotion_id"] = content_hash(_canonical(body))
    return body


def _binding_key(plan: Doc) -> str:
    return content_hash(_canonical({"host_id": plan["host_id"], "wiki": plan["wiki"]}))


def _read_if_exists(path: Path, label: str) -> Doc | None:
    if not path.is_file():
        return None
    return _read_json(path, label)


def apply_plan(inputs: RolloutInputs, approved_plan_id: str) -> Doc:
    plan = build_plan(inputs)
    if plan["plan_id"] != approved_plan_id:
        raise RolloutError("rollout plan changed; review the fresh plan and use its plan id")
    state = _state_root(inputs.state_root, (inputs.estate, inputs.wiki_root))
    transaction_rel = f"transactions/promote-{plan['plan_id']}.json"
    transaction_path = _state_path(state, transaction_rel)

    if plan["status"] == "blocked":
        outcome: Doc = {
            "schema_version": RESULT_SCHEMA,
            "status": "blocked",
            "loadable": False,
            "plan_id": plan["plan_id"],
            "host_id": plan["host_id"],
            "wiki": plan["wiki"],
            "failed_checks": [
                check["check"] for check in plan["checks"] if check["status"] == "failed"
            ],
        }
        existing = _read_if_exists(transaction_path, "rollout transaction")
        blocked_transaction = {
            "schema": TRANSACTION_SCHEMA,
            "operation": "blocked",
            "state": "applied",
            "plan_id": plan["plan_id"],
            "outcome": outcome,
        }
        if existing is not None and existing != blocked_transaction:
            raise RolloutError("rollout transaction contains foreign content")
        _write_state(state, transaction_rel, blocked_transaction)
        _write_state(state, f"outcomes/{plan['plan_id']}.json", outcome)
        outcome["help"] = ["Resolve the failed checks; this blocked receipt remains auditable"]
        return outcome

    proof = _proof_from_plan(plan)
    proof_rel = f"proofs/{proof['promotion_id']}.json"
    binding_key = _binding_key(plan)
    active_rel = f"active/{binding_key}.json"
    active_path = _state_path(state, active_rel)
    existing_active = _read_if_exists(active_path, "active rollout binding")
    transaction = _read_if_exists(transaction_path, "rollout transaction")
    if transaction is not None:
        if (
            transaction.get("schema") != TRANSACTION_SCHEMA
            or transaction.get("operation") != "promote"
            or transaction.get("plan_id") != plan["plan_id"]
            or transaction.get("promotion_id") != proof["promotion_id"]
            or transaction.get("proof") != proof
            or transaction.get("binding_key") != binding_key
            or transaction.get("state") not in {"pending", "applied"}
        ):
            raise RolloutError("rollout transaction contains foreign content")
        expected_pending = dict(transaction)
        expected_pending["state"] = "pending"
        stored = _read_if_exists(_state_path(state, proof_rel), "promotion proof")
        if transaction.get("state") == "applied":
            if stored != proof or existing_active != proof:
                raise RolloutError("applied rollout transaction does not match stored proof")
            return _result(proof, "noop")
        if stored not in (None, proof) or existing_active not in (
            transaction.get("previous_active"),
            proof,
        ):
            raise RolloutError("pending rollout transaction encountered foreign content")
    else:
        if existing_active is not None and existing_active.get("status") == "promoted":
            raise RolloutError(
                "host/wiki binding is already promoted; roll it back before replacing it"
            )
        expected_pending = {
            "schema": TRANSACTION_SCHEMA,
            "operation": "promote",
            "state": "pending",
            "plan_id": plan["plan_id"],
            "promotion_id": proof["promotion_id"],
            "binding_key": binding_key,
            "proof": proof,
            "previous_active": existing_active,
        }
        _write_state(state, transaction_rel, expected_pending)

    for other_path in sorted(_state_path(state, "transactions").glob("promote-*.json")):
        if other_path == transaction_path:
            continue
        other = _read_json(other_path, "rollout transaction")
        if other.get("binding_key") != binding_key or other.get("operation") != "promote":
            continue
        current_active = _read_if_exists(active_path, "active rollout binding")
        replaced_history = (
            current_active is not None
            and current_active.get("status") == "rolled-back"
            and current_active.get("promotion_id") == other.get("promotion_id")
        )
        if not replaced_history:
            raise RolloutError("another transaction already owns this host/wiki binding")

    for relative, document in ((proof_rel, proof), (active_rel, proof)):
        existing = _read_if_exists(_state_path(state, relative), "rollout artifact")
        if existing is not None and existing != document:
            raise RolloutError("rollout target contains foreign content")
        _write_state(state, relative, document)
    applied = dict(expected_pending)
    applied["state"] = "applied"
    _write_state(state, transaction_rel, applied)
    return _result(proof, "promoted")


def _result(proof: Doc, status: str) -> Doc:
    return {
        "schema_version": RESULT_SCHEMA,
        "status": status,
        "loadable": True,
        "promotion_id": proof["promotion_id"],
        "plan_id": proof["plan_id"],
        "host_id": proof["host_id"],
        "wiki": proof["wiki"],
        "model_class": proof["model_class"],
        "effective_access": proof["effective_access"],
        "proof": proof,
        "help": [
            "The host may opt in to this exact proof; Megamind changed no host configuration",
            "Run rollout health before use and rollback immediately on drift or failure",
        ],
    }


def _find_promotion(state: Path, promotion_id: str) -> tuple[Path, Doc]:
    proof_path = _state_path(state, f"proofs/{promotion_id}.json")
    proof = _read_if_exists(proof_path, "promotion proof")
    if proof is None or proof.get("schema_version") != PROMOTION_PROOF_SCHEMA:
        raise RolloutError("promotion proof was not found")
    return proof_path, proof


def health(state_root: Path, wiki_root: Path, promotion_id: str) -> Doc:
    state = _state_root(state_root, (wiki_root,))
    _proof_path, proof = _find_promotion(state, promotion_id)
    entry = _target_entry(wiki_root, str(proof.get("wiki", "")))
    current_card = _card_sha256(entry)
    findings = run_doctor(wiki_root)
    errors = sum(finding.severity == "error" for finding in findings)
    warnings = sum(finding.severity == "warning" for finding in findings)
    active = None
    for path in sorted(_state_path(state, "active").glob("*.json")):
        candidate = _read_json(path, "active rollout binding")
        if candidate.get("promotion_id") == promotion_id:
            active = candidate
            break
    checks = [
        _check(
            "active-binding",
            active is not None and active.get("status") == "promoted",
            promotion_id,
        ),
        _check("wiki-card-current", current_card == proof.get("wiki_card_sha256"), current_card),
        _check("wiki-health", errors == 0, f"errors={errors};warnings={warnings}"),
        _check("still-trusted", not entry.provisional, "provisional=false"),
        _check(
            "access-not-widened",
            effective_policy(entry).access_for(str(proof.get("model_class")))
            == proof.get("effective_access"),
            str(proof.get("effective_access", "")),
        ),
    ]
    healthy = all(item["status"] == "passed" for item in checks)
    return {
        "schema_version": HEALTH_SCHEMA,
        "status": "healthy" if healthy else "rollback-required",
        "loadable": healthy,
        "promotion_id": promotion_id,
        "host_id": proof["host_id"],
        "wiki": proof["wiki"],
        "checks": checks,
        "help": (
            ["The host may continue consuming only the access surface named by the proof"]
            if healthy
            else ["Stop consuming this binding and create a typed rollback receipt"]
        ),
    }


def plan_rollback(
    state_root: Path, promotion_id: str, reason: str, today: date | None = None
) -> Doc:
    if not reason.strip():
        raise RolloutError("rollback requires a non-empty safe reason")
    state = _state_root(state_root)
    _proof_path, proof = _find_promotion(state, promotion_id)
    body: Doc = {
        "schema_version": ROLLBACK_PLAN_SCHEMA,
        "status": "ready",
        "promotion_id": promotion_id,
        "host_id": proof["host_id"],
        "wiki": proof["wiki"],
        "reason_sha256": _sha256(reason),
        "today": today.isoformat() if today else "",
        "effects": {
            "loadable_after": False,
            "proof_retained": True,
            "remote_changes": False,
            "host_configuration_changes": False,
        },
    }
    body["plan_id"] = content_hash(_canonical(body))
    body["help"] = ["Re-run this exact rollback with --apply and the returned --plan-id"]
    return body


def apply_rollback(
    state_root: Path,
    promotion_id: str,
    reason: str,
    approved_plan_id: str,
    today: date | None = None,
) -> Doc:
    plan = plan_rollback(state_root, promotion_id, reason, today)
    if plan["plan_id"] != approved_plan_id:
        raise RolloutError("rollback plan changed; review the fresh plan and use its plan id")
    state = _state_root(state_root)
    _proof_path, proof = _find_promotion(state, promotion_id)
    active_path: Path | None = None
    active: Doc | None = None
    active_dir = _state_path(state, "active")
    if active_dir.is_dir():
        for path in sorted(active_dir.glob("*.json")):
            candidate = _read_json(path, "active rollout binding")
            if candidate.get("promotion_id") == promotion_id:
                active_path, active = path, candidate
                break
    receipt_body: Doc = {
        "schema_version": ROLLBACK_RECEIPT_SCHEMA,
        "status": "rolled-back",
        "loadable": False,
        "promotion_id": promotion_id,
        "plan_id": plan["plan_id"],
        "host_id": proof["host_id"],
        "wiki": proof["wiki"],
        "reason_sha256": plan["reason_sha256"],
        "rolled_back_on": plan["today"],
        "proof_retained": True,
        "remote_changes": False,
        "host_configuration_changes": False,
    }
    receipt_body["receipt_id"] = content_hash(_canonical(receipt_body))
    receipt_rel = f"receipts/{receipt_body['receipt_id']}.json"
    transaction_rel = f"transactions/rollback-{plan['plan_id']}.json"
    transaction_path = _state_path(state, transaction_rel)
    existing_transaction = _read_if_exists(transaction_path, "rollback transaction")

    disarmed = dict(proof)
    disarmed["status"] = "rolled-back"
    disarmed["loadable"] = False
    disarmed["rollback_receipt_id"] = receipt_body["receipt_id"]
    pending: Doc = {
        "schema": TRANSACTION_SCHEMA,
        "operation": "rollback",
        "state": "pending",
        "plan_id": plan["plan_id"],
        "promotion_id": promotion_id,
        "active_before": proof,
        "active_after": disarmed,
        "receipt": receipt_body,
    }
    if existing_transaction is not None:
        comparable = dict(existing_transaction)
        comparable["state"] = "pending"
        if comparable != pending or existing_transaction.get("state") not in {
            "pending",
            "applied",
        }:
            raise RolloutError("rollback transaction contains foreign content")
        existing_receipt = _read_if_exists(_state_path(state, receipt_rel), "rollback receipt")
        if existing_transaction.get("state") == "applied":
            if active != disarmed or existing_receipt != receipt_body:
                raise RolloutError("applied rollback transaction does not match its receipt")
            return {**receipt_body, "status": "noop", "help": ["Rollback was already recorded"]}
        if active not in (proof, disarmed) or existing_receipt not in (None, receipt_body):
            raise RolloutError("pending rollback transaction encountered foreign content")
    else:
        if active is None:
            raise RolloutError("promotion is not the active binding")
        if active.get("status") != "promoted" or active != proof:
            raise RolloutError("active rollout binding contains foreign content")
        _write_state(state, transaction_rel, pending)
    assert active_path is not None
    _write_state(state, active_path.relative_to(state).as_posix(), disarmed)
    _write_state(state, receipt_rel, receipt_body)
    applied = dict(pending)
    applied["state"] = "applied"
    _write_state(state, transaction_rel, applied)
    return {
        **receipt_body,
        "help": ["Proof and transaction evidence were retained; the binding is disarmed"],
    }


def rollout_status(state_root: Path, full: bool = False, limit: int = 20) -> Doc:
    state = _state_root(state_root)
    active_rows: list[Doc] = []
    active_dir = _state_path(state, "active")
    if active_dir.is_dir():
        for path in sorted(active_dir.glob("*.json")):
            row = _read_json(path, "active rollout binding")
            active_rows.append(
                {
                    "promotion_id": row.get("promotion_id", ""),
                    "host_id": row.get("host_id", ""),
                    "wiki": row.get("wiki", ""),
                    "status": row.get("status", ""),
                    "loadable": row.get("loadable") is True,
                    "model_class": row.get("model_class", ""),
                    "effective_access": row.get("effective_access", ""),
                    "sequence": row.get("sequence", 0),
                }
            )
    blocked_rows: list[Doc] = []
    outcomes_dir = _state_path(state, "outcomes")
    if outcomes_dir.is_dir():
        for path in sorted(outcomes_dir.glob("*.json")):
            blocked_rows.append(_read_json(path, "blocked rollout outcome"))
    total = len(active_rows) + len(blocked_rows)
    counts = {
        "promoted": sum(row.get("status") == "promoted" for row in active_rows),
        "rolled_back": sum(row.get("status") == "rolled-back" for row in active_rows),
        "blocked": len(blocked_rows),
    }
    notes: list[str] = []
    if not full and total > limit:
        remaining = max(0, limit - len(active_rows[:limit]))
        active_rows = active_rows[:limit]
        blocked_rows = blocked_rows[:remaining]
        notes.append(f"rollout rows truncated to {limit} of {total}; re-run with --full")
    return {
        "schema_version": STATUS_SCHEMA,
        "status": "empty" if total == 0 else "ok",
        "counts": counts,
        "total": total,
        "active": active_rows,
        "blocked": blocked_rows,
        "notes": notes,
        "help": ["Promote only in nondecreasing privacy_rank order, one host/wiki proof at a time"],
    }
