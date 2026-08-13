"""Typed, offline evidence facts used by the research state spine.

This is intentionally not a web client or a replacement for the sibling
evidence-record lane.  It validates host receipts, derives restrictive gate
results, and keeps claim/quotation/contradiction identifiers opaque.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .confidence import CLEAN_CORRECTION, QUALITY_BASE, Source, claim_confidence
from .fsops import content_hash
from .research import ReferenceResolver, ResearchError

EVIDENCE_SCHEMA = "megamind/evidence-record/v1"
QUOTATION_SCHEMA = "megamind/quotation/v1"
CLAIM_SCHEMA = "megamind/claim/v1"
CONTRADICTION_SCHEMA = "megamind/contradiction/v1"


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


def make_claim(
    data: Mapping[str, Any],
    resolver: EvidenceResolver,
    quotation_resolver: EvidenceResolver | None = None,
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
    lifecycle = str(data.get("lifecycle", "proposed"))
    claim_id = content_hash(
        json.dumps(
            {
                "claim_key": key,
                "statement": statement,
                "supported_by": supports,
                "contradicted_by": contradicted,
                "lifecycle": lifecycle,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return Claim(claim_id, key, statement, tuple(supports), tuple(contradicted), lifecycle)


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


def claim_confidence_from_records(
    records: list[EvidenceRecord], *, lifecycle: str = "proposed", contradicted: bool = False
) -> float | str:
    score = claim_confidence(
        [
            Source(r.quality, r.origin, r.decision == "accepted", r.origin_id, r.correction_status)
            for r in records
        ],
        lifecycle=lifecycle,
        freshness="fresh",
        contradicted=contradicted,
    )
    return score.render()
