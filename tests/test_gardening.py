from __future__ import annotations

import json
from pathlib import Path

import pytest

from megamind.gardening import (
    CapacityInput,
    GapRecord,
    GapStore,
    GardenError,
    PriorityInputs,
    ProvisionCriteria,
    ingest_research_result,
    make_nomination,
    plan_research_wave,
    provision_local_wiki,
)
from megamind.registry import load_registry


def test_gap_identity_lifecycle_and_replay(tmp_path: Path) -> None:
    store = GapStore(tmp_path)
    first = store.create(
        GapRecord.new(
            "ProductWiki",
            "Rate limits",
            "weak",
            priority=PriorityInputs(4, 3, 2, 1, 4),
            today="2026-01-01",
        )
    )
    assert store.create(GapRecord.new("ProductWiki", "rate  limits", "weak")) == first
    store.transition(first.gap_id, "rejected", reason="needs owner review", today="2026-01-02")
    reopened = store.transition(first.gap_id, "open", today="2026-01-03")
    assert reopened.reopened_from == "rejected"
    attempted = store.attempt(first.gap_id, "no eligible source", correlation_id="c1")
    assert attempted.attempts[0]["correlation_id"] == "c1"
    assert len((tmp_path / ".megamind/gaps.jsonl").read_text().splitlines()) == 4


def test_wave_is_one_hop_and_capacity_is_typed() -> None:
    direct = GapRecord.new("A", "one", related_topics=["two", "three", "four"])
    wave = plan_research_wave(
        [direct],
        direct.gap_id,
        CapacityInput(True, 0, {}, 100, 25),
        today="2026-01-01",
    )
    assert wave.status == "planned"
    assert len(wave.nominations) == 3
    assert len(wave.deferred_nominations) == 1
    paused = plan_research_wave([direct], direct.gap_id, CapacityInput(False, 0, {}, None, None))
    assert paused.status == "paused"
    assert paused.reason == "capacity_unknown"
    refused = plan_research_wave([direct], direct.gap_id, CapacityInput(True, 0, {}, 100, 24))
    assert refused.status == "refused"
    assert refused.reason == "quota_reserve_below_25_percent"


def test_research_bridge_is_eligible_bounded_and_idempotent(tmp_path: Path) -> None:
    gap = GapRecord.new("A", "topic")
    wave = plan_research_wave([gap], gap.gap_id, CapacityInput(True, 0, {}, 100, 25))
    nomination = make_nomination(type("Wave", (), {"wave_id": wave.wave_id})(), wave.nominations[0])
    result = ingest_research_result(
        tmp_path,
        nomination,
        {
            "correlation_id": nomination.correlation_id,
            "sources": [
                {"origin": "synthetic-source", "summary": "safe summary", "eligible": True},
                {"origin": "private-source", "summary": "CANARY_SECRET", "eligible": False},
            ],
        },
    )
    assert result.status == "proposed"
    proposal = json.loads((tmp_path / result.ingest_proposal).read_text())
    assert "CANARY_SECRET" not in json.dumps(proposal)
    again = ingest_research_result(
        tmp_path,
        nomination,
        {
            "correlation_id": nomination.correlation_id,
            "sources": [
                {"origin": "synthetic-source", "summary": "safe summary", "eligible": True}
            ],
        },
    )
    assert again.ingest_proposal == result.ingest_proposal
    assert len(list((tmp_path / ".megamind/proposals").glob("research-ingest-*.json"))) == 1


def test_provision_requires_all_criteria_and_registers_restrictively(tmp_path: Path) -> None:
    criteria = ProvisionCriteria(
        "synthetic release operations",
        3,
        2,
        "overlap with repeated queries",
        "release notes",
        "payroll",
        "owner@example.invalid",
        "synthetic sources",
        "company-private",
        "local",
        ("release", "operations"),
        "quarterly review",
    )
    files = provision_local_wiki(
        tmp_path, "ReleaseWiki", "ReleaseWiki", criteria, today="2026-01-01"
    )
    assert ".megamind/registry.json" in files
    registry = load_registry(tmp_path)
    assert registry.wiki_by_name("ReleaseWiki").provisional is True  # type: ignore[union-attr]
    card = json.loads((tmp_path / "ReleaseWiki/.megamind/wiki-card.json").read_text())
    assert card["provisional"] is True
    with pytest.raises(GardenError):
        ProvisionCriteria(
            "", 1, 1, "", "", "", "", "", "company-private", "local", ("one",), ""
        ).validate()
