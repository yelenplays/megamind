from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

import pytest

import megamind.evidence as evidence_module
from conftest import build_vault
from megamind.card import CARD_PATH, load_wiki_card, save_wiki_card
from megamind.cli import main
from megamind.evidence import (
    CLAIM_SCHEMA,
    CONTRADICTION_SCHEMA,
    CORRECTION_SCHEMA,
    EVIDENCE_SCHEMA,
    QUOTATION_SCHEMA,
    EvidenceError,
    EvidenceStore,
    acceptance_gates,
    reconcile_claims,
    resolve_corrections,
    validate_claim,
    validate_correction_chain,
    validate_correction_notice,
    validate_evidence_record,
    validate_quotation,
)
from megamind.fsops import BACKUP_DIR, MEGAMIND_DIR, content_hash
from megamind.policy import (
    RESEARCH_POLICY_SCHEMA,
    PolicyError,
    ResearchPolicy,
    ResearchTier,
    freshness_state,
    tier_for_facts,
)
from megamind.registry import load_registry, save_registry
from megamind.research import PACKET_SCHEMA
from megamind.scaffold import init_wiki_root


def _evidence() -> dict[str, object]:
    text = "A frozen fact."
    normalized = hashlib.sha256(text.encode()).hexdigest()
    canonical = "https://example.test/fact"
    return {
        "schema": EVIDENCE_SCHEMA,
        "evidence_id": content_hash(canonical + normalized),
        "source_class": "official-guidance",
        "canonical_url": canonical,
        "final_url": canonical,
        "origin_id": evidence_module.derived_origin_id(canonical),
        "origin_proof": evidence_module.origin_proof(canonical, canonical),
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


def test_origin_corroboration_requires_a_url_bound_local_proof() -> None:
    record = _evidence()
    same_origin = _evidence()
    same_origin["canonical_url"] = "https://example.test/another-fact"
    same_origin["final_url"] = "https://example.test/another-fact"
    same_origin["origin_id"] = evidence_module.derived_origin_id(str(same_origin["final_url"]))
    same_origin["origin_proof"] = evidence_module.origin_proof(
        str(same_origin["canonical_url"]), str(same_origin["final_url"])
    )
    assert record["origin_id"] == same_origin["origin_id"]

    forged = {**record, "origin_id": "url-origin/v1:" + "0" * 64}
    with pytest.raises(EvidenceError, match="derived final URL origin"):
        validate_evidence_record(forged)
    assert next(gate for gate in acceptance_gates(forged, None) if gate["gate"] == "G12")[
        "verdict"
    ] == "unknown"

    forged = {**record, "origin_proof": "0" * 64}
    with pytest.raises(EvidenceError, match="frozen URL facts"):
        validate_evidence_record(forged)
    assert next(gate for gate in acceptance_gates(forged, None) if gate["gate"] == "G12")[
        "verdict"
    ] == "unknown"


def test_video_timecode_coverage_requires_a_boolean() -> None:
    record = _evidence()
    record["source_class"] = "video"
    record["derivation"] = {
        "video_id": "synthetic-video",
        "channel": "Synthetic Channel",
        "upload_date": "2026-08-01",
        "provider": "synthetic",
        "language": "en",
        "timecode_coverage": "false",
        "transcript_sha256": "a" * 64,
        "caption_source": "creator",
    }
    with pytest.raises(EvidenceError, match="timecode_coverage"):
        validate_evidence_record(record)


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


@pytest.mark.parametrize("authorities", [None, []])
def test_empty_authority_allowlist_rejects_registry_publishers(
    authorities: list[str] | None,
) -> None:
    policy_data = {
        "schema": RESEARCH_POLICY_SCHEMA,
        "research": "approval",
        "tiers": [
            {
                "tier": 1,
                "name": "authority",
                "quality": "primary",
                "matchers": [{"kind": "publisher", "publisher": "Authority"}],
            }
        ],
    }
    if authorities is not None:
        policy_data["accepted_authorities"] = authorities
    policy = ResearchPolicy.from_data(policy_data)
    gates = acceptance_gates(_evidence(), policy)
    assert next(gate for gate in gates if gate["gate"] == "G6")["verdict"] == "fail"


@pytest.mark.parametrize(
    "matcher",
    [
        {"kind": "typo"},
        {"kind": "publisher"},
        {"kind": "max_tier"},
        {"kind": "publisher", "publisher": ""},
        {"kind": "document_type", "document_type": "unknown"},
    ],
)
def test_research_policy_matchers_are_discriminated_and_restrictive(
    matcher: dict[str, object],
) -> None:
    data = {
        "schema": RESEARCH_POLICY_SCHEMA,
        "research": "approval",
        "tiers": [
            {
                "tier": 1,
                "name": "authority",
                "quality": "primary",
                "matchers": [matcher],
            }
        ],
    }
    with pytest.raises(PolicyError):
        ResearchPolicy.from_data(data)

    forged = ResearchPolicy(
        tiers=(ResearchTier(1, "authority", "primary", (matcher,)),), research="approval"
    )
    assert (
        tier_for_facts(
            forged,
            {"publisher": "Authority", "document_type": "official-guidance", "tier": 1},
            "fact",
        )
        is None
    )


def test_empty_and_out_of_range_quotation_spans_are_refused() -> None:
    text = "A frozen fact."
    normalized = hashlib.sha256(text.encode()).hexdigest()

    def build(quote: dict[str, str], position: dict[str, int]) -> dict[str, object]:
        return {
            "schema": QUOTATION_SCHEMA,
            "quotation_id": content_hash(
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
            ),
            "evidence_id": "ev1",
            "against_hash": normalized,
            "quote": quote,
            "position": position,
            "media": {"t_start": None, "t_end": None, "segment_ids": []},
            "transcription_uncertainty": "not-applicable",
            "resolved_at": "2026-08-13",
            "resolves": True,
        }

    empty = build({"exact": "", "prefix": "", "suffix": ""}, {"start": 9999, "end": 9999})
    with pytest.raises(EvidenceError):
        validate_quotation(empty, text)
    with pytest.raises(EvidenceError):
        validate_quotation(empty)
    zero_length = build({"exact": "frozen", "prefix": "", "suffix": ""}, {"start": 2, "end": 2})
    with pytest.raises(EvidenceError):
        validate_quotation(zero_length, text)
    past_end = build({"exact": "frozen", "prefix": "", "suffix": ""}, {"start": 2, "end": 400})
    with pytest.raises(EvidenceError):
        validate_quotation(past_end, text)


def test_quote_ceiling_bounds_selectors() -> None:
    text = "A frozen fact."
    normalized = hashlib.sha256(text.encode()).hexdigest()
    quote = {"exact": "frozen", "prefix": "A ", "suffix": " fact."}
    position = {"start": 2, "end": 8}
    quotation = {
        "schema": QUOTATION_SCHEMA,
        "quotation_id": content_hash(
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
        ),
        "evidence_id": "ev1",
        "against_hash": normalized,
        "quote": quote,
        "position": position,
        "media": {"t_start": None, "t_end": None, "segment_ids": []},
        "transcription_uncertainty": "not-applicable",
        "resolved_at": "2026-08-13",
        "resolves": True,
    }
    assert validate_quotation(quotation, text, quote_ceiling_chars=6)["resolves"] is True
    with pytest.raises(EvidenceError):
        validate_quotation(quotation, text, quote_ceiling_chars=5)


def test_accepted_evidence_requires_a_derived_tier_and_quality() -> None:
    record = _evidence()
    record["acceptance"] = {
        "decision": "accepted",
        "tier": None,
        "quality": "",
        "gates": [],
        "failure": "",
    }
    with pytest.raises(EvidenceError):
        validate_evidence_record(record)


def test_explicit_research_off_denies_every_tier() -> None:
    data = {
        "schema": RESEARCH_POLICY_SCHEMA,
        "research": "off",
        "tiers": [
            {
                "tier": 1,
                "name": "authority",
                "quality": "primary",
                "matchers": [{"kind": "publisher", "publisher": "Authority"}],
            }
        ],
    }
    from megamind.policy import tier_for_facts

    denied = ResearchPolicy.from_data(data)
    permitted = ResearchPolicy.from_data({**data, "research": "approval"})
    facts = {"publisher": "Authority", "jurisdiction": "DE", "document_type": "official-guidance"}
    assert denied.permitted is False
    assert tier_for_facts(denied, facts, "fact") is None
    assert tier_for_facts(permitted, facts, "fact") is not None


def test_freshness_state_uses_frozen_dates_only() -> None:
    policy = ResearchPolicy.from_data(
        {
            "schema": RESEARCH_POLICY_SCHEMA,
            "tiers": [],
            "freshness_policy": {"fact": {"half_life_days": 30}},
        }
    )
    assert freshness_state(policy, "fact", "2026-08-01", "2026-08-13") == "fresh"
    assert freshness_state(policy, "fact", "2026-01-01", "2026-08-13") == "stale"
    assert freshness_state(policy, "fact", "", "2026-08-13") == "unknown"
    assert freshness_state(policy, "decision", "2026-08-01", "2026-08-13") == "unknown"
    assert freshness_state(None, "fact", "2026-08-01", "2026-08-13") == "unknown"


# --- the offline AXI research lane -------------------------------------------

RESEARCH_TEXT = "A frozen fact about the synthetic product."
POLICY_DATA: dict[str, Any] = {
    "schema": RESEARCH_POLICY_SCHEMA,
    "tiers": [
        {
            "tier": 1,
            "name": "authority",
            "quality": "primary",
            "matchers": [{"kind": "publisher", "publisher": "Authority"}],
        }
    ],
    "accepted_authorities": ["Authority"],
    "freshness_policy": {"fact": {"half_life_days": 400}},
    "research": "approval",
    "max_sources_per_cycle": 2,
}


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    code = main(["--format", "json", *argv])
    captured = capsys.readouterr()
    return code, json.loads(captured.out), captured.err


def write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def research_evidence(
    canonical: str = "https://authority.test/guidance", text: str = RESEARCH_TEXT
) -> dict[str, Any]:
    normalized = hashlib.sha256(text.encode()).hexdigest()
    return {
        "schema": EVIDENCE_SCHEMA,
        "evidence_id": content_hash(canonical + normalized),
        "source_class": "official-guidance",
        "canonical_url": canonical,
        "final_url": canonical,
        "origin_id": evidence_module.derived_origin_id(canonical),
        "origin_proof": evidence_module.origin_proof(canonical, canonical),
        "redirect_chain": [],
        "identifiers": {},
        "retrieved_at": "2026-08-13T00:00:00Z",
        "published_at": "2026-08-01",
        "published_precision": "exact",
        "updated_at": "",
        "update_signal": "none",
        "evidence_as_of": "2026-08-01",
        "authors": [{"name": "Authority", "identity_basis": "org"}],
        "publisher": {"name": "Authority", "basis": "authority-registry"},
        "jurisdiction": "DE",
        "rights": {
            "license": "",
            "quote_policy": "quote-bounded",
            "snapshot_policy": "local-snapshot-allowed",
        },
        "snapshot": {
            "sha256": "b" * 64,
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
        "injection_scan": {"verdict": "clean", "markers": []},
        "acceptance": {
            "decision": "deferred",
            "tier": None,
            "quality": "",
            "gates": [],
            "failure": "",
        },
        "revalidate_after": "2027-08-13",
    }


def research_quotation(
    evidence_id: str, text: str = RESEARCH_TEXT, quote: dict[str, str] | None = None
) -> dict[str, Any]:
    normalized = hashlib.sha256(text.encode()).hexdigest()
    quote = quote or {"exact": "frozen fact", "prefix": "A ", "suffix": " about"}
    start = text.index(quote["exact"])
    position = {"start": start, "end": start + len(quote["exact"])}
    return {
        "schema": QUOTATION_SCHEMA,
        "quotation_id": content_hash(
            json.dumps(
                {
                    "evidence_id": evidence_id,
                    "against_hash": normalized,
                    "quote": quote,
                    "position": position,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        "evidence_id": evidence_id,
        "against_hash": normalized,
        "quote": quote,
        "position": position,
        "media": {"t_start": None, "t_end": None, "segment_ids": []},
        "transcription_uncertainty": "not-applicable",
        "resolved_at": "2026-08-13",
        "resolves": True,
    }


def research_claim(evidence_id: str, quotation_id: str, statement: str) -> dict[str, Any]:
    body = {"claim_key": "synthetic-fact", "statement": statement, "qualifiers": {}}
    return {
        "schema": CLAIM_SCHEMA,
        "claim_id": content_hash(json.dumps(body, sort_keys=True, separators=(",", ":"))),
        "type": "fact",
        **body,
        "lifecycle": "active",
        "supported_by": [
            {"evidence_id": evidence_id, "quotation_id": quotation_id, "role": "primary"}
        ],
        "contradicted_by": [],
        "superseded_by": "",
    }


def proposed_claim(statement: str) -> dict[str, Any]:
    body = {"claim_key": "synthetic-fact", "statement": statement, "qualifiers": {}}
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


def correction_notice(
    evidence_id: str, status: str, checked_at: str, supersedes: str = ""
) -> dict[str, Any]:
    return {
        "schema": CORRECTION_SCHEMA,
        "evidence_id": evidence_id,
        "status": status,
        "checked_at": checked_at,
        "method": "host-registry",
        "notice_ids": [],
        "supersedes": supersedes,
    }


def governed_vault(tmp_path: Path) -> Path:
    root = build_vault(tmp_path)
    registry = load_registry(root)
    starter = registry.wiki_by_name("StarterWiki")
    assert starter is not None
    starter.research_policy = ResearchPolicy.from_data(POLICY_DATA)
    save_registry(root, registry)
    return root


def test_research_refuses_an_uninitialized_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = write_json(tmp_path / "in" / "evidence.json", research_evidence())
    code, doc, _ = run_json(
        capsys, "--root", str(tmp_path / "empty"), "research", "record-artifact", "--input", payload
    )
    assert code == 2
    assert doc["schema_version"] == "megamind/error/v1"
    assert doc["code"] == "not_initialized"
    assert not (tmp_path / "empty" / MEGAMIND_DIR).exists()


def test_research_refuses_an_unknown_wiki(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    payload = write_json(tmp_path / "in" / "evidence.json", research_evidence())
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-artifact",
        "--wiki",
        "TypoWiki",
        "--input",
        payload,
    )
    assert code == 2
    assert doc["code"] == "usage_error"
    assert "TypoWiki" in doc["message"]


def test_research_plan_then_cancel_round_trips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    plan_input = write_json(
        tmp_path / "in" / "plan.json",
        {"gap_id": "gap-1", "wiki": "StarterWiki", "question": "What is the synthetic fact?"},
    )
    code, planned, _ = run_json(
        capsys, "--root", str(root), "research", "plan", "--input", plan_input
    )
    assert code == 0
    assert planned["status"] == "planned"
    assert planned["job"]["state"] == "planned"

    job_input = write_json(tmp_path / "in" / "job.json", planned["job"])
    code, cancelled, _ = run_json(
        capsys, "--root", str(root), "research", "cancel", "--input", job_input
    )
    assert code == 0
    assert cancelled["status"] == "cancelled"

    code, status, _ = run_json(capsys, "--root", str(root), "research", "status")
    assert code == 0
    assert [job["state"] for job in status["jobs"]] == ["cancelled"]
    assert status["notes"] == []

    code, resumed, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "resume",
        "--input",
        write_json(tmp_path / "in" / "cancelled.json", status["jobs"][0]),
    )
    assert code == 1
    assert resumed["code"] == "research_invalid"


def test_research_cancel_requires_a_stored_job_and_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    plan_id = "f" * 12
    job_id = content_hash(
        json.dumps({"plan_id": plan_id, "gap_id": "gap-1"}, sort_keys=True, separators=(",", ":"))
    )
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "cancel",
        "--input",
        write_json(
            tmp_path / "in" / "forged-job.json",
            {
                "schema": "megamind/research-job/v1",
                "job_id": job_id,
                "plan_id": plan_id,
                "gap_id": "gap-1",
                "state": "planned",
                "attempt": 1,
                "events": [],
            },
        ),
    )
    assert code == 1
    assert doc["code"] == "research_invalid"
    assert not (root / MEGAMIND_DIR / "research" / "jobs" / f"{job_id}.jsonl").exists()


def test_research_job_lifecycle_state_advances_in_place(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    plan_input = write_json(
        tmp_path / "in" / "plan.json",
        {"gap_id": "gap-1", "wiki": "StarterWiki", "question": "What is the synthetic fact?"},
    )
    code, planned, _ = run_json(
        capsys, "--root", str(root), "research", "plan", "--input", plan_input
    )
    assert code == 0
    moved = {**planned["job"], "events": [{"event": "host-observed"}]}
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "cancel",
        "--input",
        write_json(tmp_path / "in" / "moved.json", moved),
    )
    assert code == 0
    assert doc["status"] == "cancelled"


def test_evidence_frozen_facts_cannot_be_rewritten(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record = research_evidence()
    artifact = ["--root", str(root), "research", "record-artifact", "--wiki", "StarterWiki"]
    code, _, _ = run_json(
        capsys, *artifact, "--input", write_json(tmp_path / "in" / "evidence.json", record)
    )
    assert code == 0

    # Same identity (canonical_url + normalized snapshot), different frozen fact.
    for field, value in (
        ("publisher", {"name": "Impostor", "basis": "authority-registry"}),
        ("retrieved_at", "2030-01-01T00:00:00Z"),
    ):
        forged = {**record, field: value}
        code, doc, _ = run_json(
            capsys, *artifact, "--input", write_json(tmp_path / "in" / "forged.json", forged)
        )
        assert code == 1, field
        assert doc["code"] == "evidence_invalid"
        assert "different bytes" in doc["message"]

    forged = {**record, "origin_id": "some-other-origin"}
    code, doc, _ = run_json(
        capsys, *artifact, "--input", write_json(tmp_path / "in" / "forged-origin.json", forged)
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert "derived final URL origin" in doc["message"]

    # A retraction is a frozen fact too: it may only arrive as a new notice.
    retracted = {**record, "corrections": {**record["corrections"], "status": "retracted"}}
    code, doc, _ = run_json(
        capsys, *artifact, "--input", write_json(tmp_path / "in" / "retracted.json", retracted)
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"

    stored = json.loads(
        (root / MEGAMIND_DIR / "evidence" / "evidence" / f"{record['evidence_id']}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["publisher"]["name"] == "Authority"
    assert stored["corrections"]["status"] == "clean"


def test_evidence_reaches_accepted_only_after_a_span_resolves(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record = research_evidence()
    evidence_input = write_json(tmp_path / "in" / "evidence.json", record)
    normalized_file = tmp_path / "in" / "normalized.txt"
    normalized_file.write_text(RESEARCH_TEXT, encoding="utf-8")

    artifact = ["--root", str(root), "research", "record-artifact", "--wiki", "StarterWiki"]
    code, deferred, _ = run_json(capsys, *artifact, "--input", evidence_input)
    assert code == 0
    assert deferred["status"] == "deferred"
    assert deferred["evidence"]["acceptance"]["failure"] == "acceptance_gate_unknown:G10"

    quotation = research_quotation(str(record["evidence_id"]))
    code, quoted, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-quotations",
        "--wiki",
        "StarterWiki",
        "--normalized-file",
        str(normalized_file),
        "--input",
        write_json(tmp_path / "in" / "quotations.json", [quotation]),
    )
    assert code == 0
    assert quoted["quotations"][0]["resolves"] is True

    code, accepted, _ = run_json(capsys, *artifact, "--input", evidence_input)
    assert code == 0
    assert accepted["status"] == "accepted"
    acceptance = accepted["evidence"]["acceptance"]
    assert acceptance == {
        **acceptance,
        "decision": "accepted",
        "tier": 1,
        "quality": "primary",
        "failure": "",
    }
    assert {gate["gate"]: gate["verdict"] for gate in acceptance["gates"]}["G10"] == "pass"

    # A policy-free invocation may not silently keep the derived quality.
    code, unpoliced, _ = run_json(
        capsys, "--root", str(root), "research", "record-artifact", "--input", evidence_input
    )
    assert code == 0
    assert unpoliced["status"] == "deferred"
    assert unpoliced["evidence"]["acceptance"]["quality"] == ""


def test_artifact_rechecks_stored_quotation_against_its_wiki_ceiling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record = research_evidence()
    normalized_file = tmp_path / "in" / "normalized.txt"
    normalized_file.parent.mkdir(parents=True)
    normalized_file.write_text(RESEARCH_TEXT, encoding="utf-8")
    artifact = ["--root", str(root), "research", "record-artifact", "--wiki", "StarterWiki"]
    run_json(capsys, *artifact, "--input", write_json(tmp_path / "in" / "evidence.json", record))
    code, _, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-quotations",
        "--wiki",
        "StarterWiki",
        "--normalized-file",
        str(normalized_file),
        "--input",
        write_json(
            tmp_path / "in" / "quotations.json", [research_quotation(str(record["evidence_id"]))]
        ),
    )
    assert code == 0
    registry = load_registry(root)
    starter = registry.wiki_by_name("StarterWiki")
    assert starter is not None and starter.research_policy is not None
    starter.research_policy = ResearchPolicy.from_data(
        {**starter.research_policy.to_data(), "quote_ceiling_chars": 5}
    )
    save_registry(root, registry)
    code, doc, _ = run_json(
        capsys, *artifact, "--input", write_json(tmp_path / "in" / "evidence-again.json", record)
    )
    assert code == 0
    assert doc["status"] == "rejected"
    assert "G10" in doc["evidence"]["acceptance"]["failure"]


def test_active_claim_rechecks_support_under_its_wiki_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record, quotation = accepted_artifact(tmp_path, capsys, root)
    registry = load_registry(root)
    starter = registry.wiki_by_name("StarterWiki")
    assert starter is not None and starter.research_policy is not None
    starter.research_policy = ResearchPolicy.from_data(
        {**starter.research_policy.to_data(), "quote_ceiling_chars": 5}
    )
    save_registry(root, registry)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-claims",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(
            tmp_path / "in" / "claim.json",
            [
                research_claim(
                    str(record["evidence_id"]),
                    str(quotation["quotation_id"]),
                    "The policy-rejected synthetic fact holds",
                )
            ],
        ),
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert not (root / MEGAMIND_DIR / "evidence" / "claims").exists()
    starter.research_policy = ResearchPolicy.from_data(
        {
            **starter.research_policy.to_data(),
            "quote_ceiling_chars": 400,
            "tiers": [
                {
                    **starter.research_policy.to_data()["tiers"][0],
                    "matchers": [{"kind": "publisher", "publisher": "Other Authority"}],
                }
            ],
        }
    )
    save_registry(root, registry)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-claims",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(
            tmp_path / "in" / "claim-source.json",
            [
                research_claim(
                    str(record["evidence_id"]),
                    str(quotation["quotation_id"]),
                    "The source-policy-rejected synthetic fact holds",
                )
            ],
        ),
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert not (root / MEGAMIND_DIR / "evidence" / "claims").exists()


def test_research_off_policy_denies_acceptance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = build_vault(tmp_path)
    registry = load_registry(root)
    starter = registry.wiki_by_name("StarterWiki")
    assert starter is not None
    starter.research_policy = ResearchPolicy.from_data({**POLICY_DATA, "research": "off"})
    save_registry(root, registry)
    payload = write_json(tmp_path / "in" / "evidence.json", research_evidence())
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-artifact",
        "--wiki",
        "StarterWiki",
        "--input",
        payload,
    )
    assert code == 0
    assert doc["status"] == "deferred"
    assert doc["evidence"]["acceptance"] == {
        **doc["evidence"]["acceptance"],
        "tier": None,
        "quality": "",
    }
    verdicts = {gate["gate"]: gate["verdict"] for gate in doc["evidence"]["acceptance"]["gates"]}
    assert verdicts["G11"] == "unknown"


def test_research_today_survives_placement_before_the_subcommand(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    claims = {
        "claims": [
            proposed_claim("the synthetic fact is A"),
            proposed_claim("the synthetic fact is B"),
        ]
    }
    payload = write_json(tmp_path / "in" / "claims.json", claims)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "--today",
        "2026-08-13",
        "research",
        "reconcile",
        "--input",
        payload,
    )
    assert code == 0
    assert doc["contradictions"][0]["created"] == "2026-08-13"
    assert doc["contradictions"][0]["resolution"] == "unresolved"

    code, missing, _ = run_json(
        capsys, "--root", str(root), "research", "reconcile", "--input", payload
    )
    assert code == 2
    assert missing["code"] == "usage_error"
    assert "--today" in missing["message"]


def test_discovery_respects_the_wiki_source_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)

    def candidate(origin: str) -> dict[str, Any]:
        return {
            "candidate_id": content_hash(
                json.dumps(
                    {"origin": origin, "query_hash": ""}, sort_keys=True, separators=(",", ":")
                )
            ),
            "origin": origin,
            "status": "discovered",
        }

    origins = [f"https://authority.test/{index}" for index in range(3)]
    payload = write_json(tmp_path / "in" / "candidates.json", [candidate(o) for o in origins])
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-discovery",
        "--wiki",
        "StarterWiki",
        "--input",
        payload,
    )
    assert code == 1
    assert doc["code"] == "research_invalid"
    assert "2 sources per cycle" in doc["message"]


def test_claim_confidence_uses_stored_freshness_and_contradictions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record = research_evidence()
    evidence_input = write_json(tmp_path / "in" / "evidence.json", record)
    normalized_file = tmp_path / "in" / "normalized.txt"
    normalized_file.write_text(RESEARCH_TEXT, encoding="utf-8")
    quotation = research_quotation(str(record["evidence_id"]))
    artifact = ["--root", str(root), "research", "record-artifact", "--wiki", "StarterWiki"]
    run_json(capsys, *artifact, "--input", evidence_input)
    run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-quotations",
        "--wiki",
        "StarterWiki",
        "--normalized-file",
        str(normalized_file),
        "--input",
        write_json(tmp_path / "in" / "quotations.json", [quotation]),
    )
    run_json(capsys, *artifact, "--input", evidence_input)

    claim = research_claim(
        str(record["evidence_id"]), str(quotation["quotation_id"]), "the synthetic fact holds"
    )
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "--today",
        "2026-08-13",
        "research",
        "record-claims",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(tmp_path / "in" / "claims.json", [claim]),
    )
    assert code == 0
    scored = doc["confidence"][0]
    assert scored["freshness"] == "fresh"
    assert scored["contradicted"] is False
    assert scored["score"] == 0.9
    assert scored["meets_floor"] is True

    # The same claim, once a stale window and an unresolved contradiction exist.
    code, stale, _ = run_json(
        capsys,
        "--root",
        str(root),
        "--today",
        "2030-01-01",
        "research",
        "record-claims",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(tmp_path / "in" / "claims.json", [claim]),
    )
    assert code == 0
    assert stale["confidence"][0]["freshness"] == "stale"
    assert stale["confidence"][0]["score"] == 0.6


def test_review_and_home_survive_a_malformed_evidence_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    corrupt = root / MEGAMIND_DIR / "evidence" / "evidence" / "corrupt.json"
    corrupt.parent.mkdir(parents=True, exist_ok=True)
    corrupt.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")

    code, review_doc, _ = run_json(capsys, "--root", str(root), "review")
    assert code == 0
    assert review_doc["schema_version"] == "megamind/review-report/v1"
    assert [row for row in review_doc["pending_evidence"] if row["decision"] == "invalid"]

    code, home, _ = run_json(capsys, "--root", str(root))
    assert code == 0
    assert home["schema_version"] == "megamind/home/v1"

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 1
    assert any(
        finding["check"] == "evidence" and finding["path"].endswith("corrupt.json")
        for finding in doctor["findings"]
    )


def test_doctor_reports_dangling_evidence_references(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    # Admission refuses this, so doctor's own referential pass is exercised by
    # planting the record the way a hand edit or a foreign tool would.
    quotation = validate_quotation(
        research_quotation(content_hash("https://absent.test/x" + "0" * 64)), RESEARCH_TEXT
    )
    planted = root / MEGAMIND_DIR / "evidence" / "quotations" / f"{quotation['quotation_id']}.json"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(json.dumps(quotation, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 1
    assert any(
        "cites unknown evidence record" in finding["message"] for finding in doctor["findings"]
    )


def test_quotations_and_claims_refuse_unknown_evidence_at_admission(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    normalized_file = tmp_path / "in" / "normalized.txt"
    normalized_file.parent.mkdir(parents=True, exist_ok=True)
    normalized_file.write_text(RESEARCH_TEXT, encoding="utf-8")
    ghost = content_hash("https://absent.test/x" + "0" * 64)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-quotations",
        "--wiki",
        "StarterWiki",
        "--normalized-file",
        str(normalized_file),
        "--input",
        write_json(tmp_path / "in" / "quotations.json", [research_quotation(ghost)]),
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert ghost in doc["message"]
    assert not (root / MEGAMIND_DIR / "evidence" / "quotations").exists()

    _, quotation = accepted_artifact(tmp_path, capsys, root)
    claim = research_claim(ghost, str(quotation["quotation_id"]), "the synthetic fact holds")
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-claims",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(tmp_path / "in" / "claims.json", [claim]),
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert ghost in doc["message"]
    assert not (root / MEGAMIND_DIR / "evidence" / "claims").exists()

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 0, doctor


def test_evidence_store_enforces_quotation_bindings_at_persistence(
    tmp_path: Path,
) -> None:
    root = governed_vault(tmp_path)
    store = EvidenceStore(root)
    record = research_evidence()
    evidence_id = str(record["evidence_id"])

    with pytest.raises(EvidenceError, match="unknown evidence"):
        store.put("quotations", research_quotation("missing-evidence"), RESEARCH_TEXT)

    store.put("evidence", record)

    foreign_text = "A different frozen fact."
    foreign_quotation = research_quotation(
        evidence_id,
        foreign_text,
        {"exact": "frozen fact", "prefix": "different ", "suffix": "."},
    )
    with pytest.raises(EvidenceError, match="not the normalized snapshot"):
        store.put("quotations", foreign_quotation, foreign_text)

    coordinated_record = research_evidence("https://authority.test/coordinated")
    coordinated_id = str(coordinated_record["evidence_id"])
    coordinated_quotation = research_quotation(coordinated_id)
    paths = store.put_all(
        [("evidence", coordinated_record), ("quotations", coordinated_quotation)],
        RESEARCH_TEXT,
    )
    assert [path.name for path in paths] == [
        f"{coordinated_id}.json",
        f"{coordinated_quotation['quotation_id']}.json",
    ]


def test_evidence_store_rejects_dangling_references_and_unproven_quotations(
    tmp_path: Path,
) -> None:
    root = governed_vault(tmp_path)
    store = EvidenceStore(root)

    with pytest.raises(EvidenceError, match="requires frozen normalized text"):
        store.put_all(
            [
                ("evidence", research_evidence()),
                ("quotations", research_quotation(str(research_evidence()["evidence_id"]))),
            ]
        )
    with pytest.raises(EvidenceError, match="unknown quotation"):
        store.put("claims", research_claim("missing-evidence", "missing-quotation", "synthetic"))

    first = proposed_claim("first")
    second = proposed_claim("second")
    contradiction = {
        "schema": CONTRADICTION_SCHEMA,
        "claim_ids": [str(first["claim_id"]), "missing-claim"],
        "claim_key": "synthetic-fact",
        "basis": "incompatible-value",
        "resolution": "unresolved",
        "resolution_detail": "",
        "gap_id": "",
        "created": "2026-08-13",
        "updated": "2026-08-13",
    }
    contradiction["contradiction_id"] = content_hash(
        json.dumps(
            {"claim_ids": sorted(contradiction["claim_ids"]), "claim_key": "synthetic-fact"},
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    with pytest.raises(EvidenceError, match="unknown claim"):
        store.put_all([("claims", first), ("claims", second), ("contradictions", contradiction)])


def test_evidence_store_requires_a_single_append_only_correction_chain(tmp_path: Path) -> None:
    root = governed_vault(tmp_path)
    store = EvidenceStore(root)
    record = research_evidence()
    evidence_id = str(record["evidence_id"])
    store.put("evidence", record)
    first = correction_notice(evidence_id, "expression_of_concern", "2026-08-13")
    store.put("corrections", first)
    first = validate_correction_notice(first)

    with pytest.raises(EvidenceError, match="exactly one initial"):
        store.put("corrections", correction_notice(evidence_id, "retracted", "2026-08-14"))

    second = correction_notice(evidence_id, "retracted", "2026-08-14", str(first["notice_id"]))
    store.put("corrections", second)


def test_invalid_correction_chains_resolve_restrictively() -> None:
    evidence_id = str(_evidence()["evidence_id"])
    notices = {
        "root": {"notice_id": "root", "evidence_id": evidence_id, "supersedes": ""},
        "left": {"notice_id": "left", "evidence_id": evidence_id, "supersedes": "right"},
        "right": {"notice_id": "right", "evidence_id": evidence_id, "supersedes": "left"},
    }
    with pytest.raises(EvidenceError, match="disconnected"):
        validate_correction_chain(evidence_id, notices)
    assert resolve_corrections(_evidence(), list(notices.values()))["status"] == "unknown"


def test_evidence_store_rejects_unknown_kinds_before_transaction_recovery(tmp_path: Path) -> None:
    root = governed_vault(tmp_path)
    journal = root / MEGAMIND_DIR / "evidence" / "transactions" / "pending.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("not a transaction journal\n", encoding="utf-8")
    store = EvidenceStore(root)

    operations = (
        lambda: store.put("typo", {}),
        lambda: store.put_all([("typo", {})]),
        lambda: store.get("typo", "record"),
        lambda: store.scan_readonly("typo"),
        lambda: store.scan("typo"),
    )
    for operation in operations:
        with pytest.raises(EvidenceError, match="unknown evidence store kind"):
            operation()
        assert journal.read_text(encoding="utf-8") == "not a transaction journal\n"


def test_record_quotations_commits_nothing_when_one_span_is_unwritable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record = research_evidence()
    normalized_file = tmp_path / "in" / "normalized.txt"
    normalized_file.parent.mkdir(parents=True, exist_ok=True)
    normalized_file.write_text(RESEARCH_TEXT, encoding="utf-8")
    code, _, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-artifact",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(tmp_path / "in" / "evidence.json", record),
    )
    assert code == 0
    good = research_quotation(str(record["evidence_id"]))
    bad = {**good, "position": {"start": 2, "end": 9999}}
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-quotations",
        "--wiki",
        "StarterWiki",
        "--normalized-file",
        str(normalized_file),
        "--input",
        write_json(tmp_path / "in" / "quotations.json", [good, bad]),
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert not (root / MEGAMIND_DIR / "evidence" / "quotations").exists()


def test_review_surfaces_deferred_evidence_and_unresolved_contradictions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-artifact",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(tmp_path / "in" / "evidence.json", research_evidence()),
    )
    run_json(
        capsys,
        "--root",
        str(root),
        "--today",
        "2026-08-13",
        "research",
        "reconcile",
        "--input",
        write_json(
            tmp_path / "in" / "claims.json",
            {
                "claims": [
                    proposed_claim("the synthetic fact is A"),
                    proposed_claim("the synthetic fact is B"),
                ]
            },
        ),
    )
    code, doc, _ = run_json(capsys, "--root", str(root), "review")
    assert code == 0
    assert doc["status"] == "attention"
    assert [row["decision"] for row in doc["pending_evidence"]] == ["deferred"]
    assert len(doc["contradictions"]) == 1


def test_research_policy_round_trips_through_registry_and_card(tmp_path: Path) -> None:
    root = governed_vault(tmp_path)
    reloaded = load_registry(root).wiki_by_name("StarterWiki")
    assert reloaded is not None
    assert reloaded.research_policy is not None
    assert reloaded.research_policy.to_data() == ResearchPolicy.from_data(POLICY_DATA).to_data()

    card_root = tmp_path / "canonical"
    init_wiki_root(card_root, "CardWiki")
    entry = load_wiki_card(card_root)
    entry.research_policy = ResearchPolicy.from_data(POLICY_DATA)
    save_wiki_card(card_root, entry)
    assert (card_root / CARD_PATH).is_file()
    card = load_wiki_card(card_root)
    assert card.research_policy is not None
    assert card.research_policy.permitted is True


def test_card_research_policy_governs_acceptance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    card_root = tmp_path / "canonical"
    init_wiki_root(card_root, "CardWiki")
    entry = load_wiki_card(card_root)
    entry.research_policy = ResearchPolicy.from_data(POLICY_DATA)
    save_wiki_card(card_root, entry)
    record = research_evidence()
    evidence_input = write_json(tmp_path / "in" / "evidence.json", record)
    normalized_file = tmp_path / "in" / "normalized.txt"
    normalized_file.write_text(RESEARCH_TEXT, encoding="utf-8")
    artifact = ["--root", str(card_root), "research", "record-artifact", "--wiki", "CardWiki"]
    code, _, _ = run_json(capsys, *artifact, "--input", evidence_input)
    assert code == 0
    run_json(
        capsys,
        "--root",
        str(card_root),
        "research",
        "record-quotations",
        "--wiki",
        "CardWiki",
        "--normalized-file",
        str(normalized_file),
        "--input",
        write_json(
            tmp_path / "in" / "quotations.json",
            [research_quotation(str(record["evidence_id"]))],
        ),
    )
    code, doc, _ = run_json(capsys, *artifact, "--input", evidence_input)
    assert code == 0
    assert doc["status"] == "accepted"
    assert doc["evidence"]["acceptance"]["quality"] == "primary"


def accepted_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    root: Path,
    record: dict[str, Any] | None = None,
    text: str = RESEARCH_TEXT,
    quote: dict[str, str] | None = None,
    label: str = "a",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Drive the supported path to a stored, accepted artifact and its span."""
    record = record if record is not None else research_evidence()
    evidence_input = write_json(tmp_path / "in" / f"evidence-{label}.json", record)
    normalized_file = tmp_path / "in" / f"normalized-{label}.txt"
    normalized_file.parent.mkdir(parents=True, exist_ok=True)
    normalized_file.write_text(text, encoding="utf-8")
    quotation = research_quotation(str(record["evidence_id"]), text, quote)
    artifact = ["--root", str(root), "research", "record-artifact", "--wiki", "StarterWiki"]
    run_json(capsys, *artifact, "--input", evidence_input)
    code, _, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-quotations",
        "--wiki",
        "StarterWiki",
        "--normalized-file",
        str(normalized_file),
        "--input",
        write_json(tmp_path / "in" / f"quotations-{label}.json", [quotation]),
    )
    assert code == 0
    code, doc, _ = run_json(capsys, *artifact, "--input", evidence_input)
    assert code == 0
    assert doc["status"] == "accepted"
    return record, quotation


def test_retraction_arrives_as_a_superseding_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record, _ = accepted_artifact(tmp_path, capsys, root)
    evidence_id = str(record["evidence_id"])

    code, notice_doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-correction",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(
            tmp_path / "in" / "notice.json",
            correction_notice(evidence_id, "retracted", "2026-09-01"),
        ),
    )
    assert code == 0
    assert notice_doc["status"] == "recorded"
    assert notice_doc["corrections"]["status"] == "retracted"

    # The frozen artifact is untouched; only the posture in force moved.
    stored = json.loads(
        (root / MEGAMIND_DIR / "evidence" / "evidence" / f"{evidence_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored["corrections"]["status"] == "clean"

    # Support is withdrawn immediately, before the artifact is re-recorded.
    quotation = research_quotation(evidence_id)
    claim = research_claim(evidence_id, str(quotation["quotation_id"]), "the synthetic fact holds")
    code, claims_doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "--today",
        "2026-09-02",
        "research",
        "record-claims",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(tmp_path / "in" / "claims.json", [claim]),
    )
    assert code == 1
    assert claims_doc["code"] == "evidence_invalid"

    code, review_doc, _ = run_json(capsys, "--root", str(root), "review")
    assert code == 0
    assert any(
        "retracted correction notice" in row["failure"] for row in review_doc["pending_evidence"]
    )

    # Re-deriving acceptance under the superseding posture rejects the artifact.
    code, rejected, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-artifact",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(tmp_path / "in" / "evidence.json", record),
    )
    assert code == 0
    assert rejected["status"] == "rejected"
    assert "G9" in rejected["evidence"]["acceptance"]["failure"]

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 0, doctor


def test_correction_chain_must_supersede_the_current_notice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record, _ = accepted_artifact(tmp_path, capsys, root)
    evidence_id = str(record["evidence_id"])
    first = correction_notice(evidence_id, "corrected", "2026-09-01")
    code, first_doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-correction",
        "--input",
        write_json(tmp_path / "in" / "first.json", first),
    )
    assert code == 0

    # A second notice that does not name the current head would fork the chain.
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-correction",
        "--input",
        write_json(
            tmp_path / "in" / "fork.json",
            correction_notice(evidence_id, "retracted", "2026-09-05"),
        ),
    )
    assert code == 2
    assert doc["code"] == "usage_error"
    assert "supersede" in doc["message"]

    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-correction",
        "--input",
        write_json(
            tmp_path / "in" / "second.json",
            correction_notice(
                evidence_id, "retracted", "2026-09-05", str(first_doc["notice"]["notice_id"])
            ),
        ),
    )
    assert code == 0
    assert doc["corrections"]["status"] == "retracted"

    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-correction",
        "--input",
        write_json(
            tmp_path / "in" / "dangling.json",
            correction_notice("0" * 12, "retracted", "2026-09-05"),
        ),
    )
    assert code == 2
    assert "unknown evidence record" in doc["message"]


def test_reconcile_from_claims_leaves_doctor_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "--today",
        "2026-08-13",
        "research",
        "reconcile",
        "--input",
        write_json(
            tmp_path / "in" / "claims.json",
            {
                "claims": [
                    proposed_claim("the synthetic fact is A"),
                    proposed_claim("the synthetic fact is B"),
                ]
            },
        ),
    )
    assert code == 0
    assert len(doc["claims"]) == 2
    assert len(doc["contradictions"]) == 1
    stored_claims = sorted(
        path.stem for path in (root / MEGAMIND_DIR / "evidence" / "claims").glob("*.json")
    )
    assert stored_claims == sorted(doc["contradictions"][0]["claim_ids"])

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 0, doctor


def test_reconcile_commits_nothing_when_a_claim_is_unwritable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    good = proposed_claim("the synthetic fact is A")
    bad = {**proposed_claim("the synthetic fact is B"), "lifecycle": "active"}
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "--today",
        "2026-08-13",
        "research",
        "reconcile",
        "--input",
        write_json(tmp_path / "in" / "claims.json", {"claims": [good, bad]}),
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert not (root / MEGAMIND_DIR / "evidence" / "claims").exists()
    assert not (root / MEGAMIND_DIR / "evidence" / "contradictions").exists()


def test_put_all_rolls_back_after_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = governed_vault(tmp_path)
    store = EvidenceStore(root)
    original_write = evidence_module.atomic_write
    calls = 0

    def fail_second_write(
        root: Path, target: object, content: str, *, durable: bool = False
    ) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic disk failure")
        return original_write(root, target, content, durable=durable)

    monkeypatch.setattr(evidence_module, "atomic_write", fail_second_write)
    with pytest.raises(OSError, match="synthetic disk failure"):
        store.put_all(
            [
                ("claims", proposed_claim("the synthetic fact is A")),
                ("claims", proposed_claim("the synthetic fact is B")),
            ]
        )
    assert not list((root / MEGAMIND_DIR / "evidence" / "claims").glob("*.json"))


def test_doctor_leaves_a_durable_interrupted_transaction_for_a_mutation_to_recover(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    store = EvidenceStore(root)
    interrupted = proposed_claim("the interrupted synthetic fact")
    rel = Path(MEGAMIND_DIR) / "evidence" / "claims" / f"{interrupted['claim_id']}.json"
    staged = json.dumps(interrupted, sort_keys=True, indent=2) + "\n"
    items = [
        {
            "path": rel.as_posix(),
            "previous": None,
            "staged_sha256": hashlib.sha256(staged.encode("utf-8")).hexdigest(),
        }
    ]
    transaction_id = content_hash(
        json.dumps({"items": items}, sort_keys=True, separators=(",", ":"))
    )
    journal = root / MEGAMIND_DIR / "evidence" / "transactions" / f"{transaction_id}.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "schema": "megamind/evidence-transaction/v1",
                "transaction_id": transaction_id,
                "items": items,
                "authority": hmac.new(
                    bytes.fromhex(store._transaction_authority(create=True)),
                    json.dumps({"items": items}, sort_keys=True, separators=(",", ":")).encode(),
                    hashlib.sha256,
                ).hexdigest(),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    interrupted_path = root / rel
    interrupted_path.parent.mkdir(parents=True, exist_ok=True)
    interrupted_path.write_text(staged, encoding="utf-8")
    code, doc, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 0
    assert interrupted_path.is_file()
    assert journal.is_file()
    assert doc["errors"] == 0

    EvidenceStore(root).put("claims", proposed_claim("the repaired synthetic fact"))
    assert not interrupted_path.exists()
    assert not journal.exists()
    backups = root / MEGAMIND_DIR / BACKUP_DIR
    assert list(backups.glob(f"{journal.name}.*.bak"))


def test_evidence_recovery_refuses_a_hostile_staged_journal(tmp_path: Path) -> None:
    root = governed_vault(tmp_path)
    store = EvidenceStore(root)
    store._transaction_authority(create=True)
    claim = proposed_claim("the existing synthetic fact")
    path = store.put("claims", claim)
    items = [
        {
            "path": path.relative_to(root).as_posix(),
            "previous": None,
            "staged_sha256": hashlib.sha256(b"different staged bytes").hexdigest(),
        }
    ]
    transaction_id = content_hash(json.dumps({"items": items}, sort_keys=True, separators=(",", ":")))
    journal = root / MEGAMIND_DIR / "evidence" / "transactions" / f"{transaction_id}.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        json.dumps(
            {
                "schema": "megamind/evidence-transaction/v1",
                "transaction_id": transaction_id,
                "items": items,
                "authority": "0" * 64,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(EvidenceError, match="transaction authority is invalid"):
        store.recover()

    assert path.is_file()
    assert journal.is_file()


def test_evidence_store_audits_canonical_correction_notice_id(tmp_path: Path) -> None:
    root = governed_vault(tmp_path)
    store = EvidenceStore(root)
    evidence = research_evidence()
    store.put("evidence", evidence)
    notice = correction_notice(str(evidence["evidence_id"]), "corrected", "2026-08-13")

    path = store.put("corrections", notice)

    canonical = validate_correction_notice(notice)
    assert path.name == f"{canonical['notice_id']}.json"


def test_active_claim_requires_currently_accepted_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record = research_evidence()
    normalized_file = tmp_path / "in" / "normalized.txt"
    normalized_file.parent.mkdir(parents=True)
    normalized_file.write_text(RESEARCH_TEXT, encoding="utf-8")
    run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-artifact",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(tmp_path / "in" / "evidence.json", record),
    )
    quotation = research_quotation(str(record["evidence_id"]))
    run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-quotations",
        "--wiki",
        "StarterWiki",
        "--normalized-file",
        str(normalized_file),
        "--input",
        write_json(tmp_path / "in" / "quotation.json", [quotation]),
    )
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-claims",
        "--input",
        write_json(
            tmp_path / "in" / "claim.json",
            [
                research_claim(
                    str(record["evidence_id"]), str(quotation["quotation_id"]), "Unaccepted fact"
                )
            ],
        ),
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert not (root / MEGAMIND_DIR / "evidence" / "claims").exists()


def test_research_status_degrades_on_an_unreadable_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    code, _, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "plan",
        "--input",
        write_json(
            tmp_path / "in" / "plan.json",
            {"gap_id": "gap-1", "wiki": "StarterWiki", "question": "q"},
        ),
    )
    assert code == 0
    bad = root / MEGAMIND_DIR / "research" / "jobs" / "bad.jsonl"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")

    code, doc, _ = run_json(capsys, "--root", str(root), "research", "status")
    assert code == 0
    assert doc["status"] == "attention"
    assert len(doc["plans"]) == 1
    assert [problem["record"] for problem in doc["problems"]] == ["bad"]
    assert any("doctor" in entry for entry in doc["help"])


def stored_claim_ids(tmp_path: Path, capsys: pytest.CaptureFixture[str], root: Path) -> list[str]:
    """Reconcile two claims into the store and return their ids."""
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "--today",
        "2026-08-13",
        "research",
        "reconcile",
        "--input",
        write_json(
            tmp_path / "in" / "seed-claims.json",
            {
                "claims": [
                    proposed_claim("the synthetic fact is A"),
                    proposed_claim("the synthetic fact is B"),
                ]
            },
        ),
    )
    assert code == 0
    return [str(claim["claim_id"]) for claim in doc["claims"]]


def test_packet_refuses_unknown_claim_and_contradiction_references(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)

    def packet(claim_ids: list[str], contradiction_ids: list[str]) -> dict[str, Any]:
        body = {
            "plan_id": "plan-1",
            "claim_ids": sorted(claim_ids),
            "contradiction_ids": sorted(contradiction_ids),
            "interpretation": "",
            "supported_by": [],
            "answerability": {},
            "confidence": "unknown",
        }
        return {
            "schema": PACKET_SCHEMA,
            "packet_id": content_hash(
                json.dumps({"schema": PACKET_SCHEMA, **body}, sort_keys=True, separators=(",", ":"))
            ),
            **body,
        }

    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "packet",
        "--input",
        write_json(tmp_path / "in" / "ghost.json", packet(["ghostclaim01"], [])),
    )
    assert code == 1
    assert doc["code"] == "research_invalid"
    assert "ghostclaim01" in doc["message"]
    assert not (root / MEGAMIND_DIR / "research" / "packets").exists()

    claim_ids = stored_claim_ids(tmp_path, capsys, root)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "packet",
        "--input",
        write_json(tmp_path / "in" / "ghost2.json", packet(claim_ids, ["ghostconflict"])),
    )
    assert code == 1
    assert "ghostconflict" in doc["message"]

    contradictions = sorted(
        path.stem for path in (root / MEGAMIND_DIR / "evidence" / "contradictions").glob("*.json")
    )
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "packet",
        "--input",
        write_json(tmp_path / "in" / "good.json", packet(claim_ids, contradictions)),
    )
    assert code == 0
    assert doc["status"] == "packet-ready"

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 0, doctor


def test_reconcile_refuses_a_contradiction_citing_unknown_claims(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    ids = sorted(["ghostclaim01", "ghostclaim02"])
    contradiction = {
        "schema": CONTRADICTION_SCHEMA,
        "contradiction_id": content_hash(
            json.dumps(
                {"claim_ids": ids, "claim_key": "synthetic-fact"},
                sort_keys=True,
                separators=(",", ":"),
            )
        ),
        "claim_ids": ids,
        "claim_key": "synthetic-fact",
        "basis": "incompatible-value",
        "resolution": "unresolved",
        "resolution_detail": "",
        "gap_id": "",
        "created": "2026-08-13",
        "updated": "2026-08-13",
    }
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "reconcile",
        "--input",
        write_json(tmp_path / "in" / "contradictions.json", {"contradictions": [contradiction]}),
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert "ghostclaim01" in doc["message"]
    assert not (root / MEGAMIND_DIR / "evidence" / "contradictions").exists()

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 0, doctor


def test_evidence_invalid_help_names_the_correction_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record = research_evidence()
    artifact = ["--root", str(root), "research", "record-artifact", "--wiki", "StarterWiki"]
    code, _, _ = run_json(
        capsys, *artifact, "--input", write_json(tmp_path / "in" / "evidence.json", record)
    )
    assert code == 0
    retracted = {**record, "corrections": {**record["corrections"], "status": "retracted"}}
    code, doc, _ = run_json(
        capsys, *artifact, "--input", write_json(tmp_path / "in" / "retracted.json", retracted)
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert any("record-correction" in entry for entry in doc["help"])


SECOND_TEXT = "A different frozen record about the synthetic product."
SECOND_QUOTE = {"exact": "frozen record", "prefix": "different ", "suffix": " about"}


def test_claim_support_must_pair_a_span_with_its_own_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    first, first_span = accepted_artifact(tmp_path, capsys, root, label="a")
    second, second_span = accepted_artifact(
        tmp_path,
        capsys,
        root,
        research_evidence("https://authority.test/other", SECOND_TEXT),
        SECOND_TEXT,
        SECOND_QUOTE,
        label="b",
    )
    assert first["evidence_id"] != second["evidence_id"]

    def record_claim(claim: dict[str, Any], name: str) -> tuple[int, dict[str, Any]]:
        code, doc, _ = run_json(
            capsys,
            "--root",
            str(root),
            "--today",
            "2026-09-02",
            "research",
            "record-claims",
            "--wiki",
            "StarterWiki",
            "--input",
            write_json(tmp_path / "in" / f"claim-{name}.json", [claim]),
        )
        return code, doc

    # The span the second artifact actually produced is honest support for it.
    code, honest = record_claim(
        research_claim(
            str(second["evidence_id"]),
            str(second_span["quotation_id"]),
            "the second synthetic fact holds",
        ),
        "honest",
    )
    assert code == 0
    assert honest["confidence"][0]["score"] == 0.9

    # Crediting the first artifact's span to the second is refused outright.
    mispaired = research_claim(
        str(second["evidence_id"]),
        str(first_span["quotation_id"]),
        "the mispaired synthetic fact holds",
    )
    code, doc = record_claim(mispaired, "mispaired")
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert str(first_span["quotation_id"]) in doc["message"]
    assert str(first["evidence_id"]) in doc["message"]

    # A retraction of the first artifact cannot be escaped by relabelling.
    code, _, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-correction",
        "--input",
        write_json(
            tmp_path / "in" / "notice.json",
            correction_notice(str(first["evidence_id"]), "retracted", "2026-09-01"),
        ),
    )
    assert code == 0
    code, doc = record_claim(mispaired, "mispaired-after-retraction")
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    code, withdrawn = record_claim(
        research_claim(
            str(first["evidence_id"]),
            str(first_span["quotation_id"]),
            "the first synthetic fact holds",
        ),
        "retracted",
    )
    assert code == 1
    assert withdrawn["code"] == "evidence_invalid"

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 0, doctor


def test_doctor_reports_a_mispaired_claim_support(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    first, first_span = accepted_artifact(tmp_path, capsys, root, label="a")
    second, _ = accepted_artifact(
        tmp_path,
        capsys,
        root,
        research_evidence("https://authority.test/other", SECOND_TEXT),
        SECOND_TEXT,
        SECOND_QUOTE,
        label="b",
    )
    # Admission refuses this, so doctor's own pairing check is exercised by
    # planting the record the way a hand edit or a foreign tool would.
    claim = validate_claim(
        research_claim(
            str(second["evidence_id"]),
            str(first_span["quotation_id"]),
            "the mispaired synthetic fact holds",
        )
    )
    planted = root / MEGAMIND_DIR / "evidence" / "claims" / f"{claim['claim_id']}.json"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(json.dumps(claim, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 1
    assert any(
        "is bound to" in finding["message"] and str(first["evidence_id"]) in finding["message"]
        for finding in doctor["findings"]
    )


def test_doctor_reports_a_correction_notice_from_another_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    first, _ = accepted_artifact(tmp_path, capsys, root, label="a")
    second, _ = accepted_artifact(
        tmp_path,
        capsys,
        root,
        research_evidence("https://authority.test/other", SECOND_TEXT),
        SECOND_TEXT,
        SECOND_QUOTE,
        label="b",
    )
    code, notice_doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-correction",
        "--input",
        write_json(
            tmp_path / "in" / "notice.json",
            correction_notice(str(first["evidence_id"]), "retracted", "2026-09-01"),
        ),
    )
    assert code == 0
    # A notice for the second artifact may not supersede the first's chain.
    crossed = validate_correction_notice(
        correction_notice(
            str(second["evidence_id"]),
            "retracted",
            "2026-09-02",
            str(notice_doc["notice"]["notice_id"]),
        )
    )
    planted = root / MEGAMIND_DIR / "evidence" / "corrections" / f"{crossed['notice_id']}.json"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(json.dumps(crossed, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 1
    assert any(
        "belongs to a different evidence record" in finding["message"]
        for finding in doctor["findings"]
    )


FORGED_TEXT = "A forged claim about the synthetic product."
FORGED_QUOTE = {"exact": "forged claim", "prefix": "A ", "suffix": " about"}


def test_quotation_must_be_bound_to_its_artifact_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record = research_evidence()
    evidence_id = str(record["evidence_id"])
    evidence_input = write_json(tmp_path / "in" / "evidence.json", record)
    artifact = ["--root", str(root), "research", "record-artifact", "--wiki", "StarterWiki"]
    code, deferred, _ = run_json(capsys, *artifact, "--input", evidence_input)
    assert code == 0
    assert deferred["status"] == "deferred"

    # A span whose against_hash is the digest of text the host chose, not of
    # the artifact's frozen snapshot.
    forged_file = tmp_path / "in" / "forged.txt"
    forged_file.parent.mkdir(parents=True, exist_ok=True)
    forged_file.write_text(FORGED_TEXT, encoding="utf-8")
    forged = research_quotation(evidence_id, FORGED_TEXT, FORGED_QUOTE)
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-quotations",
        "--wiki",
        "StarterWiki",
        "--normalized-file",
        str(forged_file),
        "--input",
        write_json(tmp_path / "in" / "forged.json", [forged]),
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert str(forged["against_hash"]) in doc["message"]
    assert evidence_id in doc["message"]
    assert not (root / MEGAMIND_DIR / "evidence" / "quotations").exists()

    # The acceptance gate the forged span was meant to flip stays unknown.
    code, still_deferred, _ = run_json(capsys, *artifact, "--input", evidence_input)
    assert code == 0
    assert still_deferred["status"] == "deferred"
    gates = {g["gate"]: g["verdict"] for g in still_deferred["evidence"]["acceptance"]["gates"]}
    assert gates["G10"] == "unknown"

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 0, doctor


def test_quotation_refuses_another_artifacts_snapshot_hash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    first, _ = accepted_artifact(tmp_path, capsys, root, label="a")
    second = research_evidence("https://authority.test/other", SECOND_TEXT)
    code, _, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-artifact",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(tmp_path / "in" / "evidence-b.json", second),
    )
    assert code == 0
    normalized_file = tmp_path / "in" / "normalized-a.txt"
    normalized_file.parent.mkdir(parents=True, exist_ok=True)
    normalized_file.write_text(RESEARCH_TEXT, encoding="utf-8")

    # The first artifact's snapshot digest, offered as the second's.
    crossed = research_quotation(str(second["evidence_id"]), RESEARCH_TEXT)
    assert crossed["against_hash"] == first["snapshot"]["normalized_sha256"]
    code, doc, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-quotations",
        "--wiki",
        "StarterWiki",
        "--normalized-file",
        str(normalized_file),
        "--input",
        write_json(tmp_path / "in" / "crossed.json", [crossed]),
    )
    assert code == 1
    assert doc["code"] == "evidence_invalid"
    assert str(second["evidence_id"]) in doc["message"]


def test_doctor_reports_a_quotation_bound_to_foreign_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = governed_vault(tmp_path)
    record = research_evidence()
    code, _, _ = run_json(
        capsys,
        "--root",
        str(root),
        "research",
        "record-artifact",
        "--wiki",
        "StarterWiki",
        "--input",
        write_json(tmp_path / "in" / "evidence.json", record),
    )
    assert code == 0
    # Admission refuses this, so doctor's own binding check is exercised by
    # planting the record the way a hand edit or a foreign tool would.
    forged = validate_quotation(
        research_quotation(str(record["evidence_id"]), FORGED_TEXT, FORGED_QUOTE), FORGED_TEXT
    )
    planted = root / MEGAMIND_DIR / "evidence" / "quotations" / f"{forged['quotation_id']}.json"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(json.dumps(forged, sort_keys=True, indent=2) + "\n", encoding="utf-8")

    code, doctor, _ = run_json(capsys, "--root", str(root), "doctor")
    assert code == 1
    assert any(
        "is not the normalized snapshot of" in finding["message"] for finding in doctor["findings"]
    )
