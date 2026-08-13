"""Deterministic governed evidence records and provenance.

This module consumes frozen, host-supplied facts only.  It never retrieves a
URL, interprets source prose as control data, or assigns a verdict supplied by
a model.  Every validator refuses unknown fields and represents acceptance,
quotation, and contradiction outcomes with typed values.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .confidence import CLEAN_CORRECTION, QUALITY_BASE, Source, claim_confidence
from .fsops import (
    MEGAMIND_DIR,
    atomic_write,
    content_hash,
    identity_bytes,
    resolve_contained,
)
from .policy import (
    CLAIM_TYPES,
    SOURCE_CLASSES,
    ResearchPolicy,
    admits_claim_type,
    tier_for_facts,
)

EVIDENCE_SCHEMA = "megamind/evidence-record/v1"
QUOTATION_SCHEMA = "megamind/quotation/v1"
CLAIM_SCHEMA = "megamind/claim/v1"
CONTRADICTION_SCHEMA = "megamind/contradiction/v1"

GATE_VERDICTS = ("pass", "fail", "unknown")
DECISIONS = ("accepted", "rejected", "deferred")
SOURCE_CLASS_SET = set(SOURCE_CLASSES)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
QUOTE_CEILING_CHARS = 1000

# The identity of every stored document covers only part of its body: the rest
# is governed state Megamind itself derives (acceptance verdicts, resolution
# outcomes, lifecycle).  Immutability therefore applies to the identity-bearing
# fields, and a rewrite that touches anything else is refused.
MUTABLE_FIELDS: dict[str, frozenset[str]] = {
    "evidence": frozenset({"acceptance"}),
    "quotations": frozenset({"resolves", "resolved_at"}),
    "claims": frozenset({"lifecycle", "supported_by", "contradicted_by", "superseded_by"}),
    "contradictions": frozenset({"resolution", "resolution_detail", "updated"}),
}

# One stored record per entry: its identifier, the validated record when it
# reads, and the problem that stopped it when it does not.
ScanResult = list[tuple[str, dict[str, Any] | None, str]]


class EvidenceError(ValueError):
    """Malformed or unsafe evidence input."""

    code = "evidence_invalid"


def _map(label: str, value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvidenceError(f"{label} must be an object")
    return dict(value)


def _unknown(label: str, value: Mapping[str, object], allowed: set[str]) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise EvidenceError(f"{label} has unknown field(s): {', '.join(extra)}")


def _str(label: str, value: object, *, nonempty: bool = False, limit: int = 1000) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise EvidenceError(f"{label} must be {'a non-empty ' if nonempty else 'a '}string")
    if len(value) > limit:
        raise EvidenceError(f"{label} exceeds its length limit")
    return value


def _strings(label: str, value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise EvidenceError(f"{label} must be a list of strings")
    return list(value)


def _date(label: str, value: object, *, required: bool = True) -> str:
    if value == "" and not required:
        return ""
    if not isinstance(value, str):
        raise EvidenceError(f"{label} must be an ISO date")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise EvidenceError(f"{label} must be an ISO date") from error


def _timestamp(label: str, value: object) -> str:
    value = _str(label, value, nonempty=True, limit=80)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError(f"{label} must be an ISO timestamp") from error
    return value


def _hash(label: str, value: object) -> str:
    value = _str(label, value, nonempty=True, limit=64).lower()
    if not SHA256.fullmatch(value):
        raise EvidenceError(f"{label} must be a SHA-256 digest")
    return value


def _canonical_url(value: object) -> str:
    value = _str("canonical_url", value, nonempty=True, limit=2000)
    if not re.match(r"^https?://[^\s]+$", value):
        raise EvidenceError("canonical_url must be an http or https URL")
    return value


def _gates(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise EvidenceError("acceptance gates must be a list")
    result: list[dict[str, str]] = []
    for index, item in enumerate(value):
        data = _map(f"acceptance gates[{index}]", item)
        _unknown(f"acceptance gates[{index}]", data, {"gate", "verdict", "detail"})
        gate = _str("acceptance gate name", data.get("gate"), nonempty=True, limit=20)
        verdict = _str("acceptance gate verdict", data.get("verdict"), nonempty=True, limit=10)
        if verdict not in GATE_VERDICTS:
            raise EvidenceError("acceptance gate verdict is invalid")
        result.append(
            {
                "gate": gate,
                "verdict": verdict,
                "detail": _str("acceptance gate detail", data.get("detail", ""), limit=300),
            }
        )
    if len({item["gate"] for item in result}) != len(result):
        raise EvidenceError("acceptance gates must be unique")
    return result


def _validate_evidence(raw: object) -> dict[str, Any]:
    data = _map("evidence record", raw)
    allowed = {
        "schema",
        "evidence_id",
        "source_class",
        "origin_id",
        "canonical_url",
        "final_url",
        "redirect_chain",
        "identifiers",
        "retrieved_at",
        "published_at",
        "published_precision",
        "updated_at",
        "update_signal",
        "evidence_as_of",
        "authors",
        "publisher",
        "jurisdiction",
        "rights",
        "snapshot",
        "corrections",
        "injection_scan",
        "acceptance",
        "revalidate_after",
        "derivation",
    }
    _unknown("evidence record", data, allowed)
    if data.get("schema") != EVIDENCE_SCHEMA:
        raise EvidenceError(f"evidence record schema must be {EVIDENCE_SCHEMA}")
    source_class = _str("source_class", data.get("source_class"), nonempty=True, limit=40)
    if source_class not in SOURCE_CLASS_SET:
        raise EvidenceError("source_class is invalid")
    canonical = _canonical_url(data.get("canonical_url"))
    final_url = _canonical_url(data.get("final_url", canonical))
    origin_id = _str("origin_id", data.get("origin_id"), nonempty=True, limit=300)
    normalized = _hash(
        "snapshot.normalized_sha256",
        _map("snapshot", data.get("snapshot", {})).get("normalized_sha256"),
    )
    expected_id = content_hash(canonical + normalized)
    if data.get("evidence_id") != expected_id:
        raise EvidenceError("evidence_id does not match canonical_url and normalized snapshot")
    identifiers = _map("identifiers", data.get("identifiers", {}))
    _unknown("identifiers", identifiers, {"doi", "pmid", "celex", "video_id", "accession"})
    for key, value in identifiers.items():
        _str(f"identifiers.{key}", value, limit=300)
    published_precision = _str(
        "published_precision", data.get("published_precision", "unknown"), limit=20
    )
    if published_precision not in {"exact", "month", "year", "unknown"}:
        raise EvidenceError("published_precision is invalid")
    published = data.get("published_at", "")
    if published_precision == "unknown":
        if published not in ("", None):
            raise EvidenceError("unknown publication precision requires an empty published_at")
        published = ""
    else:
        if not isinstance(published, str):
            raise EvidenceError("published_at must be a string")
        pattern = {"exact": r"^\d{4}-\d{2}-\d{2}$", "month": r"^\d{4}-\d{2}$", "year": r"^\d{4}$"}[
            published_precision
        ]
        if not re.fullmatch(pattern, published):
            raise EvidenceError("published_at does not match published_precision")
    retrieved = _timestamp("retrieved_at", data.get("retrieved_at"))
    updated = data.get("updated_at", "")
    if updated:
        updated = _timestamp("updated_at", updated)
    update_signal = _str("update_signal", data.get("update_signal", "none"), limit=40)
    as_of = data.get("evidence_as_of", "")
    as_of = _date("evidence_as_of", as_of, required=False) if as_of else ""
    authors_raw = data.get("authors", [])
    if not isinstance(authors_raw, list):
        raise EvidenceError("authors must be a list")
    authors: list[dict[str, str]] = []
    for index, item in enumerate(authors_raw):
        author = _map(f"authors[{index}]", item)
        _unknown(f"authors[{index}]", author, {"name", "identity_basis"})
        basis = _str("author identity_basis", author.get("identity_basis"), nonempty=True, limit=30)
        if basis not in {"orcid", "byline", "org", "unattributed"}:
            raise EvidenceError("author identity_basis is invalid")
        authors.append(
            {
                "name": _str("author name", author.get("name"), nonempty=True, limit=300),
                "identity_basis": basis,
            }
        )
    publisher = _map("publisher", data.get("publisher", {}))
    _unknown("publisher", publisher, {"name", "basis"})
    publisher_basis = _str("publisher basis", publisher.get("basis"), nonempty=True, limit=30)
    if publisher_basis not in {"authority-registry", "host", "unresolved"}:
        raise EvidenceError("publisher basis is invalid")
    rights = _map("rights", data.get("rights", {}))
    _unknown("rights", rights, {"license", "quote_policy", "snapshot_policy"})
    quote_policy = _str("rights quote_policy", rights.get("quote_policy"), nonempty=True, limit=30)
    snapshot_policy = _str(
        "rights snapshot_policy", rights.get("snapshot_policy"), nonempty=True, limit=40
    )
    if quote_policy not in {"quote-free", "quote-bounded", "no-quote"} or snapshot_policy not in {
        "local-snapshot-allowed",
        "no-store",
    }:
        raise EvidenceError("rights policy is invalid")
    snapshot = _map("snapshot", data.get("snapshot", {}))
    _unknown(
        "snapshot",
        snapshot,
        {"sha256", "normalized_sha256", "bytes", "content_type", "archive_url", "archive_datetime"},
    )
    snapshot_hash = _hash("snapshot.sha256", snapshot.get("sha256"))
    normalized_hash = _hash("snapshot.normalized_sha256", snapshot.get("normalized_sha256"))
    size = snapshot.get("bytes", 0)
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise EvidenceError("snapshot.bytes must be a non-negative integer")
    content_type = _str("snapshot.content_type", snapshot.get("content_type", ""), limit=100)
    archive_url = snapshot.get("archive_url", "")
    if archive_url:
        archive_url = _canonical_url(archive_url)
    archive_datetime = snapshot.get("archive_datetime", "")
    if archive_datetime:
        archive_datetime = _timestamp("snapshot.archive_datetime", archive_datetime)
    corrections = _map("corrections", data.get("corrections", {}))
    _unknown("corrections", corrections, {"checked_at", "method", "status", "notice_ids"})
    correction_status = _str(
        "corrections.status", corrections.get("status"), nonempty=True, limit=30
    )
    if correction_status not in {
        "clean",
        "corrected",
        "expression_of_concern",
        "retracted",
        "unknown",
    }:
        raise EvidenceError("corrections.status is invalid")
    checked = _date("corrections.checked_at", corrections.get("checked_at"))
    notices = _strings("corrections.notice_ids", corrections.get("notice_ids", []))
    injection = _map("injection_scan", data.get("injection_scan", {}))
    _unknown("injection_scan", injection, {"verdict", "markers"})
    injection_verdict = _str("injection_scan.verdict", injection.get("verdict", "clean"), limit=20)
    if injection_verdict not in {"clean", "suspicious", "unknown"}:
        raise EvidenceError("injection_scan.verdict is invalid")
    markers = _strings("injection_scan.markers", injection.get("markers", []))
    acceptance = _map("acceptance", data.get("acceptance", {}))
    _unknown("acceptance", acceptance, {"decision", "tier", "quality", "gates", "failure"})
    decision = _str("acceptance.decision", acceptance.get("decision"), nonempty=True, limit=20)
    if decision not in DECISIONS:
        raise EvidenceError("acceptance.decision is invalid")
    tier = acceptance.get("tier")
    if tier is not None and (isinstance(tier, bool) or not isinstance(tier, int) or tier <= 0):
        raise EvidenceError("acceptance.tier must be positive or null")
    quality = acceptance.get("quality", "")
    if quality and quality not in QUALITY_BASE:
        raise EvidenceError("acceptance.quality is invalid")
    if decision == "accepted" and (not quality or tier is None):
        raise EvidenceError("accepted evidence requires a derived acceptance tier and quality")
    gates = _gates(acceptance.get("gates", []))
    failure = _str("acceptance.failure", acceptance.get("failure", ""), limit=300)
    revalidate = _date("revalidate_after", data.get("revalidate_after"))
    derivation = data.get("derivation")
    if derivation is not None:
        derivation = _map("derivation", derivation)
        _unknown(
            "derivation",
            derivation,
            {
                "video_id",
                "channel",
                "upload_date",
                "provider",
                "language",
                "timecode_coverage",
                "transcript_sha256",
                "caption_source",
            },
        )
        for key, value in derivation.items():
            _str(f"derivation.{key}", value, limit=300)
    if source_class == "video" and derivation is None:
        raise EvidenceError("video evidence requires derivation lineage")
    return {
        "schema": EVIDENCE_SCHEMA,
        "evidence_id": expected_id,
        "source_class": source_class,
        "origin_id": origin_id,
        "canonical_url": canonical,
        "final_url": final_url,
        "redirect_chain": [
            _canonical_url(item)
            for item in _strings("redirect_chain", data.get("redirect_chain", []))
        ],
        "identifiers": identifiers,
        "retrieved_at": retrieved,
        "published_at": published,
        "published_precision": published_precision,
        "updated_at": updated,
        "update_signal": update_signal,
        "evidence_as_of": as_of,
        "authors": authors,
        "publisher": {
            "name": _str("publisher name", publisher.get("name"), nonempty=True, limit=300),
            "basis": publisher_basis,
        },
        "jurisdiction": _str("jurisdiction", data.get("jurisdiction", ""), limit=100),
        "rights": {
            "license": _str("rights license", rights.get("license", ""), limit=300),
            "quote_policy": quote_policy,
            "snapshot_policy": snapshot_policy,
        },
        "snapshot": {
            "sha256": snapshot_hash,
            "normalized_sha256": normalized_hash,
            "bytes": size,
            "content_type": content_type,
            "archive_url": archive_url,
            "archive_datetime": archive_datetime,
        },
        "corrections": {
            "checked_at": checked,
            "method": _str(
                "corrections.method", corrections.get("method"), nonempty=True, limit=100
            ),
            "status": correction_status,
            "notice_ids": notices,
        },
        "injection_scan": {"verdict": injection_verdict, "markers": markers},
        "acceptance": {
            "decision": decision,
            "tier": tier,
            "quality": quality,
            "gates": gates,
            "failure": failure,
        },
        "revalidate_after": revalidate,
        **({"derivation": derivation} if derivation is not None else {}),
    }


def validate_evidence_record(raw: object) -> dict[str, Any]:
    """Validate and return a canonical evidence record."""
    return _validate_evidence(raw)


def _quotation_verdict(quotations: Sequence[Mapping[str, Any]]) -> str:
    """G10 is only a pass once a hash-bound span actually re-resolved."""
    if not quotations:
        return "unknown"
    return "pass" if any(item.get("resolves") for item in quotations) else "fail"


def _publisher_verdict(publisher: Mapping[str, Any], policy: ResearchPolicy | None) -> str:
    """A declared registry authority must appear in the wiki's accepted list."""
    if publisher["basis"] == "unresolved":
        return "fail"
    authorities = policy.accepted_authorities if policy is not None else ()
    if (
        authorities
        and publisher["basis"] == "authority-registry"
        and publisher["name"] not in authorities
    ):
        return "fail"
    return "pass"


def acceptance_gates(
    record: Mapping[str, Any],
    policy: ResearchPolicy | None,
    claim_type: str = "fact",
    quotations: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, str]]:
    """Derive common and class gates from facts; never trust incoming verdicts.

    ``quotations`` are the already-validated hash-bound spans recorded against
    this evidence record.  They are the only thing that can turn G10 from
    ``unknown`` into a verdict, so acceptance stays claim-bound.
    """
    if claim_type not in CLAIM_TYPES:
        raise EvidenceError("claim_type is invalid")
    source_class = str(record["source_class"])
    snapshot = record["snapshot"]
    corrections = record["corrections"]
    publisher = record["publisher"]
    tier = tier_for_facts(
        policy,
        {
            "publisher": publisher["name"],
            "jurisdiction": record["jurisdiction"],
            "document_type": source_class,
        },
        claim_type,
    )
    gates = [
        {"gate": "G1", "verdict": "pass", "detail": "frozen retrieval facts present"},
        {"gate": "G2", "verdict": "pass", "detail": "canonical identity present"},
        {
            "gate": "G3",
            "verdict": "pass" if record["published_precision"] != "unknown" else "unknown",
            "detail": "publication date precision",
        },
        {
            "gate": "G4",
            "verdict": "pass" if record["update_signal"] else "unknown",
            "detail": "update signal",
        },
        {
            "gate": "G5",
            "verdict": "pass"
            if record["authors"]
            and all(a["identity_basis"] != "unattributed" for a in record["authors"])
            else "fail",
            "detail": "attributed author",
        },
        {
            "gate": "G6",
            "verdict": _publisher_verdict(publisher, policy),
            "detail": "publisher identity",
        },
        {"gate": "G7", "verdict": "pass", "detail": "rights posture determined"},
        {
            "gate": "G8",
            "verdict": "pass" if snapshot["sha256"] and snapshot["normalized_sha256"] else "fail",
            "detail": "snapshot hashes",
        },
        {
            "gate": "G9",
            "verdict": "pass" if corrections["status"] == CLEAN_CORRECTION else "fail",
            "detail": "corrections status",
        },
        {
            "gate": "G10",
            "verdict": _quotation_verdict(quotations),
            "detail": "quotation validation is claim-bound",
        },
        {
            "gate": "G11",
            "verdict": "pass"
            if tier is not None and admits_claim_type(policy, source_class, claim_type)
            else "unknown",
            "detail": "wiki research policy",
        },
        {
            "gate": "G12",
            "verdict": "pass" if record["origin_id"] else "unknown",
            "detail": "derived origin identity",
        },
    ]
    if source_class == "video":
        derivation = record.get("derivation", {})
        gates.extend(
            [
                {
                    "gate": "V1",
                    "verdict": "pass" if derivation.get("video_id") else "fail",
                    "detail": "video identity",
                },
                {
                    "gate": "V2",
                    "verdict": "pass" if derivation.get("upload_date") else "unknown",
                    "detail": "video publication date",
                },
                {
                    "gate": "V3",
                    "verdict": "pass" if derivation.get("caption_source") else "unknown",
                    "detail": "caption provenance",
                },
                {
                    "gate": "V4",
                    "verdict": "pass" if derivation.get("timecode_coverage") else "unknown",
                    "detail": "timecode coverage",
                },
                {
                    "gate": "V6",
                    "verdict": "pass" if derivation.get("transcript_sha256") else "unknown",
                    "detail": "transcript integrity",
                },
            ]
        )
    return gates


def decide_acceptance(gates: Sequence[Mapping[str, str]]) -> tuple[str, str]:
    failures = [gate["gate"] for gate in gates if gate["verdict"] == "fail"]
    unknowns = [gate["gate"] for gate in gates if gate["verdict"] == "unknown"]
    if failures:
        return "rejected", "acceptance_gate_failed:" + ",".join(failures)
    if unknowns:
        return "deferred", "acceptance_gate_unknown:" + ",".join(unknowns)
    return "accepted", ""


def _selector_quote(label: str, value: object, limit: int = QUOTE_CEILING_CHARS) -> dict[str, str]:
    data = _map(label, value)
    _unknown(label, data, {"exact", "prefix", "suffix"})
    quote = {
        key: _str(f"{label}.{key}", data.get(key, ""), limit=limit)
        for key in ("exact", "prefix", "suffix")
    }
    # An empty exact selector resolves against any frozen text and therefore
    # proves nothing about the snapshot it claims to cite.
    if not quote["exact"].strip():
        raise EvidenceError(f"{label}.exact must be a non-empty string")
    return quote


def validate_quotation(
    raw: object,
    normalized_text: str | None = None,
    *,
    quote_ceiling_chars: int = QUOTE_CEILING_CHARS,
) -> dict[str, Any]:
    data = _map("quotation", raw)
    _unknown(
        "quotation",
        data,
        {
            "schema",
            "quotation_id",
            "evidence_id",
            "against_hash",
            "quote",
            "position",
            "media",
            "transcription_uncertainty",
            "resolved_at",
            "resolves",
        },
    )
    if data.get("schema") != QUOTATION_SCHEMA:
        raise EvidenceError(f"quotation schema must be {QUOTATION_SCHEMA}")
    quotation_id = _str("quotation_id", data.get("quotation_id"), nonempty=True, limit=64)
    evidence_id = _str("quotation evidence_id", data.get("evidence_id"), nonempty=True, limit=64)
    against_hash = _hash("quotation against_hash", data.get("against_hash"))
    if quote_ceiling_chars <= 0:
        raise EvidenceError("quotation ceiling must be positive")
    quote = _selector_quote("quotation quote", data.get("quote"), quote_ceiling_chars)
    position = _map("quotation position", data.get("position", {}))
    _unknown("quotation position", position, {"start", "end"})
    start_value, end_value = position.get("start"), position.get("end")
    if (
        isinstance(start_value, bool)
        or not isinstance(start_value, int)
        or start_value < 0
        or isinstance(end_value, bool)
        or not isinstance(end_value, int)
        or end_value <= start_value
    ):
        raise EvidenceError("quotation position must be a non-empty half-open range")
    start, end = start_value, end_value
    media = _map("quotation media", data.get("media", {}))
    _unknown("quotation media", media, {"t_start", "t_end", "segment_ids"})
    for key in ("t_start", "t_end"):
        value = media.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0
        ):
            raise EvidenceError("quotation media timecodes must be non-negative numbers")
    segments = _strings("quotation media segment_ids", media.get("segment_ids", []))
    uncertainty = _str(
        "transcription_uncertainty",
        data.get("transcription_uncertainty", "not-applicable"),
        limit=30,
    )
    if uncertainty not in {"not-applicable", "low", "unknown"}:
        raise EvidenceError("transcription_uncertainty is invalid")
    resolved_at = _date("quotation resolved_at", data.get("resolved_at"))
    resolves = data.get("resolves")
    if not isinstance(resolves, bool):
        raise EvidenceError("quotation resolves must be boolean")
    actual = False
    if normalized_text is not None:
        actual_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
        if actual_hash != against_hash:
            raise EvidenceError("quotation text does not match against_hash")
        # Python slicing clamps silently, so an out-of-range span would other-
        # wise read as an empty string that matches an empty selector.
        if end > len(normalized_text):
            raise EvidenceError("quotation position runs past the frozen normalized text")
        exact = normalized_text[start:end]
        actual = (
            exact == quote["exact"]
            and normalized_text[max(0, start - len(quote["prefix"])) : start].endswith(
                quote["prefix"]
            )
            and normalized_text[end : end + len(quote["suffix"])] == quote["suffix"]
        )
        if actual != resolves:
            raise EvidenceError("quotation resolves does not match the supplied text")
    elif resolves:
        # The host must have supplied frozen text at recording time.  A later
        # read can validate the binding and the persisted resolution fact, but
        # cannot require quarantine bytes to remain inside the wiki.
        actual = True
    expected = content_hash(
        _stable(
            {
                "evidence_id": evidence_id,
                "against_hash": against_hash,
                "quote": quote,
                "position": {"start": start, "end": end},
            }
        )
    )
    if quotation_id != expected:
        raise EvidenceError("quotation_id does not match its selectors")
    return {
        "schema": QUOTATION_SCHEMA,
        "quotation_id": quotation_id,
        "evidence_id": evidence_id,
        "against_hash": against_hash,
        "quote": quote,
        "position": {"start": start, "end": end},
        "media": {
            "t_start": media.get("t_start"),
            "t_end": media.get("t_end"),
            "segment_ids": segments,
        },
        "transcription_uncertainty": uncertainty,
        "resolved_at": resolved_at,
        "resolves": actual,
    }


def validate_claim(
    raw: object, *, quotations: Mapping[str, Mapping[str, Any]] | None = None
) -> dict[str, Any]:
    data = _map("claim", raw)
    _unknown(
        "claim",
        data,
        {
            "schema",
            "claim_id",
            "claim_key",
            "type",
            "statement",
            "qualifiers",
            "lifecycle",
            "supported_by",
            "contradicted_by",
            "superseded_by",
        },
    )
    if data.get("schema") != CLAIM_SCHEMA:
        raise EvidenceError(f"claim schema must be {CLAIM_SCHEMA}")
    claim_key = _str("claim_key", data.get("claim_key"), nonempty=True, limit=300)
    statement = _str("claim statement", data.get("statement"), nonempty=True, limit=2000)
    claim_type = _str("claim type", data.get("type"), nonempty=True, limit=30)
    if claim_type not in CLAIM_TYPES:
        raise EvidenceError("claim type is invalid")
    qualifiers = _map("claim qualifiers", data.get("qualifiers", {}))
    _unknown(
        "claim qualifiers",
        qualifiers,
        {"population", "excludes", "jurisdiction", "as_of", "version"},
    )
    normalized_qualifiers: dict[str, object] = {}
    for key, value in qualifiers.items():
        if key == "excludes":
            normalized_qualifiers[key] = _strings("claim qualifiers.excludes", value)
        else:
            normalized_qualifiers[key] = _str(f"claim qualifiers.{key}", value, limit=300)
    lifecycle = _str("claim lifecycle", data.get("lifecycle"), nonempty=True, limit=20)
    if lifecycle not in {"proposed", "confirmed", "active", "shaky", "rejected", "superseded"}:
        raise EvidenceError("claim lifecycle is invalid")
    supported_raw = data.get("supported_by", [])
    if not isinstance(supported_raw, list):
        raise EvidenceError("claim supported_by must be a list")
    supported: list[dict[str, str]] = []
    for index, item in enumerate(supported_raw):
        support = _map(f"claim supported_by[{index}]", item)
        _unknown(f"claim supported_by[{index}]", support, {"evidence_id", "quotation_id", "role"})
        role = _str("claim support role", support.get("role"), nonempty=True, limit=30)
        if role not in {"primary", "corroborating", "context"}:
            raise EvidenceError("claim support role is invalid")
        quotation_id = _str(
            "claim quotation_id", support.get("quotation_id"), nonempty=True, limit=64
        )
        if quotations is not None and quotation_id not in quotations:
            raise EvidenceError("claim references an unknown quotation")
        supported.append(
            {
                "evidence_id": _str(
                    "claim evidence_id", support.get("evidence_id"), nonempty=True, limit=64
                ),
                "quotation_id": quotation_id,
                "role": role,
            }
        )
    contradicted = _strings("claim contradicted_by", data.get("contradicted_by", []))
    superseded = data.get("superseded_by", "")
    superseded = _str("claim superseded_by", superseded, limit=64)
    expected = content_hash(
        _stable(
            {"claim_key": claim_key, "statement": statement, "qualifiers": normalized_qualifiers}
        )
    )
    if data.get("claim_id") != expected:
        raise EvidenceError("claim_id does not match claim content")
    if lifecycle == "active" and not supported:
        raise EvidenceError("active claim requires citation support")
    if (
        lifecycle == "active"
        and quotations is not None
        and any(not quotations[item["quotation_id"]].get("resolves", False) for item in supported)
    ):
        raise EvidenceError("active claim requires resolvable quotation spans")
    return {
        "schema": CLAIM_SCHEMA,
        "claim_id": expected,
        "claim_key": claim_key,
        "type": claim_type,
        "statement": statement,
        "qualifiers": normalized_qualifiers,
        "lifecycle": lifecycle,
        "supported_by": supported,
        "contradicted_by": contradicted,
        "superseded_by": superseded,
    }


def validate_contradiction(raw: object) -> dict[str, Any]:
    data = _map("contradiction", raw)
    _unknown(
        "contradiction",
        data,
        {
            "schema",
            "contradiction_id",
            "claim_ids",
            "claim_key",
            "basis",
            "resolution",
            "resolution_detail",
            "gap_id",
            "created",
            "updated",
        },
    )
    if data.get("schema") != CONTRADICTION_SCHEMA:
        raise EvidenceError(f"contradiction schema must be {CONTRADICTION_SCHEMA}")
    ids = _strings("contradiction claim_ids", data.get("claim_ids"))
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise EvidenceError("contradiction needs at least two distinct claim ids")
    ids = sorted(ids)
    basis = _str("contradiction basis", data.get("basis"), nonempty=True, limit=40)
    if basis not in {"incompatible-value", "incompatible-polarity", "incompatible-scope"}:
        raise EvidenceError("contradiction basis is invalid")
    resolution = _str("contradiction resolution", data.get("resolution"), nonempty=True, limit=30)
    if resolution not in {
        "unresolved",
        "scope_disjoint",
        "supersession",
        "retraction",
        "authority_precedence",
    }:
        raise EvidenceError("contradiction resolution is invalid")
    created = _date("contradiction created", data.get("created"))
    updated = _date("contradiction updated", data.get("updated"))
    expected = content_hash(_stable({"claim_ids": ids, "claim_key": data.get("claim_key", "")}))
    if data.get("contradiction_id") != expected:
        raise EvidenceError("contradiction_id does not match claim ids")
    return {
        "schema": CONTRADICTION_SCHEMA,
        "contradiction_id": expected,
        "claim_ids": ids,
        "claim_key": _str(
            "contradiction claim_key", data.get("claim_key"), nonempty=True, limit=300
        ),
        "basis": basis,
        "resolution": resolution,
        "resolution_detail": _str(
            "contradiction resolution_detail", data.get("resolution_detail", ""), limit=500
        ),
        "gap_id": _str("contradiction gap_id", data.get("gap_id", ""), limit=64),
        "created": created,
        "updated": updated,
    }


def reconcile_claims(
    claims: Sequence[Mapping[str, Any]], *, today: str, gap_id: str = ""
) -> list[dict[str, Any]]:
    """Create visible unresolved contradictions without choosing a winner."""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for raw in claims:
        claim = validate_claim(raw)
        groups.setdefault(str(claim["claim_key"]), []).append(claim)
    records: list[dict[str, Any]] = []
    for claim_key, group in sorted(groups.items()):
        statements = {str(item["statement"]) for item in group}
        if len(group) < 2 or len(statements) < 2:
            continue
        ids = sorted(str(item["claim_id"]) for item in group)
        record = {
            "schema": CONTRADICTION_SCHEMA,
            "contradiction_id": content_hash(_stable({"claim_ids": ids, "claim_key": claim_key})),
            "claim_ids": ids,
            "claim_key": claim_key,
            "basis": "incompatible-value",
            "resolution": "unresolved",
            "resolution_detail": "claims share a key but assert different statements",
            "gap_id": gap_id,
            "created": today,
            "updated": today,
        }
        records.append(validate_contradiction(record))
    return records


def score_claim(
    claim: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    *,
    freshness: str = "fresh",
    contradicted: bool = False,
) -> Any:
    """Use the existing confidence constants after evidence acceptance only."""
    sources: list[Source] = []
    for support in claim.get("supported_by", []):
        record = evidence.get(support["evidence_id"])
        if record is None:
            continue
        acceptance = record["acceptance"]
        quality = str(acceptance.get("quality", ""))
        sources.append(
            Source(
                quality,
                record["canonical_url"],
                acceptance.get("decision") == "accepted" and quality in QUALITY_BASE,
                record["origin_id"],
                record["corrections"]["status"],
            )
        )
    return claim_confidence(
        sources,
        lifecycle=str(claim.get("lifecycle", "")),
        freshness=freshness,
        contradicted=contradicted,
    )


def _stable(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class EvidenceStore:
    """Content-addressed immutable evidence documents under a vault root."""

    def __init__(self, root: Path):
        self.root = root

    def _path(self, kind: str, identifier: str) -> Path:
        if kind not in {"evidence", "quotations", "claims", "contradictions"}:
            raise EvidenceError("unknown evidence store kind")
        return Path(MEGAMIND_DIR) / "evidence" / kind / f"{identifier}.json"

    def put(
        self,
        kind: str,
        data: Mapping[str, Any],
        normalized_text: str | None = None,
        *,
        quote_ceiling_chars: int = QUOTE_CEILING_CHARS,
    ) -> Path:
        validators = {
            "evidence": validate_evidence_record,
            "quotations": validate_quotation,
            "claims": validate_claim,
            "contradictions": validate_contradiction,
        }
        if kind == "quotations":
            canonical = validate_quotation(
                data, normalized_text, quote_ceiling_chars=quote_ceiling_chars
            )
        else:
            canonical = validators[kind](data)
        identifier = str(
            canonical.get(
                {
                    "evidence": "evidence_id",
                    "quotations": "quotation_id",
                    "claims": "claim_id",
                    "contradictions": "contradiction_id",
                }[kind]
            )
        )
        path = resolve_contained(self.root, self._path(kind, identifier))
        text = json.dumps(canonical, sort_keys=True, indent=2) + "\n"
        if path.is_file():
            mutable = MUTABLE_FIELDS[kind]
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise EvidenceError("stored evidence record is not valid JSON") from error
            if identity_bytes(existing, mutable) != identity_bytes(canonical, mutable):
                raise EvidenceError(
                    "content-addressed evidence record already exists with different bytes"
                )
        atomic_write(self.root, self._path(kind, identifier), text)
        return path

    def get(self, kind: str, identifier: str) -> dict[str, Any]:
        path = resolve_contained(self.root, self._path(kind, identifier))
        if not path.is_file():
            raise EvidenceError("evidence record not found")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise EvidenceError("evidence record is not valid JSON") from error
        validators = {
            "evidence": validate_evidence_record,
            "quotations": validate_quotation,
            "claims": validate_claim,
            "contradictions": validate_contradiction,
        }
        return validators[kind](data)

    def list(self, kind: str) -> list[dict[str, Any]]:
        directory = resolve_contained(self.root, Path(MEGAMIND_DIR) / "evidence" / kind)
        if not directory.is_dir():
            return []
        return [self.get(kind, path.stem) for path in sorted(directory.glob("*.json"))]

    def scan(self, kind: str) -> ScanResult:
        """List every record, reporting rather than raising on a bad one.

        A projection over the store must stay usable when one file is corrupt:
        an unreadable record is the exact thing the reader needs to be told
        about, so it is returned as a typed problem instead of aborting the
        whole read. ``list`` stays strict for callers that need all-or-nothing.
        """
        directory = resolve_contained(self.root, Path(MEGAMIND_DIR) / "evidence" / kind)
        if not directory.is_dir():
            return []
        results: ScanResult = []
        for path in sorted(directory.glob("*.json")):
            try:
                results.append((path.stem, self.get(kind, path.stem), ""))
            except (EvidenceError, OSError, UnicodeDecodeError) as error:
                results.append((path.stem, None, str(error)))
        return results
