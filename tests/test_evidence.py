from __future__ import annotations

import hashlib
import json

import pytest

from megamind.evidence import (
    CLAIM_SCHEMA,
    CONTRADICTION_SCHEMA,
    EVIDENCE_SCHEMA,
    QUOTATION_SCHEMA,
    EvidenceError,
    reconcile_claims,
    validate_claim,
    validate_evidence_record,
    validate_quotation,
)
from megamind.fsops import content_hash
from megamind.policy import PolicyError, ResearchPolicy


def _evidence() -> dict[str, object]:
    text = "A frozen fact."
    normalized = hashlib.sha256(text.encode()).hexdigest()
    canonical = "https://example.test/fact"
    return {
        "schema": EVIDENCE_SCHEMA,
        "evidence_id": content_hash(canonical + normalized),
        "source_class": "official-guidance",
        "origin_id": "authority-1",
        "canonical_url": canonical,
        "final_url": canonical,
        "redirect_chain": [],
        "identifiers": {},
        "retrieved_at": "2026-08-13T00:00:00Z",
        "published_at": "2026-08-01",
        "published_precision": "exact",
        "updated_at": "",
        "update_signal": "none",
        "evidence_as_of": "",
        "authors": [{"name": "Authority", "identity_basis": "org"}],
        "publisher": {"name": "Authority", "basis": "authority-registry"},
        "jurisdiction": "DE",
        "rights": {"license": "", "quote_policy": "quote-bounded", "snapshot_policy": "no-store"},
        "snapshot": {
            "sha256": "a" * 64,
            "normalized_sha256": normalized,
            "bytes": len(text),
            "content_type": "text/plain",
            "archive_url": "",
            "archive_datetime": "",
        },
        "corrections": {
            "checked_at": "2026-08-13",
            "method": "host-registry",
            "status": "clean",
            "notice_ids": [],
        },
        "injection_scan": {"verdict": "suspicious", "markers": ["instruction-shaped"]},
        "acceptance": {
            "decision": "accepted",
            "tier": 1,
            "quality": "primary",
            "gates": [],
            "failure": "",
        },
        "revalidate_after": "2027-08-13",
    }


def test_evidence_unknown_fields_and_retraction_are_restrictive() -> None:
    record = _evidence()
    record["unexpected"] = True
    with pytest.raises(EvidenceError):
        validate_evidence_record(record)
    record = _evidence()
    record["corrections"] = {**record["corrections"], "status": "retracted"}  # type: ignore[index]
    assert validate_evidence_record(record)["corrections"]["status"] == "retracted"


def test_quotation_is_hash_bound_and_claim_requires_resolution() -> None:
    text = "A frozen fact."
    normalized = hashlib.sha256(text.encode()).hexdigest()
    quote = {"exact": "frozen", "prefix": "A ", "suffix": " fact."}
    position = {"start": 2, "end": 8}
    quotation_id = content_hash(
        json.dumps(
            {
                "evidence_id": "ev1",
                "against_hash": normalized,
                "quote": quote,
                "position": position,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    quotation = {
        "schema": QUOTATION_SCHEMA,
        "quotation_id": quotation_id,
        "evidence_id": "ev1",
        "against_hash": normalized,
        "quote": quote,
        "position": position,
        "media": {"t_start": None, "t_end": None, "segment_ids": []},
        "transcription_uncertainty": "not-applicable",
        "resolved_at": "2026-08-13",
        "resolves": True,
    }
    assert validate_quotation(quotation, text)["resolves"] is True
    claim_body = {
        "claim_key": "fact",
        "statement": "A fact",
        "qualifiers": {},
    }
    claim = {
        "schema": CLAIM_SCHEMA,
        "claim_id": content_hash(json.dumps(claim_body, sort_keys=True, separators=(",", ":"))),
        "type": "fact",
        "lifecycle": "active",
        "supported_by": [{"evidence_id": "ev1", "quotation_id": quotation_id, "role": "primary"}],
        "contradicted_by": [],
        "superseded_by": "",
        **claim_body,
    }
    assert (
        validate_claim(claim, quotations={quotation_id: quotation})["claim_id"] == claim["claim_id"]
    )
    with pytest.raises(EvidenceError):
        validate_claim({**claim, "supported_by": []})


def test_contradictions_are_unresolved_without_averaging() -> None:
    def claim(statement: str) -> dict[str, object]:
        body = {"claim_key": "k", "statement": statement, "qualifiers": {}}
        return {
            "schema": CLAIM_SCHEMA,
            "claim_id": content_hash(json.dumps(body, sort_keys=True, separators=(",", ":"))),
            "type": "fact",
            **body,
            "lifecycle": "proposed",
            "supported_by": [],
            "contradicted_by": [],
            "superseded_by": "",
        }

    contradictions = reconcile_claims([claim("one"), claim("two")], today="2026-08-13")
    assert len(contradictions) == 1
    assert contradictions[0]["schema"] == CONTRADICTION_SCHEMA
    assert contradictions[0]["resolution"] == "unresolved"


def test_research_policy_rejects_unknown_fields_and_absence_is_denial() -> None:
    with pytest.raises(PolicyError):
        ResearchPolicy.from_data(
            {"schema": "megamind/research-policy/v1", "tiers": [], "unknown": 1}
        )
    policy = ResearchPolicy.from_data({"schema": "megamind/research-policy/v1", "tiers": []})
    assert policy.permitted is False
