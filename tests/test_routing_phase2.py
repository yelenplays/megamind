"""Phase 2 routing: thresholds, confidence, freshness, v1 compatibility."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from conftest import write
from megamind.registry import load_registry, save_registry
from megamind.routing import route

TODAY = date(2026, 8, 10)


def test_strong_match_decides_load(vault: Path) -> None:
    result = route(vault, load_registry(vault), "what is our pricing model?")
    assert result.decision == "load"
    assert result.confidence is not None and result.confidence >= 0.75
    assert result.thresholds == {
        "reliance_floor": 0.75,
        "offer_floor": 0.25,
        "ambiguity_band": 0.05,
    }


def test_weak_match_offers_without_loading(vault: Path) -> None:
    result = route(vault, load_registry(vault), "synthetic knowledge")
    assert result.matched  # candidates exist, but nothing may auto-load
    assert result.decision == "offer"
    assert result.confidence is not None and 0.25 <= result.confidence < 0.75
    assert any("reliance floor" in note for note in result.notes)


def test_below_no_match_floor_drops_weak_evidence(vault: Path) -> None:
    result = route(vault, load_registry(vault), "knowledge aardvark bebop crimson dazzle epsilon")
    assert not result.matched
    assert result.decision == "no-match"
    assert result.candidates == []
    assert result.confidence is None
    assert any("no-match floor" in note for note in result.notes)


def test_load_packet_carries_no_sub_floor_candidate(vault: Path) -> None:
    """`load` authorizes opening every returned path, so weak rows must not ride along."""
    registry = load_registry(vault)
    from megamind.registry import WikiEntry

    (vault / "EchoWiki").mkdir()
    registry.wikis.append(
        WikiEntry(
            name="EchoWiki",
            path="EchoWiki",
            privacy="public-reference",
            description="Synthetic echo prose that merely mentions pricing.",
            keywords=["echo"],
        )
    )
    save_registry(vault, registry)

    result = route(vault, load_registry(vault), "pricing")
    assert result.decision == "load"
    assert all(candidate.confidence >= 0.75 for candidate in result.candidates)
    assert "EchoWiki" not in [candidate.wiki for candidate in result.candidates]
    assert any("reliance floor" in note and "EchoWiki" in note for note in result.notes)


def test_ambiguity_band_offers_a_choice(vault: Path) -> None:
    registry = load_registry(vault)
    from megamind.registry import WikiEntry

    for name in ("ZedAlpha", "ZedBeta"):
        (vault / name).mkdir()
        registry.wikis.append(
            WikiEntry(name=name, path=name, privacy="public-reference", keywords=["zeppelin"])
        )
    save_registry(vault, registry)
    result = route(vault, load_registry(vault), "zeppelin")
    assert result.decision == "offer"
    assert result.matched
    assert len(result.candidates) == 2
    assert any("ambiguity band" in note for note in result.notes)


def test_freshness_is_computed_only_with_a_reference_date(vault: Path) -> None:
    registry = load_registry(vault)
    undated = route(vault, registry, "pricing model")
    best = undated.candidates[0]
    assert best.freshness["updated"] == "2026-01-10"
    assert best.freshness["age_days"] is None
    assert best.freshness["stale"] is None  # unknown without --today, never guessed

    dated = route(vault, registry, "pricing model", today=TODAY)
    stale = dated.candidates[0].freshness
    assert stale["updated"] == "2026-01-10"
    assert stale["age_days"] == 212
    assert stale["stale"] is True


def test_pointer_candidate_freshness_reads_nothing(vault: Path) -> None:
    result = route(vault, load_registry(vault), "archive history", today=TODAY)
    pointer = result.candidates[0]
    assert pointer.kind == "pointer"
    assert pointer.freshness == {"updated": None, "age_days": None, "stale": None}


def test_route_output_is_repeatable_with_all_options(vault: Path) -> None:
    from megamind.semantic import NgramBackend

    registry = load_registry(vault)
    first = route(vault, registry, "brand color palette", today=TODAY, semantic=NgramBackend())
    second = route(vault, registry, "brand color palette", today=TODAY, semantic=NgramBackend())
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_v1_registry_routes_with_confidence_and_thresholds(tmp_path: Path) -> None:
    """A v1 registry gets the same Phase 2 thresholds with restrictive defaults."""
    root = tmp_path / "v1vault"
    write(
        root,
        ".megamind/registry.json",
        json.dumps(
            {
                "version": 1,
                "wikis": [
                    {
                        "name": "NotesWiki",
                        "path": "NotesWiki",
                        "privacy": "public-reference",
                        "description": "Synthetic cider notes.",
                        "keywords": ["cider"],
                        "card": "NotesWiki/CARD.md",
                        "digest": "",
                        "index": "",
                    }
                ],
            }
        )
        + "\n",
    )
    write(
        root,
        "NotesWiki/CARD.md",
        "---\nmegamind: routing-card\nwiki: NotesWiki\nprivacy: public-reference\n"
        "keywords: [cider]\n---\n\n# NotesWiki card\n\nAnswers synthetic cider questions.\n",
    )
    assert load_registry(root).version == 1

    strong = route(root, load_registry(root), "cider press")
    assert strong.decision == "load"
    assert strong.confidence is not None and strong.confidence >= 0.75

    weak = route(root, load_registry(root), "synthetic notes about nothing much at all really")
    assert weak.matched or weak.decision == "no-match"
    if weak.matched:
        assert weak.decision == "offer"
        assert weak.confidence is not None and weak.confidence < 0.75
