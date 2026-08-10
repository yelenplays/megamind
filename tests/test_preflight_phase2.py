"""Phase 2 preflight: thresholds, confidence/evidence packets, semantic safety."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from megamind.catalog import RootRef
from megamind.preflight import run_preflight
from megamind.registry import WikiEntry, load_registry, save_registry
from megamind.semantic import NgramBackend

TODAY = date(2026, 8, 10)


def _ref(root: Path) -> RootRef:
    return RootRef(label=str(root), path=root)


# --- thresholds ---------------------------------------------------------------


def test_confident_match_loads_automatically(vault: Path) -> None:
    result = run_preflight([_ref(vault)], "how does pricing work", "cloud")
    assert result.status == "matched"
    assert result.confidence is not None and result.confidence >= 0.75
    assert result.thresholds["reliance_floor"] == 0.75


def test_sub_floor_match_offers_choices_without_loading(vault: Path) -> None:
    result = run_preflight([_ref(vault)], "knowledge base", "local")
    assert result.status == "ambiguous"
    assert result.matches == []
    assert [str(offer["name"]) for offer in result.offers] == ["ProductWiki"]
    assert result.confidence is not None and 0.25 <= result.confidence < 0.75
    assert any("reliance floor" in note for note in result.notes)
    # an offer never carries loadable paths or a follow-up command
    for offer in result.offers:
        assert "allows" not in offer
        assert "follow_up" not in offer


def test_sub_floor_wikis_stay_offers_inside_a_confident_match(vault: Path) -> None:
    """A confident top match never drags weaker wikis into loadable `matches`."""
    result = run_preflight([_ref(vault)], "pricing synthetic", "local")
    assert result.status == "matched"
    assert [str(match["name"]) for match in result.matches] == ["ProductWiki"]
    for match in result.matches:
        assert match["confidence"]["meets_floor"] is True  # type: ignore[index]
        assert "allows" in match and "follow_up" in match
    assert len(result.offers) > 1  # the weaker wikis are still surfaced, just not loadable
    for offer in result.offers:
        assert offer["confidence"]["meets_floor"] is False  # type: ignore[index]
        assert "allows" not in offer
        assert "follow_up" not in offer
    assert "ProductWiki" not in [str(offer["name"]) for offer in result.offers]


def test_below_no_match_floor_stays_quiet(vault: Path) -> None:
    result = run_preflight(
        [_ref(vault)], "synthetic aardvark bebop crimson dazzle epsilon", "local"
    )
    assert result.status == "no-match"
    assert result.matches == [] and result.offers == []
    assert result.confidence is None
    assert any("no-match floor" in note for note in result.notes)


def test_ambiguity_band_offers_near_ties(vault: Path) -> None:
    registry = load_registry(vault)
    for name in ("ZedAlpha", "ZedBeta"):
        (vault / name).mkdir()
        registry.wikis.append(
            WikiEntry(
                name=name,
                path=name,
                privacy="public-reference",
                keywords=["zeppelin"],
                sensitivity="public-reference",
            )
        )
    save_registry(vault, registry)
    result = run_preflight([_ref(vault)], "zeppelin", "local")
    assert result.status == "ambiguous"
    assert result.confidence == 1.0
    assert any("ambiguity band" in note for note in result.notes)


# --- evidence packets ---------------------------------------------------------


def test_matches_carry_confidence_freshness_and_evidence(vault: Path) -> None:
    result = run_preflight([_ref(vault)], "how does pricing work", "cloud")
    match = result.matches[0]
    confidence = match["confidence"]
    assert isinstance(confidence, dict)
    assert confidence["score"] == result.confidence
    assert confidence["meets_floor"] is True
    freshness = match["freshness"]
    assert isinstance(freshness, dict)
    assert set(freshness) == {"half_life_days", "last_confirmed", "stale"}
    evidence = match["evidence"]
    assert isinstance(evidence, dict)
    assert any("trigger match: pricing" in r for r in evidence["lexical"])  # type: ignore[operator]
    assert evidence["semantic"] is None  # semantic disabled: no fabricated score
    # the packet stays card-level: no page content path is ever handed out
    assert "topics/" not in json.dumps(match)


def test_freshness_stays_unknown_without_a_reference_date(vault: Path) -> None:
    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    assert product is not None
    product.freshness.half_life_days = 90
    product.freshness.last_confirmed = "2026-08-01"
    save_registry(vault, registry)

    undated = run_preflight([_ref(vault)], "pricing", "local")
    assert undated.matches[0]["freshness"]["stale"] is None  # type: ignore[index]

    from megamind.catalog import build_catalog

    catalog = build_catalog([_ref(vault)], today=date(2026, 12, 31))
    dated = run_preflight([_ref(vault)], "pricing", "local", catalog)
    assert dated.matches[0]["freshness"]["stale"] is True  # type: ignore[index]


def test_v1_registry_gets_the_same_thresholds(tmp_path: Path) -> None:
    """Phase 1 v1 registries keep loading and get Phase 2 decisions unchanged in shape."""
    root = tmp_path / "v1vault"
    (root / ".megamind").mkdir(parents=True)
    (root / ".megamind" / "registry.json").write_text(
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
                        "card": "",
                        "digest": "",
                        "index": "",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "NotesWiki").mkdir()
    assert load_registry(root).version == 1

    strong = run_preflight([_ref(root)], "cider press", "local")
    assert strong.status == "matched"
    assert strong.confidence is not None and strong.confidence >= 0.75

    quiet = run_preflight([_ref(root)], "aardvark bebop crimson", "local")
    assert quiet.status == "no-match"
    assert quiet.matches == [] and quiet.offers == []


# --- semantic layer ------------------------------------------------------------


def test_semantic_is_disabled_by_default_and_typed(vault: Path) -> None:
    result = run_preflight([_ref(vault)], "pricing", "local")
    assert result.semantic["status"] == "disabled"
    assert result.semantic["backend"] == "none"


def test_semantic_fallback_is_typed_and_keeps_policy(vault: Path) -> None:
    class CorruptBackend:
        name = "fake-corrupt"

        def unavailable_reason(self) -> str | None:
            return None

        def similarity(self, query: str, text: str) -> float:
            raise RuntimeError("corrupt index")

    plain = run_preflight([_ref(vault)], "pricing", "local")
    broken = run_preflight([_ref(vault)], "pricing", "local", semantic=CorruptBackend())
    assert broken.semantic["status"] == "error"
    assert broken.semantic["reason"]
    assert broken.status == plain.status
    assert [str(m["name"]) for m in broken.matches] == [str(m["name"]) for m in plain.matches]


def test_semantic_rerank_keeps_digest_and_pointer_access_boundaries(vault: Path) -> None:
    digest = run_preflight(
        [_ref(vault)], "research interview findings", "local", semantic=NgramBackend()
    )
    assert digest.status == "matched"
    assert digest.matches[0]["allows"] == ["ResearchDigest/DIGEST.md"]

    pointer = run_preflight([_ref(vault)], "archive history", "local", semantic=NgramBackend())
    assert pointer.status == "matched"
    assert pointer.matches[0]["allows"] == []
    assert "manually" in str(pointer.matches[0]["follow_up"])  # location metadata only
