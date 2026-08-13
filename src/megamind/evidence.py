"""Typed, offline evidence facts used by the research state spine.

This is intentionally not a web client or a replacement for the sibling
evidence-record lane.  It validates host receipts, derives restrictive gate
results, and keeps claim/quotation/contradiction identifiers opaque.

A claim's lifecycle, freshness, and confidence are derived here from the
frozen records.  A receipt may state a lifecycle, but that is an observation
which can only narrow the derived one, and freshness the lane never observed
stays unknown rather than becoming fresh.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from .confidence import (
    CLEAN_CORRECTION,
    FRESHNESS_STATES,
    LIFECYCLE_CAP,
    QUALITY_BASE,
    Source,
    claim_confidence,
)
from .fsops import atomic_write, content_hash, resolve_contained
from .models import LIFECYCLE_STATUSES
from .research import RESEARCH_DIR, MappingResolver, ReferenceResolver, ResearchError

EVIDENCE_SCHEMA = "megamind/evidence-record/v1"
QUOTATION_SCHEMA = "megamind/quotation/v1"
CLAIM_SCHEMA = "megamind/claim/v1"
CONTRADICTION_SCHEMA = "megamind/contradiction/v1"

EVIDENCE_DIR = RESEARCH_DIR / "evidence"
CLAIMS_DIR = RESEARCH_DIR / "claims"
CONTRADICTIONS_DIR = RESEARCH_DIR / "contradictions"

# A lifecycle is exactly as strong as the cap it buys, so "narrower" is the
# lower cap, and a status with no declared cap ranks most restrictive rather
# than landing on the unknown-lifecycle cap, which sits above proposed.
_STRICTEST_CAP = min(LIFECYCLE_CAP.values())
_LIFECYCLE_RANK = {
    status: LIFECYCLE_CAP.get(status, _STRICTEST_CAP) for status in LIFECYCLE_STATUSES
}
# Slice 1 has no step that earns a stronger lifecycle, so this is the most a
# freshly extracted claim can hold.
EXTRACTED_LIFECYCLE = "proposed"


class EvidenceResolver(ReferenceResolver, Protocol):
    """Resolver seam: this lane does not interpret sibling IDs or their content."""


class EvidenceAcceptanceError(ResearchError):
    code = "evidence_acceptance_invalid"


@dataclass(frozen=True)
class GateResult:
    name: str
    verdict: str
    reason: str = ""

    def to_data(self) -> dict[str, str]:
        return {"name": self.name, "verdict": self.verdict, "reason": self.reason}


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    origin: str
    origin_id: str
    decision: str
    quality: str
    correction_status: str
    gates: tuple[GateResult, ...]
    snapshot_sha256: str
    normalized_sha256: str
    claim_types: tuple[str, ...] = ()
    rights: dict[str, str] | None = None
    derivation: dict[str, Any] | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": EVIDENCE_SCHEMA,
            "evidence_id": self.evidence_id,
            "origin": self.origin,
            "origin_id": self.origin_id,
            "decision": self.decision,
            "quality": self.quality,
            "correction_status": self.correction_status,
            "gates": [g.to_data() for g in self.gates],
            "snapshot_sha256": self.snapshot_sha256,
            "normalized_sha256": self.normalized_sha256,
            "claim_types": list(self.claim_types),
            "rights": self.rights or {},
            "derivation": self.derivation or {},
        }


@dataclass(frozen=True)
class Quotation:
    quotation_id: str
    evidence_id: str
    selector: dict[str, Any]
    resolves: bool
    transcription_uncertainty: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": QUOTATION_SCHEMA,
            "quotation_id": self.quotation_id,
            "evidence_id": self.evidence_id,
            "selector": self.selector,
            "resolves": self.resolves,
            "transcription_uncertainty": self.transcription_uncertainty,
        }


@dataclass(frozen=True)
class Claim:
    claim_id: str
    claim_key: str
    statement: str
    supported_by: tuple[str, ...]
    contradicted_by: tuple[str, ...] = ()
    lifecycle: str = "proposed"
    confidence: float | str = "unknown"

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": CLAIM_SCHEMA,
            "claim_id": self.claim_id,
            "claim_key": self.claim_key,
            "statement": self.statement,
            "supported_by": list(self.supported_by),
            "contradicted_by": list(self.contradicted_by),
            "lifecycle": self.lifecycle,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class Contradiction:
    contradiction_id: str
    claim_ids: tuple[str, ...]
    basis: str
    resolution: str
    gap_id: str = ""

    def to_data(self) -> dict[str, Any]:
        return {
            "schema": CONTRADICTION_SCHEMA,
            "contradiction_id": self.contradiction_id,
            "claim_ids": list(self.claim_ids),
            "basis": self.basis,
            "resolution": self.resolution,
            "gap_id": self.gap_id,
        }


_HEX64 = set("0123456789abcdef")


def _sha(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(c not in _HEX64 for c in value.lower())
    ):
        raise EvidenceAcceptanceError(f"{label} must be a sha256 digest")
    return value.lower()


def _strings(data: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = data.get(name, [])
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise EvidenceAcceptanceError(f"{name} must be a list of strings")
    return tuple(value)


def _gates(
    data: Mapping[str, Any],
    origin_id: str,
    correction: str,
    snapshot: str,
    normalized: str,
    claim_types: tuple[str, ...],
) -> tuple[GateResult, ...]:
    # Host receipts are observations.  These gates are computed here from
    # typed facts; a source body cannot set quality, rights, or eligibility.
    facts = data.get("facts", {})
    if not isinstance(facts, dict):
        raise EvidenceAcceptanceError("facts must be an object")

    def fact(name: str, default: object = False) -> object:
        return facts.get(name, default)

    checks: list[GateResult] = []
    checks.append(
        GateResult(
            "retrievable",
            "pass" if fact("retrievable") is True else "fail",
            "frozen artifact receipt required",
        )
    )
    checks.append(GateResult("identity", "pass" if fact("identity_resolved") is True else "fail"))
    checks.append(GateResult("dated", "pass" if fact("dated") is True else "unknown"))
    checks.append(GateResult("attributed", "pass" if fact("attributed") is True else "fail"))
    checks.append(GateResult("publisher", "pass" if fact("publisher_resolved") is True else "fail"))
    checks.append(GateResult("rights", "pass" if fact("rights_determined") is True else "unknown"))
    checks.append(GateResult("hashed", "pass" if snapshot and normalized else "fail"))
    checks.append(
        GateResult("corrections", "pass" if correction == CLEAN_CORRECTION else "fail", correction)
    )
    checks.append(GateResult("origin_id", "pass" if origin_id else "unknown"))
    if "video" in claim_types:
        derivation = data.get("derivation", {})
        timecodes = isinstance(derivation, dict) and derivation.get("timecode_coverage") is True
        checks.append(GateResult("video-timecodes", "pass" if timecodes else "fail"))
    return tuple(checks)


def accept_evidence(data: Mapping[str, Any]) -> EvidenceRecord:
    allowed = {
        "origin",
        "origin_id",
        "quality",
        "correction_status",
        "snapshot_sha256",
        "normalized_sha256",
        "claim_types",
        "rights",
        "facts",
        "derivation",
    }
    if set(data) - allowed:
        raise EvidenceAcceptanceError("evidence record contains unknown fields")
    origin = data.get("origin")
    origin_id = data.get("origin_id", "")
    if not isinstance(origin, str) or not origin:
        raise EvidenceAcceptanceError("origin is required")
    if not isinstance(origin_id, str):
        raise EvidenceAcceptanceError("origin_id must be a string")
    quality = data.get("quality", "")
    if quality not in QUALITY_BASE:
        raise EvidenceAcceptanceError("quality must be a known confidence quality")
    correction = data.get("correction_status", CLEAN_CORRECTION)
    if correction not in {"clean", "corrected", "expression_of_concern", "retracted", "unknown"}:
        raise EvidenceAcceptanceError("unknown correction status")
    snapshot = _sha(data.get("snapshot_sha256", ""), "snapshot_sha256")
    normalized = _sha(data.get("normalized_sha256", ""), "normalized_sha256")
    claim_types = _strings(data, "claim_types")
    gates = _gates(data, origin_id, correction, snapshot, normalized, claim_types)
    failed = [g for g in gates if g.verdict != "pass"]
    if correction == "retracted":
        decision = "rejected"
    elif failed:
        decision = "deferred" if any(g.verdict == "unknown" for g in failed) else "rejected"
    else:
        decision = "accepted"
    body = {
        "origin": origin,
        "origin_id": origin_id,
        "decision": decision,
        "quality": quality,
        "correction_status": correction,
        "gates": [g.to_data() for g in gates],
        "snapshot_sha256": snapshot,
        "normalized_sha256": normalized,
        "claim_types": list(claim_types),
        "rights": data.get("rights", {}) if isinstance(data.get("rights", {}), dict) else {},
        "derivation": data.get("derivation", {})
        if isinstance(data.get("derivation", {}), dict)
        else {},
    }
    evidence_id = content_hash(json.dumps(body, sort_keys=True, separators=(",", ":")))
    return EvidenceRecord(
        evidence_id,
        origin,
        origin_id,
        decision,
        quality,
        correction,
        gates,
        snapshot,
        normalized,
        claim_types,
        body["rights"],
        body["derivation"],
    )


def make_quotation(data: Mapping[str, Any], resolver: EvidenceResolver) -> Quotation:
    if set(data) - {"evidence_id", "selector", "resolves", "transcription_uncertainty"}:
        raise EvidenceAcceptanceError("quotation contains unknown fields")
    evidence_id = data.get("evidence_id")
    selector = data.get("selector")
    if (
        not isinstance(evidence_id, str)
        or not evidence_id
        or not resolver.resolve("evidence", evidence_id)
    ):
        raise EvidenceAcceptanceError("quotation references unknown evidence")
    if not isinstance(selector, dict) or not selector:
        raise EvidenceAcceptanceError("quotation selector is required")
    resolves = data.get("resolves") is True
    if not resolves:
        raise EvidenceAcceptanceError("quotation span is not resolvable")
    body = {
        "evidence_id": evidence_id,
        "selector": selector,
        "resolves": resolves,
        "transcription_uncertainty": str(data.get("transcription_uncertainty", "")),
    }
    quotation_id = content_hash(json.dumps(body, sort_keys=True, separators=(",", ":")))
    return Quotation(
        quotation_id,
        evidence_id,
        selector,
        resolves,
        str(body["transcription_uncertainty"]),
    )


def narrower_lifecycle(first: str, second: str) -> str:
    return first if _LIFECYCLE_RANK[first] <= _LIFECYCLE_RANK[second] else second


def make_claim(
    data: Mapping[str, Any],
    resolver: EvidenceResolver,
    quotation_resolver: EvidenceResolver | None = None,
    *,
    lifecycle: str = EXTRACTED_LIFECYCLE,
) -> Claim:
    if set(data) - {"claim_key", "statement", "supported_by", "contradicted_by", "lifecycle"}:
        raise EvidenceAcceptanceError("claim contains unknown fields")
    key = data.get("claim_key")
    statement = data.get("statement")
    supports = data.get("supported_by", [])
    if not isinstance(key, str) or not key or not isinstance(statement, str) or not statement:
        raise EvidenceAcceptanceError("claim key and statement are required")
    if not isinstance(supports, list) or not all(isinstance(x, str) for x in supports):
        raise EvidenceAcceptanceError("supported_by must contain opaque references")
    for ref in supports:
        if quotation_resolver is not None:
            if not quotation_resolver.resolve("quotation", ref):
                raise EvidenceAcceptanceError("claim contains an unresolved quotation")
        elif not resolver.resolve("evidence", ref):
            raise EvidenceAcceptanceError("claim contains an unresolved evidence reference")
    contradicted = data.get("contradicted_by", [])
    if not isinstance(contradicted, list) or not all(isinstance(x, str) for x in contradicted):
        raise EvidenceAcceptanceError("contradicted_by must contain opaque references")
    if lifecycle not in _LIFECYCLE_RANK:
        raise EvidenceAcceptanceError("derived lifecycle is not a known lifecycle status")
    observed = data.get("lifecycle", EXTRACTED_LIFECYCLE)
    if not isinstance(observed, str) or observed not in _LIFECYCLE_RANK:
        raise EvidenceAcceptanceError(
            f"claim lifecycle must be one of {', '.join(LIFECYCLE_STATUSES)}"
        )
    effective = narrower_lifecycle(lifecycle, observed)
    claim_id = content_hash(
        json.dumps(
            {
                "claim_key": key,
                "statement": statement,
                "supported_by": supports,
                "contradicted_by": contradicted,
                "lifecycle": effective,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return Claim(claim_id, key, statement, tuple(supports), tuple(contradicted), effective)


def make_contradiction(data: Mapping[str, Any], resolver: EvidenceResolver) -> Contradiction:
    if set(data) - {"claim_ids", "basis", "resolution", "gap_id"}:
        raise EvidenceAcceptanceError("contradiction contains unknown fields")
    ids = data.get("claim_ids", [])
    if (
        not isinstance(ids, list)
        or len(ids) < 2
        or not all(isinstance(x, str) and resolver.resolve("claim", x) for x in ids)
    ):
        raise EvidenceAcceptanceError("contradiction claim references are invalid")
    resolution = str(data.get("resolution", "unresolved"))
    if resolution not in {
        "unresolved",
        "scope_disjoint",
        "supersession",
        "retraction",
        "authority_precedence",
    }:
        raise EvidenceAcceptanceError("invalid contradiction resolution")
    basis = str(data.get("basis", ""))
    body = {
        "claim_ids": ids,
        "basis": basis,
        "resolution": resolution,
        "gap_id": str(data.get("gap_id", "")),
    }
    return Contradiction(
        content_hash(json.dumps(body, sort_keys=True, separators=(",", ":"))),
        tuple(str(value) for value in ids),
        basis,
        resolution,
        str(body["gap_id"]),
    )


def _freeze(root: Path, relative: Path, document: Mapping[str, Any]) -> Path:
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    path = resolve_contained(root, relative)
    if path.is_file() and path.read_text(encoding="utf-8") != text:
        raise EvidenceAcceptanceError("frozen evidence artifact is immutable")
    return atomic_write(root, relative, text)


def store_claim(root: Path, claim: Claim) -> Path:
    return _freeze(root, CLAIMS_DIR / f"{claim.claim_id}.json", claim.to_data())


def store_contradiction(root: Path, contradiction: Contradiction) -> Path:
    return _freeze(
        root,
        CONTRADICTIONS_DIR / f"{contradiction.contradiction_id}.json",
        contradiction.to_data(),
    )


def _read_document(path: Path, schema: str) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise EvidenceAcceptanceError(f"frozen artifact is unreadable: {path.name}") from error
    if not isinstance(raw, dict) or raw.get("schema") != schema:
        raise EvidenceAcceptanceError(f"frozen artifact is not a {schema} document: {path.name}")
    return raw


def _record_from_data(raw: Mapping[str, Any]) -> EvidenceRecord:
    rights = raw.get("rights")
    derivation = raw.get("derivation")
    return EvidenceRecord(
        str(raw.get("evidence_id", "")),
        str(raw.get("origin", "")),
        str(raw.get("origin_id", "")),
        str(raw.get("decision", "")),
        str(raw.get("quality", "")),
        str(raw.get("correction_status", "")),
        tuple(
            GateResult(str(g.get("name", "")), str(g.get("verdict", "")), str(g.get("reason", "")))
            for g in raw.get("gates", [])
            if isinstance(g, dict)
        ),
        str(raw.get("snapshot_sha256", "")),
        str(raw.get("normalized_sha256", "")),
        tuple(str(value) for value in raw.get("claim_types", [])),
        rights if isinstance(rights, dict) else {},
        derivation if isinstance(derivation, dict) else {},
    )


def frozen_evidence(root: Path) -> dict[str, EvidenceRecord]:
    """Read back frozen evidence records; this never reads a source body."""
    directory = resolve_contained(root, EVIDENCE_DIR)
    if not directory.is_dir():
        return {}
    records: dict[str, EvidenceRecord] = {}
    for path in sorted(directory.glob("*.json")):
        raw = _read_document(path, EVIDENCE_SCHEMA)
        body = {key: value for key, value in raw.items() if key not in {"schema", "evidence_id"}}
        identifier = raw.get("evidence_id")
        if (
            not isinstance(identifier, str)
            or identifier != path.stem
            or identifier != content_hash(json.dumps(body, sort_keys=True, separators=(",", ":")))
        ):
            raise EvidenceAcceptanceError(f"frozen evidence artifact is invalid: {path.name}")
        record = _record_from_data(raw)
        records[record.evidence_id] = record
    return records


def frozen_ids(root: Path, kind: str) -> set[str]:
    """Identifiers of the frozen claim or contradiction artifacts, opaque to callers."""
    directory = {"claim": CLAIMS_DIR, "contradiction": CONTRADICTIONS_DIR}.get(kind)
    if directory is None:
        raise EvidenceAcceptanceError(f"unknown frozen artifact kind: {kind}")
    resolved = resolve_contained(root, directory)
    if not resolved.is_dir():
        return set()
    return {path.stem for path in resolved.glob("*.json")}


def unresolved_contradictions(root: Path, identifiers: Iterable[str]) -> list[str]:
    """Contradictions among the given references that no resolution rule settled."""
    unresolved: list[str] = []
    for identifier in sorted(set(identifiers)):
        path = resolve_contained(root, CONTRADICTIONS_DIR / f"{identifier}.json")
        if not path.is_file():
            raise EvidenceAcceptanceError("frozen contradiction artifact is missing")
        raw = _read_document(path, CONTRADICTION_SCHEMA)
        contradiction = make_contradiction(
            {key: value for key, value in raw.items() if key not in {"schema", "contradiction_id"}},
            MappingResolver({"claim": frozen_ids(root, "claim")}),
        )
        if (
            raw.get("contradiction_id") != identifier
            or contradiction.contradiction_id != identifier
        ):
            raise EvidenceAcceptanceError("frozen contradiction does not match its content")
        if contradiction.resolution == "unresolved":
            unresolved.append(identifier)
    return unresolved


class FrozenPacketResolver:
    """Narrow packet resolver over the frozen claim and contradiction artifacts.

    It answers only what packet derivation needs - does this reference exist,
    what confidence did this lane compute for it, is this contradiction still
    unresolved - so packet fields never come from a host receipt.
    """

    def __init__(self, root: Path):
        self.root = root
        self.claims = frozen_ids(root, "claim")
        self.contradictions = frozen_ids(root, "contradiction")

    def resolve(self, kind: str, identifier: str) -> bool:
        if kind == "claim":
            return identifier in self.claims
        if kind == "contradiction":
            return identifier in self.contradictions
        return False

    def claim_confidence(self, identifier: str) -> float | str:
        if identifier not in self.claims:
            raise EvidenceAcceptanceError("claim reference is not frozen in this vault")
        path = resolve_contained(self.root, CLAIMS_DIR / f"{identifier}.json")
        raw = _read_document(path, CLAIM_SCHEMA)
        records = frozen_evidence(self.root)
        claim = make_resolved_claim(
            {
                key: value
                for key, value in raw.items()
                if key not in {"schema", "claim_id", "confidence"}
            },
            records,
        )
        if (
            raw.get("claim_id") != identifier
            or claim.claim_id != identifier
            or raw.get("confidence") != claim.confidence
        ):
            raise EvidenceAcceptanceError("frozen claim confidence does not match frozen evidence")
        return claim.confidence

    def contradiction_is_unresolved(self, identifier: str) -> bool:
        if identifier not in self.contradictions:
            return True
        return bool(unresolved_contradictions(self.root, [identifier]))


def make_resolved_claim(data: Mapping[str, Any], records: Mapping[str, EvidenceRecord]) -> Claim:
    """Validate one host claim against frozen accepted evidence and derive its verdict.

    Support references resolve only against evidence this lane already accepted,
    so a deferred or rejected record cannot carry a claim.  Lifecycle, freshness,
    and confidence are all computed from those records rather than taken from
    the receipt, and the derived lifecycle is what the claim identity covers, so
    a forged one changes neither the identifier nor the score.
    """
    accepted = {key for key, record in records.items() if record.decision == "accepted"}
    claim = make_claim(data, MappingResolver({"evidence": accepted}))
    return replace(
        claim,
        confidence=claim_confidence_from_records(
            [records[ref] for ref in claim.supported_by],
            lifecycle=claim.lifecycle,
            contradicted=bool(claim.contradicted_by),
        ),
    )


def claim_confidence_from_records(
    records: list[EvidenceRecord],
    *,
    lifecycle: str = EXTRACTED_LIFECYCLE,
    contradicted: bool = False,
    freshness: str = "unknown",
) -> float | str:
    """Score a claim from frozen records only.

    ``freshness`` defaults to unknown and a caller may only raise it with a
    freshness it actually observed: a passing ``dated`` gate says a record
    carries a date, never that the date is recent, and this lane freezes no
    timestamp to compare against a freshness policy.
    """
    if freshness not in FRESHNESS_STATES:
        raise EvidenceAcceptanceError(f"freshness must be one of {', '.join(FRESHNESS_STATES)}")
    score = claim_confidence(
        [
            Source(r.quality, r.origin, r.decision == "accepted", r.origin_id, r.correction_status)
            for r in records
        ],
        lifecycle=lifecycle,
        freshness=freshness,
        contradicted=contradicted,
    )
    return score.render()
