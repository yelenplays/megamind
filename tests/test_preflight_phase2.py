"""Phase 2 preflight: thresholds, confidence/evidence packets, semantic safety."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from megamind.card import load_wiki_card, save_wiki_card
from megamind.catalog import RootRef, discover_roots
from megamind.preflight import run_preflight
from megamind.registry import ContextBudget, WikiEntry, load_registry, save_registry
from megamind.scaffold import init_wiki_root
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


def test_rows_outside_the_ambiguity_band_are_named(vault: Path) -> None:
    """Every wiki dropped from the answer is stated, never silently absent."""
    registry = load_registry(vault)
    for name, keywords in (
        ("ZedAlpha", ["zeppelin", "hangar"]),
        ("ZedBeta", ["zeppelin", "hangar"]),
        ("ZedGamma", ["zeppelin"]),
    ):
        (vault / name).mkdir()
        registry.wikis.append(
            WikiEntry(
                name=name,
                path=name,
                privacy="public-reference",
                keywords=keywords,
                sensitivity="public-reference",
            )
        )
    save_registry(vault, registry)

    result = run_preflight([_ref(vault)], "zeppelin hangar", "local")
    assert result.status == "ambiguous"
    named = [str(offer["name"]) for offer in result.offers]
    assert named == ["ZedAlpha", "ZedBeta"]
    assert any("outside the ambiguity band" in note and "ZedGamma" in note for note in result.notes)


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
    assert set(evidence) == {
        "routing_class",
        "coverage",
        "signal_classes",
        "signal_counts",
        "provenance",
        "lexical_classes",
        "semantic",
    }
    assert evidence["routing_class"] == "lexical-card"
    assert evidence["lexical_classes"] == ["trigger", "scope"]
    assert set(evidence["lexical_classes"]) <= {"trigger", "name", "scope"}
    assert "lexical" not in evidence
    assert evidence["coverage"] == {"matched_terms": 1, "request_terms": 2, "ratio": 0.5}
    assert evidence["provenance"] == {
        "source": "registry-card",
        "scope": "declared card metadata only",
        "page_content": False,
    }
    evidence_json = json.dumps(evidence)
    assert all(token not in evidence_json for token in ("how", "does", "pricing", "work"))
    assert str(vault) not in evidence_json
    assert "topics/" not in evidence_json
    assert evidence["semantic"] is None  # semantic disabled: no fabricated score
    # `reasons` keeps its v2 semantics: literal lexical reason strings
    assert match["reasons"] == ["trigger match: pricing", "scope match: pricing"]
    # the packet stays card-level: no page content path is ever handed out
    assert "topics/" not in json.dumps(match)


def test_authorized_matches_preserve_card_context_budgets(vault: Path) -> None:
    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    research = registry.wiki_by_name("ResearchDigest")
    assert product is not None and research is not None
    product.context_budget = ContextBudget(max_candidates=2, max_context_chars=2345)
    research.context_budget = ContextBudget(max_candidates=1, max_context_chars=987)
    save_registry(vault, registry)

    for model_class in ("local", "cloud"):
        full = run_preflight([_ref(vault)], "pricing", model_class)
        assert full.status == "matched"
        assert full.matches[0]["access"] == "full"
        assert full.matches[0]["context_budget"] == {
            "max_candidates": 2,
            "max_context_chars": 2345,
        }

    for model_class in ("local", "cloud"):
        digest = run_preflight([_ref(vault)], "research interview", model_class)
        assert digest.status == "matched"
        assert digest.matches[0]["access"] == "digest-only"
        assert digest.matches[0]["context_budget"] == {
            "max_candidates": 1,
            "max_context_chars": 987,
        }

    offer = run_preflight([_ref(vault)], "knowledge base", "local")
    assert offer.status == "ambiguous"
    assert all("context_budget" not in item for item in offer.offers)

    filtered = run_preflight([_ref(vault)], "brand color palette", "cloud")
    assert filtered.status == "privacy-filtered"
    assert all(
        "context_budget" not in item and "evidence" not in item for item in filtered.filtered
    )

    no_match = run_preflight([_ref(vault)], "quantum llama", "local")
    assert no_match.status == "no-match"
    assert no_match.matches == [] and no_match.offers == []

    broken = run_preflight([RootRef(label="missing", path=vault / "missing")], "pricing", "local")
    assert broken.status == "unavailable"
    assert broken.matches == [] and broken.offers == []
    assert all("context_budget" not in item for item in broken.root_issues)


def test_matches_without_a_load_path_never_carry_a_budget(vault: Path) -> None:
    """A budget bounds a load path, so an entry with no path never ships one."""
    registry = load_registry(vault)
    archive = registry.wiki_by_name("ArchiveBox")
    research = registry.wiki_by_name("ResearchDigest")
    assert archive is not None and research is not None
    archive.context_budget = ContextBudget(max_candidates=4, max_context_chars=4321)
    research.context_budget = ContextBudget(max_candidates=3, max_context_chars=3210)
    research.digest = ""  # digest-only access with no approved digest to load
    save_registry(vault, registry)

    pointer = run_preflight([_ref(vault)], "archive history", "local")
    assert pointer.status == "matched"
    assert pointer.matches[0]["name"] == "ArchiveBox"
    assert pointer.matches[0]["access"] == "none"
    assert pointer.matches[0]["allows"] == []
    assert "context_budget" not in pointer.matches[0]

    digest = run_preflight([_ref(vault)], "research interview", "local")
    assert digest.status == "matched"
    assert digest.matches[0]["name"] == "ResearchDigest"
    assert digest.matches[0]["allows"] == []
    assert "context_budget" not in digest.matches[0]


def test_canonical_card_roots_report_card_provenance_and_budget(tmp_path: Path) -> None:
    """A wiki-card root is named as such, and its own budget is the one surfaced."""
    estate = tmp_path / "estate"
    estate.mkdir()
    init_wiki_root(estate / "SoloWiki", "SoloWiki")
    card = load_wiki_card(estate / "SoloWiki")
    card.purpose = "Answers synthetic cider press questions."
    card.triggers = ["cider"]
    card.sensitivity = "public-reference"
    card.context_budget = ContextBudget(max_candidates=5, max_context_chars=5555)
    save_wiki_card(estate / "SoloWiki", card)

    result = run_preflight(discover_roots(estate), "cider press care", "local")
    assert result.status == "matched"
    match = result.matches[0]
    assert match["access"] == "full"
    evidence = match["evidence"]
    assert isinstance(evidence, dict)
    assert evidence["provenance"] == {
        "source": "canonical-card",  # not the registry projection
        "scope": "declared card metadata only",
        "page_content": False,
    }
    assert match["context_budget"] == {"max_candidates": 5, "max_context_chars": 5555}
    assert "cider" not in json.dumps(evidence)


def test_budget_and_evidence_repeat_byte_stable(vault: Path) -> None:
    """The additive packet is a deterministic function of the pinned proof inputs."""
    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    assert product is not None
    product.context_budget = ContextBudget(max_candidates=2, max_context_chars=2345)
    save_registry(vault, registry)

    for model_class in ("local", "cloud"):
        first = run_preflight([_ref(vault)], "how does pricing work", model_class)
        second = run_preflight([_ref(vault)], "how does pricing work", model_class)
        assert json.dumps(first.matches, sort_keys=True) == json.dumps(
            second.matches, sort_keys=True
        )
        assert json.dumps(first.offers, sort_keys=True) == json.dumps(second.offers, sort_keys=True)
        assert first.preflight_id == second.preflight_id
        assert first.matches[0]["evidence"] == second.matches[0]["evidence"]
        assert first.matches[0]["context_budget"] == {
            "max_candidates": 2,
            "max_context_chars": 2345,
        }


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
