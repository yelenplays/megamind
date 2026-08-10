"""Local semantic reranking: reorder-only, typed fallbacks, never widens access."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from megamind.catalog import RootRef
from megamind.preflight import run_preflight
from megamind.registry import WikiEntry, load_registry, save_registry
from megamind.routing import route
from megamind.semantic import (
    NgramBackend,
    ngram_similarity,
    rerank,
)


def _ref(root: Path) -> RootRef:
    return RootRef(label=str(root), path=root)


class UnavailableBackend:
    name = "fake-unavailable"

    def unavailable_reason(self) -> str | None:
        return "model file missing"

    def similarity(self, query: str, text: str) -> float:
        raise AssertionError("an unavailable backend is never queried")


class CorruptBackend:
    name = "fake-corrupt"

    def unavailable_reason(self) -> str | None:
        return None

    def similarity(self, query: str, text: str) -> float:
        raise RuntimeError("index file is corrupt")


class ExplodingAvailabilityBackend:
    name = "fake-exploding"

    def unavailable_reason(self) -> str | None:
        raise RuntimeError("backend metadata unreadable")

    def similarity(self, query: str, text: str) -> float:
        raise AssertionError("never reached")


def test_ngram_similarity_is_deterministic_and_bounded() -> None:
    assert ngram_similarity("pricing model", "pricing model") == 1.0
    assert ngram_similarity("", "pricing") == 0.0
    assert ngram_similarity("pricing", "") == 0.0
    assert ngram_similarity("zeppelin", "quantum") == 0.0
    similar = ngram_similarity("release train schedule", "weekly release schedule")
    assert 0.0 < similar < 1.0
    assert ngram_similarity("a b c", "a b c") == ngram_similarity("a b c", "a b c")


def test_rerank_disabled_by_default_returns_identity() -> None:
    order, outcome = rerank("q", ["a", "b"], [2.0, 1.0], ["ta", "tb"], None)
    assert order == [0, 1]
    assert outcome.status == "disabled"
    assert outcome.to_dict()["reason"]


def test_rerank_reorders_by_blend_and_stays_stable() -> None:
    # b is lexically weaker but semantically identical to the query.
    order, outcome = rerank(
        "weekly release train",
        ["a", "b"],
        [3.0, 2.0],
        ["unrelated content entirely", "weekly release train"],
        NgramBackend(),
    )
    assert outcome.status == "ok"
    assert outcome.scores["b"] == 1.0
    assert order == [1, 0]
    # identical inputs rerank identically
    again, _ = rerank(
        "weekly release train",
        ["a", "b"],
        [3.0, 2.0],
        ["unrelated content entirely", "weekly release train"],
        NgramBackend(),
    )
    assert again == order


def test_rerank_never_raises_for_bad_backends() -> None:
    for backend in (UnavailableBackend(), CorruptBackend(), ExplodingAvailabilityBackend()):
        order, outcome = rerank("q", ["a"], [1.0], ["text"], backend)
        assert order == [0]  # lexical order preserved
        assert outcome.status in ("unavailable", "error")
        assert outcome.backend == backend.name
        assert outcome.reason


def test_route_semantic_is_opt_in_and_typed(vault: Path) -> None:
    registry = load_registry(vault)
    plain = route(vault, registry, "pricing model")
    assert plain.semantic["status"] == "disabled"
    assert all(candidate.semantic_score is None for candidate in plain.candidates)

    semantic = route(vault, registry, "pricing model", semantic=NgramBackend())
    assert semantic.semantic["status"] == "ok"
    assert semantic.semantic["backend"] == "char-ngram"
    assert all(candidate.semantic_score is not None for candidate in semantic.candidates)
    # thresholds and membership are the lexical baseline's, rerank only reorders
    assert semantic.decision == plain.decision
    assert semantic.confidence == plain.confidence
    assert {c.path for c in semantic.candidates} == {c.path for c in plain.candidates}


def test_route_semantic_failure_keeps_lexical_result(vault: Path) -> None:
    registry = load_registry(vault)
    plain = route(vault, registry, "pricing model")
    broken = route(vault, registry, "pricing model", semantic=CorruptBackend())
    assert broken.semantic["status"] == "error"
    assert [c.path for c in broken.candidates] == [c.path for c in plain.candidates]
    assert broken.decision == plain.decision


def test_route_semantic_never_reads_pointer_content(vault: Path) -> None:
    registry = load_registry(vault)
    result = route(vault, registry, "archive history", semantic=NgramBackend())
    pointer = [c for c in result.candidates if c.kind == "pointer"]
    assert pointer and pointer[0].semantic_score == 0.0


def test_preflight_semantic_reranks_only_authorized_matches(vault: Path) -> None:
    registry = load_registry(vault)
    for name, keywords, purpose in (
        ("AlphaWiki", ["zeppelin"], "Heavy machinery storage notes."),
        (
            "BetaWiki",
            ["hangar"],
            "Zeppelin airship maintenance procedures and weekly hangar schedules.",
        ),
    ):
        (vault / name).mkdir()
        registry.wikis.append(
            WikiEntry(
                name=name,
                path=name,
                privacy="public-reference",
                keywords=keywords,
                purpose=purpose,
                sensitivity="public-reference",
            )
        )
    save_registry(vault, registry)

    plain = run_preflight([_ref(vault)], "zeppelin airship maintenance", "local")
    boosted = run_preflight(
        [_ref(vault)], "zeppelin airship maintenance", "local", semantic=NgramBackend()
    )
    assert plain.semantic["status"] == "disabled"
    assert boosted.semantic["status"] == "ok"
    plain_names = [str(entry["name"]) for entry in plain.matches + plain.offers]
    boosted_names = [str(entry["name"]) for entry in boosted.matches + boosted.offers]
    assert set(plain_names) == set(boosted_names)  # membership never changes
    assert boosted_names[0] == "BetaWiki"  # the semantically closer card leads
    assert plain_names[0] == "AlphaWiki"  # lexical order is name-tied, Alpha first


def test_preflight_semantic_cannot_resurrect_a_filtered_wiki(vault: Path) -> None:
    """A cloud-none wiki whose card is a near-perfect textual match still stays filtered."""
    registry = load_registry(vault)
    brand = registry.wiki_by_name("BrandingWiki")
    assert brand is not None
    brand.purpose = "brand color palette guidance for every brand color palette question"
    save_registry(vault, registry)

    result = run_preflight([_ref(vault)], "brand color palette", "cloud", semantic=NgramBackend())
    assert result.status == "privacy-filtered"
    assert result.matches == []
    assert [str(entry["name"]) for entry in result.filtered] == ["BrandingWiki"]
    serialized = json.dumps(result.matches) + json.dumps(result.offers)
    assert "BrandingWiki" not in serialized
    assert "BrandingWiki/" not in serialized


def test_semantic_layer_makes_no_network_calls(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    registry = load_registry(vault)
    route(vault, registry, "pricing model", semantic=NgramBackend())
