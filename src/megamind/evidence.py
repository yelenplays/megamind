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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .confidence import (
    CLEAN_CORRECTION,
    CORRECTION_STATUSES,
    FRESHNESS_STATES,
    QUALITY_BASE,
    Source,
    claim_confidence,
)
from .fsops import (
    MEGAMIND_DIR,
    append_audit,
    atomic_write,
    backup_existing,
    content_hash,
    identity_bytes,
    remove_contained,
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
CORRECTION_SCHEMA = "megamind/correction-notice/v1"
TRANSACTION_SCHEMA = "megamind/evidence-transaction/v1"

GATE_VERDICTS = ("pass", "fail", "unknown")
DECISIONS = ("accepted", "rejected", "deferred")
SOURCE_CLASS_SET = set(SOURCE_CLASSES)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
QUOTE_CEILING_CHARS = 1000
UNKNOWN_CORRECTION = "unknown"

# The identity of every stored document covers only part of its body: the rest
# is governed state Megamind itself derives (acceptance verdicts, resolution
# outcomes, lifecycle).  Immutability therefore applies to the identity-bearing
# fields, and a rewrite that touches anything else is refused.
#
# The frozen facts a host retrieved are not in that set at all.  A recheck that
# finds a correction or a retraction never edits them: it appends a new
# content-bound notice that supersedes the previous posture, so the record of
# what was true at retrieval time survives alongside what is true now.
MUTABLE_FIELDS: dict[str, frozenset[str]] = {
    "evidence": frozenset({"acceptance"}),
    "quotations": frozenset({"resolves", "resolved_at"}),
    "claims": frozenset({"lifecycle", "supported_by", "contradicted_by", "superseded_by"}),
    "contradictions": frozenset({"resolution", "resolution_detail", "updated"}),
    "corrections": frozenset(),
}

STORE_KINDS: tuple[str, ...] = tuple(MUTABLE_FIELDS)
ID_KEYS: dict[str, str] = {
    "evidence": "evidence_id",
    "quotations": "quotation_id",
    "claims": "claim_id",
    "contradictions": "contradiction_id",
    "corrections": "notice_id",
}

# One stored record per entry: its identifier, the validated record when it
# reads, and the problem that stopped it when it does not.
ScanResult = list[tuple[str, dict[str, Any] | None, str]]


class EvidenceError(ValueError):
    """Malformed or unsafe evidence input."""

    code = "evidence_invalid"


class EvidenceAcceptanceError(EvidenceError):
    code = "evidence_acceptance_invalid"


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


def _url_origin(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        raise EvidenceError("origin URL must have a host")
    try:
        port = parts.port
    except ValueError as error:
        raise EvidenceError("origin URL has an invalid port") from error
    host = host.casefold()
    if ":" in host:
        host = f"[{host}]"
    default_port = (parts.scheme.casefold() == "http" and port == 80) or (
        parts.scheme.casefold() == "https" and port == 443
    )
    return f"{parts.scheme.casefold()}://{host}{'' if port is None or default_port else f':{port}'}"


def derived_origin_id(final_url: str) -> str:
    """Return the sole origin identity eligible for corroboration."""
    return "url-origin/v1:" + hashlib.sha256(_url_origin(final_url).encode("utf-8")).hexdigest()


def origin_proof(canonical_url: str, final_url: str) -> str:
    """Bind a derived origin identity to the frozen URL facts."""
    payload = json.dumps(
        {
            "canonical_url": canonical_url,
            "final_url": final_url,
            "origin_id": derived_origin_id(final_url),
            "schema": "megamind/origin-proof/v1",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verified_origin_id(record: Mapping[str, Any]) -> str:
    """Return a corroborating origin only when its local provenance binds."""
    try:
        canonical = _canonical_url(record.get("canonical_url"))
        final = _canonical_url(record.get("final_url", canonical))
        expected_id = derived_origin_id(final)
        expected_proof = origin_proof(canonical, final)
        if record.get("origin_id") == expected_id and record.get("origin_proof") == expected_proof:
            return expected_id
    except EvidenceError:
        pass
    return ""


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
        "origin_proof",
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
    if origin_id != derived_origin_id(final_url):
        raise EvidenceError("origin_id does not match the derived final URL origin")
    proof = _hash("origin_proof", data.get("origin_proof"))
    if proof != origin_proof(canonical, final_url):
        raise EvidenceError("origin_proof does not bind the frozen URL facts")
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
            if key == "timecode_coverage":
                if not isinstance(value, bool):
                    raise EvidenceError("derivation.timecode_coverage must be a boolean")
            else:
                _str(f"derivation.{key}", value, limit=300)
    if source_class == "video" and derivation is None:
        raise EvidenceError("video evidence requires derivation lineage")
    return {
        "schema": EVIDENCE_SCHEMA,
        "evidence_id": expected_id,
        "source_class": source_class,
        "origin_id": origin_id,
        "origin_proof": proof,
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


def validate_correction_notice(raw: object) -> dict[str, Any]:
    """Validate one append-only correction or retraction notice.

    A notice is a content-bound event about an already-frozen artifact, not an
    edit of it: its identity covers every fact it asserts, including the notice
    it supersedes, so a posture history can only ever grow.
    """
    data = _map("correction notice", raw)
    _unknown(
        "correction notice",
        data,
        {
            "schema",
            "notice_id",
            "evidence_id",
            "status",
            "checked_at",
            "method",
            "notice_ids",
            "supersedes",
        },
    )
    if data.get("schema") != CORRECTION_SCHEMA:
        raise EvidenceError(f"correction notice schema must be {CORRECTION_SCHEMA}")
    status = _str("correction notice status", data.get("status"), nonempty=True, limit=30)
    if status not in CORRECTION_STATUSES:
        raise EvidenceError("correction notice status is invalid")
    body = {
        "evidence_id": _str(
            "correction notice evidence_id", data.get("evidence_id"), nonempty=True, limit=64
        ),
        "status": status,
        "checked_at": _date("correction notice checked_at", data.get("checked_at")),
        "method": _str("correction notice method", data.get("method"), nonempty=True, limit=100),
        "notice_ids": _strings("correction notice notice_ids", data.get("notice_ids", [])),
        "supersedes": _str("correction notice supersedes", data.get("supersedes", ""), limit=64),
    }
    expected = content_hash(_stable(body))
    if data.get("notice_id", expected) != expected:
        raise EvidenceError("notice_id does not match the correction notice body")
    return {"schema": CORRECTION_SCHEMA, "notice_id": expected, **body}


def correction_head(
    evidence_id: str, notices: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """The single current notice of one artifact's supersession chain.

    A chain with no head or several heads names no posture at all, so it
    resolves to ``None`` and every caller treats it restrictively rather than
    picking a winner.
    """
    chain = [item for item in notices if str(item["evidence_id"]) == evidence_id]
    if not chain:
        return None
    superseded = {str(item["supersedes"]) for item in chain if item["supersedes"]}
    heads = [item for item in chain if str(item["notice_id"]) not in superseded]
    return heads[0] if len(heads) == 1 else None


def validate_correction_chain(evidence_id: str, notices: Mapping[str, Mapping[str, Any]]) -> None:
    chain = [notice for notice in notices.values() if str(notice["evidence_id"]) == evidence_id]
    roots = [notice for notice in chain if not notice["supersedes"]]
    if len(roots) != 1:
        raise EvidenceError("correction chain must have exactly one initial notice")
    children: dict[str, int] = {}
    for notice in chain:
        notice_id = str(notice["notice_id"])
        supersedes = str(notice["supersedes"])
        if supersedes == notice_id:
            raise EvidenceError("correction notice cannot supersede itself")
        if supersedes:
            prior = notices.get(supersedes)
            if prior is None or str(prior["evidence_id"]) != evidence_id:
                raise EvidenceError("correction notice does not extend its evidence chain")
            children[supersedes] = children.get(supersedes, 0) + 1
    if any(count > 1 for count in children.values()):
        raise EvidenceError("correction chain cannot fork")
    head = correction_head(evidence_id, chain)
    if head is None:
        raise EvidenceError("correction chain has no single current notice")
    visited: set[str] = set()
    current: Mapping[str, Any] | None = head
    while current is not None:
        current_id = str(current["notice_id"])
        if current_id in visited:
            raise EvidenceError("correction chain cannot contain a cycle")
        visited.add(current_id)
        predecessor = str(current["supersedes"])
        current = notices.get(predecessor) if predecessor else None
    if len(visited) != len(chain):
        raise EvidenceError("correction chain is disconnected")


def resolve_corrections(
    record: Mapping[str, Any], notices: Sequence[Mapping[str, Any]] = ()
) -> dict[str, Any]:
    """The correction posture in force now: the chain head, else the frozen one.

    The record's own block stays exactly as the host retrieved it. This is the
    single owner of "what is the current posture", so no caller has to decide
    whether a stored artifact or a later notice wins.
    """
    evidence_id = str(record["evidence_id"])
    chain = [item for item in notices if str(item["evidence_id"]) == evidence_id]
    if not chain:
        return dict(record["corrections"])
    indexed_chain = {str(item["notice_id"]): item for item in chain}
    try:
        validate_correction_chain(evidence_id, indexed_chain)
    except EvidenceError:
        return {**dict(record["corrections"]), "status": UNKNOWN_CORRECTION}
    head = correction_head(evidence_id, chain)
    if head is None:
        return {**dict(record["corrections"]), "status": UNKNOWN_CORRECTION}
    return {
        "checked_at": head["checked_at"],
        "method": head["method"],
        "status": head["status"],
        "notice_ids": list(head["notice_ids"]),
    }


def _quotation_verdict(quotations: Sequence[Mapping[str, Any]], quote_ceiling_chars: int) -> str:
    """G10 is only a pass once a hash-bound span actually re-resolved."""
    if not quotations:
        return "unknown"
    for quotation in quotations:
        try:
            _selector_quote("quotation quote", quotation["quote"], quote_ceiling_chars)
        except (EvidenceError, KeyError):
            continue
        if quotation.get("resolves") is True:
            return "pass"
    return "fail"


def _publisher_verdict(publisher: Mapping[str, Any], policy: ResearchPolicy | None) -> str:
    """A declared registry authority must appear in the wiki's accepted list."""
    if publisher["basis"] == "unresolved":
        return "fail"
    if policy is None:
        return "unknown"
    authorities = policy.accepted_authorities
    if publisher["basis"] == "authority-registry" and publisher["name"] not in authorities:
        return "fail"
    return "pass"


def acceptance_gates(
    record: Mapping[str, Any],
    policy: ResearchPolicy | None,
    claim_type: str = "fact",
    quotations: Sequence[Mapping[str, Any]] = (),
    notices: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, str]]:
    """Derive common and class gates from facts; never trust incoming verdicts.

    ``quotations`` are the already-validated hash-bound spans recorded against
    this evidence record.  They are the only thing that can turn G10 from
    ``unknown`` into a verdict, so acceptance stays claim-bound.  ``notices``
    are that record's correction chain, so G9 judges the posture in force now
    rather than the one frozen at retrieval time.
    """
    if claim_type not in CLAIM_TYPES:
        raise EvidenceError("claim_type is invalid")
    source_class = str(record["source_class"])
    snapshot = record["snapshot"]
    corrections = resolve_corrections(record, notices)
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
            "verdict": _quotation_verdict(
                quotations,
                policy.quote_ceiling_chars if policy is not None else QUOTE_CEILING_CHARS,
            ),
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
            "verdict": "pass" if verified_origin_id(record) else "unknown",
            "detail": "locally proven derived origin identity",
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
                    "verdict": "pass" if derivation.get("timecode_coverage") is True else "unknown",
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
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
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
    if evidence is not None:
        if evidence_id not in evidence:
            raise EvidenceError(f"quotation references an unknown evidence record: {evidence_id}")
        # Otherwise the span is bound to whatever text the caller supplied
        # rather than to the frozen snapshot, and forged text could resolve.
        frozen = str(evidence[evidence_id]["snapshot"]["normalized_sha256"])
        if against_hash != frozen:
            raise EvidenceError(
                f"quotation against_hash {against_hash} is not the normalized snapshot of "
                f"evidence record {evidence_id} ({frozen})"
            )
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
    raw: object,
    *,
    quotations: Mapping[str, Mapping[str, Any]] | None = None,
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
    notices: Sequence[Mapping[str, Any]] = (),
    policy: ResearchPolicy | None = None,
    enforce_policy: bool = False,
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
        evidence_id = _str("claim evidence_id", support.get("evidence_id"), nonempty=True, limit=64)
        if quotations is not None and quotation_id not in quotations:
            raise EvidenceError(f"claim references an unknown quotation: {quotation_id}")
        if evidence is not None and evidence_id not in evidence:
            raise EvidenceError(f"claim references an unknown evidence record: {evidence_id}")
        # A span proves text against the one snapshot it was hash-bound to.
        # Crediting it to a different artifact would let a host pick whose
        # acceptance and correction posture a quotation earns.
        if quotations is not None:
            span_evidence = str(quotations[quotation_id]["evidence_id"])
            if span_evidence != evidence_id:
                raise EvidenceError(
                    f"claim support pairs quotation {quotation_id} with evidence record "
                    f"{evidence_id}, but that span is bound to {span_evidence}"
                )
        supported.append(
            {
                "evidence_id": evidence_id,
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
    if lifecycle == "active" and evidence is not None:
        for support in supported:
            record = evidence[support["evidence_id"]]
            if record["acceptance"].get("decision") != "accepted":
                raise EvidenceError("active claim requires accepted evidence")
            if resolve_corrections(record, notices)["status"] != CLEAN_CORRECTION:
                raise EvidenceError("active claim requires currently clean evidence")
    if lifecycle == "active" and enforce_policy:
        if quotations is None or evidence is None:
            raise EvidenceError("active claim policy admission requires stored support")
        for support in supported:
            gates = acceptance_gates(
                evidence[support["evidence_id"]],
                policy,
                claim_type,
                [quotations[support["quotation_id"]]],
                notices,
            )
            if decide_acceptance(gates)[0] != "accepted":
                raise EvidenceError("active claim support is not accepted under the wiki policy")
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


def validate_contradiction(
    raw: object, *, claims: Mapping[str, Mapping[str, Any]] | None = None
) -> dict[str, Any]:
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
    if claims is not None:
        for claim_id in ids:
            if claim_id not in claims:
                raise EvidenceError(f"contradiction references an unknown claim: {claim_id}")
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
    notices: Sequence[Mapping[str, Any]] = (),
) -> Any:
    """Use the existing confidence constants after evidence acceptance only.

    Support is weighed against the correction posture in force now, so a
    retraction recorded after acceptance removes the source immediately rather
    than waiting for the artifact's acceptance block to be re-derived.
    """
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
                verified_origin_id(record),
                str(resolve_corrections(record, notices)["status"]),
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


def _validate(kind: str, data: object) -> dict[str, Any]:
    validators = {
        "evidence": validate_evidence_record,
        "quotations": validate_quotation,
        "claims": validate_claim,
        "contradictions": validate_contradiction,
        "corrections": validate_correction_notice,
    }
    return validators[kind](data)


class EvidenceStore:
    """Content-addressed immutable evidence documents under a vault root."""

    def __init__(self, root: Path):
        self.root = root

    def _require_kind(self, kind: str) -> None:
        if kind not in STORE_KINDS:
            raise EvidenceError("unknown evidence store kind")

    def _path(self, kind: str, identifier: str) -> Path:
        self._require_kind(kind)
        return Path(MEGAMIND_DIR) / "evidence" / kind / f"{identifier}.json"

    def _transaction_path(self, identifier: str) -> Path:
        return Path(MEGAMIND_DIR) / "evidence" / "transactions" / f"{identifier}.json"

    def _pending_transaction_matches(
        self, prepared: Sequence[tuple[Path, dict[str, Any], str]]
    ) -> str | None:
        directory = resolve_contained(self.root, Path(MEGAMIND_DIR) / "evidence" / "transactions")
        if not directory.is_dir():
            return None
        journals = sorted(directory.glob("*.json"))
        if not journals:
            return None
        expected = {
            rel.as_posix(): hashlib.sha256(text.encode("utf-8")).hexdigest()
            for rel, _, text in prepared
        }
        for journal in journals:
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise EvidenceError("evidence transaction journal is unreadable") from error
            if (
                not isinstance(payload, dict)
                or set(payload) != {"schema", "transaction_id", "items"}
                or payload["schema"] != TRANSACTION_SCHEMA
                or payload["transaction_id"] != journal.stem
                or not isinstance(payload["items"], list)
                or content_hash(_stable({"items": payload["items"]})) != journal.stem
            ):
                raise EvidenceError("evidence transaction recovery authority is unavailable")
            journal_items = payload["items"]
            if len(journal_items) != len(expected):
                raise EvidenceError("evidence transaction recovery authority is unavailable")
            for item in journal_items:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"path", "previous", "staged_sha256"}
                    or not isinstance(item["path"], str)
                    or not isinstance(item["staged_sha256"], str)
                    or item["staged_sha256"] != expected.get(item["path"])
                    or (item["previous"] is not None and not isinstance(item["previous"], str))
                ):
                    raise EvidenceError("evidence transaction recovery authority is unavailable")
                target = resolve_contained(self.root, Path(item["path"]))
                current = target.read_text(encoding="utf-8") if target.is_file() else None
                if current is None and item["previous"] is None:
                    continue
                if current is not None and hashlib.sha256(current.encode("utf-8")).hexdigest() == item[
                    "staged_sha256"
                ]:
                    continue
                if current == item["previous"]:
                    continue
                raise EvidenceError("evidence transaction recovery authority is unavailable")
        return journal.stem

    def _recover_transactions(self) -> None:
        directory = resolve_contained(self.root, Path(MEGAMIND_DIR) / "evidence" / "transactions")
        if not directory.is_dir():
            return
        for journal in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise EvidenceError("evidence transaction journal is unreadable") from error
            if not isinstance(payload, dict) or set(payload) != {"schema", "transaction_id", "items"}:
                raise EvidenceError("evidence transaction journal is invalid")
            if payload["schema"] != TRANSACTION_SCHEMA or payload["transaction_id"] != journal.stem:
                raise EvidenceError("evidence transaction journal is invalid")
            items = payload["items"]
            if not isinstance(items, list) or not items:
                raise EvidenceError("evidence transaction journal is invalid")
            expected = content_hash(_stable({"items": items}))
            if expected != journal.stem:
                raise EvidenceError("evidence transaction journal is invalid")
            raise EvidenceError("evidence transaction recovery authority is unavailable")

    def recover(self) -> None:
        self._recover_transactions()

    def _admission_records(
        self, items: Sequence[tuple[str, Mapping[str, Any]]] = ()
    ) -> dict[str, dict[str, dict[str, Any]]]:
        for kind, _ in items:
            self._require_kind(kind)
        records: dict[str, dict[str, dict[str, Any]]] = {}
        for kind in STORE_KINDS:
            records[kind] = {
                identifier: record
                for identifier, record, _ in self.scan_readonly(kind)
                if record is not None
            }
        for kind, data in items:
            record = _validate(kind, data)
            records[kind][str(record[ID_KEYS[kind]])] = record
        return records

    def _validate_admission(
        self,
        kind: str,
        data: Mapping[str, Any],
        normalized_text: str | None,
        quote_ceiling_chars: int,
        records: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        self._require_kind(kind)
        if kind == "quotations":
            provisional = validate_quotation(data, quote_ceiling_chars=quote_ceiling_chars)
            if provisional["resolves"] and normalized_text is None:
                raise EvidenceError("resolved quotation requires frozen normalized text")
            return validate_quotation(
                data,
                normalized_text,
                quote_ceiling_chars=quote_ceiling_chars,
                evidence=records["evidence"],
            )
        if kind == "claims":
            return validate_claim(
                data,
                quotations=records["quotations"],
                evidence=records["evidence"],
                notices=list(records["corrections"].values()),
            )
        if kind == "contradictions":
            return validate_contradiction(data, claims=records["claims"])
        if kind == "corrections":
            notice = validate_correction_notice(data)
            evidence_id = str(notice["evidence_id"])
            if evidence_id not in records["evidence"]:
                raise EvidenceError(
                    f"correction notice references an unknown evidence record: {evidence_id}"
                )
            validate_correction_chain(evidence_id, records["corrections"])
            return notice
        return _validate(kind, data)

    def _prepare(
        self,
        kind: str,
        data: Mapping[str, Any],
        normalized_text: str | None = None,
        quote_ceiling_chars: int = QUOTE_CEILING_CHARS,
        records: Mapping[str, Mapping[str, Mapping[str, Any]]] | None = None,
    ) -> tuple[Path, dict[str, Any], str]:
        """Validate one record and prove it may be written, without writing."""
        self._require_kind(kind)
        canonical = self._validate_admission(
            kind,
            data,
            normalized_text,
            quote_ceiling_chars,
            records if records is not None else self._admission_records(((kind, data),)),
        )
        rel = self._path(kind, str(canonical[ID_KEYS[kind]]))
        path = resolve_contained(self.root, rel)
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
        return rel, canonical, text

    def put(
        self,
        kind: str,
        data: Mapping[str, Any],
        normalized_text: str | None = None,
        *,
        quote_ceiling_chars: int = QUOTE_CEILING_CHARS,
    ) -> Path:
        self._require_kind(kind)
        self._recover_transactions()
        rel, canonical, text = self._prepare(
            kind,
            data,
            normalized_text,
            quote_ceiling_chars,
            self._admission_records(((kind, data),)),
        )
        if resolve_contained(self.root, rel).is_file():
            backup_existing(self.root, rel, durable=True)
        atomic_write(self.root, rel, text)
        append_audit(
            self.root,
            "evidence-record",
            {"kind": kind, "record_id": str(canonical[ID_KEYS[kind]])},
        )
        return resolve_contained(self.root, rel)

    def put_all(
        self,
        items: Sequence[tuple[str, Mapping[str, Any]]],
        normalized_text: str | None = None,
        *,
        quote_ceiling_chars: int = QUOTE_CEILING_CHARS,
    ) -> list[Path]:
        """Write a related set only once every record in it is provably writable.

        Records that reference each other must land together or not at all,
        otherwise a refusal halfway through leaves a vault whose own doctor
        reports it broken. Validation and the immutability check therefore run
        over the whole set before the first byte is written.
        """
        for kind, _ in items:
            self._require_kind(kind)
        records = self._admission_records(items)
        prepared = [
            self._prepare(kind, data, normalized_text, quote_ceiling_chars, records)
            for kind, data in items
        ]
        if len({rel for rel, _, _ in prepared}) != len(prepared):
            raise EvidenceError("evidence transaction contains duplicate record targets")
        items_for_journal: list[dict[str, str | None]] = []
        for rel, _, text in prepared:
            path = resolve_contained(self.root, rel)
            previous = path.read_text(encoding="utf-8") if path.is_file() else None
            items_for_journal.append(
                {
                    "path": rel.as_posix(),
                    "previous": previous,
                    "staged_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
            if previous is not None:
                backup_existing(self.root, rel, durable=True)
        pending_transaction_id = self._pending_transaction_matches(prepared)
        transaction_id = pending_transaction_id or content_hash(_stable({"items": items_for_journal}))
        journal_rel = self._transaction_path(transaction_id)
        if pending_transaction_id is None:
            atomic_write(
                self.root,
                journal_rel,
                json.dumps(
                    {
                        "schema": TRANSACTION_SCHEMA,
                        "transaction_id": transaction_id,
                        "items": items_for_journal,
                    },
                    sort_keys=True,
                    indent=2,
                )
                + "\n",
                durable=True,
            )
        for rel, _, text in prepared:
            atomic_write(self.root, rel, text, durable=True)
        backup_existing(self.root, journal_rel, durable=True)
        remove_contained(self.root, journal_rel, durable=True)
        append_audit(
            self.root,
            "evidence-transaction-complete",
            {"transaction_id": transaction_id, "paths": [rel.as_posix() for rel, _, _ in prepared]},
        )
        for rel, canonical, _ in prepared:
            append_audit(self.root, "evidence-record", {"kind": rel.parent.name, "record_id": str(canonical[ID_KEYS[rel.parent.name]])})
        return [resolve_contained(self.root, rel) for rel, _, _ in prepared]

    def get(self, kind: str, identifier: str) -> dict[str, Any]:
        self._require_kind(kind)
        self._recover_transactions()
        return self._read(kind, identifier)

    def _read(self, kind: str, identifier: str) -> dict[str, Any]:
        path = resolve_contained(self.root, self._path(kind, identifier))
        if not path.is_file():
            raise EvidenceError("evidence record not found")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise EvidenceError("evidence record is not valid JSON") from error
        return _validate(kind, data)

    def scan_readonly(self, kind: str) -> ScanResult:
        """List records without recovering a pending transaction."""
        self._require_kind(kind)
        directory = resolve_contained(self.root, Path(MEGAMIND_DIR) / "evidence" / kind)
        if not directory.is_dir():
            return []
        results: ScanResult = []
        for path in sorted(directory.glob("*.json")):
            try:
                results.append((path.stem, self._read(kind, path.stem), ""))
            except (EvidenceError, OSError, UnicodeDecodeError) as error:
                results.append((path.stem, None, str(error)))
        return results

    def scan(self, kind: str) -> ScanResult:
        """List every record, reporting rather than raising on a bad one.

        A projection over the store must stay usable when one file is corrupt:
        an unreadable record is the exact thing the reader needs to be told
        about, so it is returned as a typed problem instead of aborting the
        whole read.
        """
        return self.scan_readonly(kind)


@dataclass(frozen=True)
class _LegacyEvidence:
    evidence_id: str
    origin: str
    origin_id: str
    decision: str
    quality: str
    correction_status: str


@dataclass(frozen=True)
class _LegacyClaim:
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
class _LegacyContradiction:
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


def accept_evidence(data: Mapping[str, Any]) -> _LegacyEvidence:
    required = ("origin", "quality", "snapshot_sha256", "normalized_sha256")
    if any(not isinstance(data.get(name), str) or not data[name] for name in required):
        raise EvidenceAcceptanceError("legacy evidence facts are incomplete")
    quality = str(data["quality"])
    if quality not in QUALITY_BASE:
        raise EvidenceAcceptanceError("quality must be a known confidence quality")
    correction = str(data.get("correction_status", CLEAN_CORRECTION))
    if correction not in CORRECTION_STATUSES:
        raise EvidenceAcceptanceError("unknown correction status")
    facts = data.get("facts", {})
    if not isinstance(facts, Mapping):
        raise EvidenceAcceptanceError("facts must be an object")
    accepted = correction == CLEAN_CORRECTION and all(
        facts.get(name) is True
        for name in ("retrievable", "identity_resolved", "dated", "attributed", "publisher_resolved", "rights_determined")
    )
    body = {
        "origin": data["origin"], "origin_id": data.get("origin_id", ""), "quality": quality,
        "correction_status": correction, "snapshot_sha256": data["snapshot_sha256"],
        "normalized_sha256": data["normalized_sha256"], "decision": "accepted" if accepted else "rejected" if correction == "retracted" else "deferred",
    }
    return _LegacyEvidence(
        content_hash(_stable(body)), str(body["origin"]), "", str(body["decision"]), quality, correction
    )


def claim_confidence_from_records(
    records: list[_LegacyEvidence], *, lifecycle: str = "proposed", contradicted: bool = False,
    freshness: str = "unknown",
) -> float | str:
    if freshness not in FRESHNESS_STATES:
        raise EvidenceAcceptanceError(f"freshness must be one of {', '.join(FRESHNESS_STATES)}")
    return claim_confidence(
        [Source(record.quality, record.origin, record.decision == "accepted", record.origin_id, record.correction_status) for record in records],
        lifecycle=lifecycle, freshness=freshness, contradicted=contradicted,
    ).render()


def make_resolved_claim(data: Mapping[str, Any], records: Mapping[str, _LegacyEvidence]) -> _LegacyClaim:
    supports = data.get("supported_by", [])
    if not isinstance(supports, list) or any(item not in records or records[item].decision != "accepted" for item in supports):
        raise EvidenceAcceptanceError("claim contains an unresolved evidence reference")
    key, statement = data.get("claim_key"), data.get("statement")
    if not isinstance(key, str) or not key or not isinstance(statement, str) or not statement:
        raise EvidenceAcceptanceError("claim key and statement are required")
    lifecycle = str(data.get("lifecycle", "proposed"))
    allowed = {"proposed", "confirmed", "active", "shaky", "rejected", "superseded"}
    if lifecycle not in allowed:
        raise EvidenceAcceptanceError("claim lifecycle must be one of proposed, confirmed, active, shaky, rejected, superseded")
    effective = "proposed" if lifecycle in {"active", "shaky", "confirmed"} else lifecycle
    contradicted = data.get("contradicted_by", [])
    if not isinstance(contradicted, list) or not all(isinstance(item, str) for item in contradicted):
        raise EvidenceAcceptanceError("contradicted_by must contain opaque references")
    identifier = content_hash(_stable({"claim_key": key, "statement": statement, "supported_by": supports, "contradicted_by": contradicted, "lifecycle": effective}))
    return _LegacyClaim(identifier, key, statement, tuple(supports), tuple(contradicted), effective, claim_confidence_from_records([records[item] for item in supports], lifecycle=effective, contradicted=bool(contradicted)))


def make_contradiction(data: Mapping[str, Any], resolver: Any) -> _LegacyContradiction:
    ids = data.get("claim_ids", [])
    if not isinstance(ids, list) or len(ids) < 2 or any(not isinstance(item, str) or not resolver.resolve("claim", item) for item in ids):
        raise EvidenceAcceptanceError("contradiction claim references are invalid")
    resolution = str(data.get("resolution", "unresolved"))
    if resolution not in {"unresolved", "scope_disjoint", "supersession", "retraction", "authority_precedence"}:
        raise EvidenceAcceptanceError("invalid contradiction resolution")
    body = {"claim_ids": ids, "basis": str(data.get("basis", "")), "resolution": resolution, "gap_id": str(data.get("gap_id", ""))}
    return _LegacyContradiction(content_hash(_stable(body)), tuple(ids), body["basis"], resolution, body["gap_id"])


def store_contradiction(root: Path, contradiction: _LegacyContradiction) -> Path:
    relative = Path(MEGAMIND_DIR) / "research" / "contradictions" / f"{contradiction.contradiction_id}.json"
    path = resolve_contained(root, relative)
    text = json.dumps(contradiction.to_data(), indent=2, sort_keys=True) + "\n"
    if path.is_file():
        if path.read_text(encoding="utf-8") != text:
            raise EvidenceAcceptanceError("frozen contradiction already exists with different bytes")
        return path
    result = atomic_write(root, relative, text)
    append_audit(root, "research-contradiction", {"contradiction_id": contradiction.contradiction_id})
    return result


def unresolved_contradictions(root: Path, identifiers: Iterable[str]) -> list[str]:
    result: list[str] = []
    for identifier in sorted(set(identifiers)):
        path = resolve_contained(root, Path(MEGAMIND_DIR) / "research" / "contradictions" / f"{identifier}.json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise EvidenceAcceptanceError("frozen contradiction artifact is missing") from error
        if not isinstance(raw, Mapping) or raw.get("contradiction_id") != identifier:
            raise EvidenceAcceptanceError("frozen contradiction does not match its content")
        body = {key: raw.get(key) for key in ("claim_ids", "basis", "resolution", "gap_id")}
        if content_hash(_stable(body)) != identifier:
            raise EvidenceAcceptanceError("frozen contradiction does not match its content")
        if raw.get("resolution") == "unresolved":
            result.append(identifier)
    return result
