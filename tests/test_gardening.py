from __future__ import annotations

import json
from pathlib import Path

import pytest

from megamind.card import load_wiki_card
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
    validate_gap_journal,
)
from megamind.registry import RegistryError, load_registry, save_registry
from megamind.scaffold import init_vault, init_wiki_root


def make_criteria(**overrides: object) -> ProvisionCriteria:
    """The fully-qualified synthetic criteria set; override one field per case."""
    fields: dict[str, object] = {
        "accepted_domain": "synthetic release operations",
        "repeat_demand": 3,
        "multi_topic": 2,
        "overlap": "overlap with repeated queries",
        "scope": "release notes",
        "exclusions": "payroll",
        "owner": "owner@example.invalid",
        "source_policy": "synthetic sources",
        "privacy": "company-private",
        "model_access": "local",
        "seed_topics": ("release", "operations"),
        "maintenance": "quarterly review",
    }
    fields.update(overrides)
    return ProvisionCriteria(**fields)  # type: ignore[arg-type]


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


def test_cooldown_survives_a_later_transition_that_omits_it(tmp_path: Path) -> None:
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("ProductWiki", "Rate limits", today="2026-01-01"))
    store.transition(gap.gap_id, "nominated", today="2026-01-02")
    store.attempt(gap.gap_id, "no source", cooldown_until="2026-02-01", today="2026-01-03")
    paused = store.transition(gap.gap_id, "paused", today="2026-01-04")
    assert paused.cooldown_until == "2026-02-01"
    assert store.get(gap.gap_id).cooldown_until == "2026-02-01"
    cleared = store.transition(gap.gap_id, "open", cooldown_until="", today="2026-01-05")
    assert cleared.cooldown_until == ""


def test_gap_audit_records_carry_the_mutation_details(tmp_path: Path) -> None:
    store = GapStore(tmp_path)
    gap = store.create(GapRecord.new("ProductWiki", "Rate limits", today="2026-01-01"))
    store.transition(gap.gap_id, "planned", today="2026-01-02")
    store.attempt(gap.gap_id, "no source", correlation_id="c1", today="2026-01-03")
    events = [
        json.loads(line)
        for line in (tmp_path / ".megamind/audit/log.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert [entry["event"] for entry in events] == ["created", "transition", "attempt"]
    assert events[1]["status"] == "planned"
    assert events[2]["attempt_id"] == store.get(gap.gap_id).attempts[0]["attempt_id"]
    assert all(entry["gap_id"] == gap.gap_id for entry in events)
    assert all(entry["audit_ref"] == f"gaps.jsonl:{gap.gap_id}" for entry in events)


@pytest.mark.parametrize("line", ["[]", "3", "null", '"text"', "{}", '{"schema": "other"}'])
def test_malformed_journal_lines_are_typed_errors_not_crashes(tmp_path: Path, line: str) -> None:
    (tmp_path / ".megamind").mkdir(parents=True)
    (tmp_path / ".megamind/gaps.jsonl").write_text(line + "\n", encoding="utf-8")
    with pytest.raises(GardenError, match="invalid gap journal entry"):
        GapStore(tmp_path).records()
    assert validate_gap_journal(tmp_path)


def test_research_result_rejects_a_source_without_an_origin(tmp_path: Path) -> None:
    gap = GapRecord.new("A", "topic")
    wave = plan_research_wave([gap], gap.gap_id, CapacityInput(True, 0, {}, 100, 25))
    nomination = make_nomination(type("Wave", (), {"wave_id": wave.wave_id})(), wave.nominations[0])
    for sources in ([{"summary": "s", "eligible": True}], [{"origin": 7, "eligible": True}]):
        with pytest.raises(GardenError, match="origin"):
            ingest_research_result(
                tmp_path,
                nomination,
                {"correlation_id": nomination.correlation_id, "sources": sources},
            )
    assert not list((tmp_path / ".megamind/proposals").glob("*.json"))


@pytest.mark.parametrize("payload", [[], "text", 3])
def test_research_bridge_rejects_non_object_payloads(tmp_path: Path, payload: object) -> None:
    gap = GapRecord.new("A", "topic")
    wave = plan_research_wave([gap], gap.gap_id, CapacityInput(True, 0, {}, 100, 25))
    with pytest.raises(GardenError, match="JSON object"):
        make_nomination(type("Wave", (), {"wave_id": wave.wave_id})(), payload)  # type: ignore[arg-type]
    nomination = make_nomination(type("Wave", (), {"wave_id": wave.wave_id})(), wave.nominations[0])
    with pytest.raises(GardenError, match="JSON object"):
        ingest_research_result(tmp_path, nomination, payload)  # type: ignore[arg-type]


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
    init_vault(tmp_path, starter=False)
    files = provision_local_wiki(
        tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria(), today="2026-01-01"
    )
    assert ".megamind/registry.json" in files
    registry = load_registry(tmp_path)
    assert registry.wiki_by_name("ReleaseWiki").provisional is True  # type: ignore[union-attr]
    card = json.loads((tmp_path / "ReleaseWiki/.megamind/wiki-card.json").read_text())
    assert card["provisional"] is True
    with pytest.raises(GardenError):
        make_criteria(accepted_domain="", repeat_demand=1, seed_topics=("one",)).validate()


def test_provision_card_paths_are_rooted_at_the_wiki(tmp_path: Path) -> None:
    """The nested card is authoritative for its own root, so its paths resolve
    from there; the registry entry keeps the vault-relative ones."""
    init_vault(tmp_path, starter=False)
    provision_local_wiki(tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria())
    wiki_root = tmp_path / "ReleaseWiki"
    card = load_wiki_card(wiki_root)
    assert card.path == "."
    assert (card.card, card.index) == ("CARD.md", "INDEX.md")
    assert (wiki_root / card.card).is_file()
    assert (wiki_root / card.index).is_file()
    entry = load_registry(tmp_path).wiki_by_name("ReleaseWiki")
    assert entry is not None
    assert (entry.card, entry.index) == ("ReleaseWiki/CARD.md", "ReleaseWiki/INDEX.md")
    assert (tmp_path / entry.card).is_file()


def test_provision_refuses_to_create_a_dual_shape_root(tmp_path: Path) -> None:
    init_wiki_root(tmp_path, "MyWiki")
    with pytest.raises(GardenError, match="canonical single-wiki root"):
        provision_local_wiki(tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria())
    assert not (tmp_path / ".megamind/registry.json").exists()
    assert not (tmp_path / "ReleaseWiki").exists()


def test_provision_refuses_to_bootstrap_an_uninitialized_root(tmp_path: Path) -> None:
    with pytest.raises(RegistryError) as error:
        provision_local_wiki(tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria())
    assert error.value.code == "not_initialized"
    assert not (tmp_path / ".megamind").exists()
    assert not (tmp_path / "ReleaseWiki").exists()


def test_provision_refuses_a_v1_registry_instead_of_migrating_it(tmp_path: Path) -> None:
    init_vault(tmp_path, starter=False)
    registry = load_registry(tmp_path)
    registry.version = 1
    save_registry(tmp_path, registry)
    with pytest.raises(GardenError, match="migrate"):
        provision_local_wiki(tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria())
    assert load_registry(tmp_path).version == 1
    assert not (tmp_path / "ReleaseWiki").exists()


def test_provision_validates_the_registry_plan_before_writing_anything(tmp_path: Path) -> None:
    init_vault(tmp_path, starter=False)
    absolute = str(tmp_path / "ReleaseWiki")
    with pytest.raises(RegistryError, match="root-relative"):
        provision_local_wiki(tmp_path, "ReleaseWiki", absolute, make_criteria())
    assert not (tmp_path / "ReleaseWiki").exists()
    assert load_registry(tmp_path).wiki_by_name("ReleaseWiki") is None
    # A retry with the correct relative path is not blocked by an orphan scaffold.
    provision_local_wiki(tmp_path, "ReleaseWiki", "ReleaseWiki", make_criteria())
    assert load_registry(tmp_path).wiki_by_name("ReleaseWiki") is not None
