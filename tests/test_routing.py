from __future__ import annotations

from pathlib import Path

from megamind.registry import load_registry
from megamind.routing import route, tokenize


def test_tokenize_filters_stopwords_and_singularizes() -> None:
    assert tokenize("What is the pricing for features?") == ["pricing", "feature"]
    assert tokenize("") == []
    assert tokenize("the is a of") == []


def test_route_finds_exact_page(vault: Path) -> None:
    registry = load_registry(vault)
    result = route(vault, registry, "what is our pricing model?")
    assert result.matched
    best = result.candidates[0]
    assert best.path == "ProductWiki/topics/pricing-model.md"
    assert best.kind == "page"
    assert best.wiki == "ProductWiki"
    assert any("keyword match: pricing" in reason for reason in best.reasons)


def test_route_is_deterministic(vault: Path) -> None:
    registry = load_registry(vault)
    first = route(vault, registry, "brand color palette")
    second = route(vault, registry, "brand color palette")
    assert first.to_dict() == second.to_dict()


def test_route_no_match_is_explicit_and_quiet(vault: Path) -> None:
    registry = load_registry(vault)
    result = route(vault, registry, "quantum blockchain llama farming")
    assert not result.matched
    assert result.candidates == []
    assert "no wiki matched this query" in result.notes


def test_route_empty_query(vault: Path) -> None:
    registry = load_registry(vault)
    result = route(vault, registry, "the of and")
    assert not result.matched
    assert "no usable terms in query" in result.notes


def test_digest_only_wiki_returns_digest_not_pages(vault: Path) -> None:
    registry = load_registry(vault)
    result = route(vault, registry, "research interview findings")
    assert result.matched
    best = result.candidates[0]
    assert best.wiki == "ResearchDigest"
    assert best.kind == "digest"
    assert best.path == "ResearchDigest/DIGEST.md"


def test_pointer_only_wiki_returns_pointer(vault: Path) -> None:
    registry = load_registry(vault)
    result = route(vault, registry, "archive history")
    assert result.matched
    best = result.candidates[0]
    assert best.wiki == "ArchiveBox"
    assert best.kind == "pointer"
    assert best.chars == 0
    assert any("pointer-only" in reason for reason in best.reasons)


def test_candidate_budget_enforced(vault: Path) -> None:
    registry = load_registry(vault)
    registry.budgets.max_candidates = 1
    result = route(vault, registry, "product pricing feature release")
    assert len(result.candidates) == 1


def test_context_char_budget_enforced(vault: Path) -> None:
    registry = load_registry(vault)
    registry.budgets.max_context_chars = 300  # smaller than two pages together
    result = route(vault, registry, "product pricing feature release")
    assert result.matched
    assert (
        result.context_chars <= 300 or len([c for c in result.candidates if c.kind == "page"]) == 1
    )
    assert any("context budget" in note for note in result.notes)


def test_route_reports_reasons_for_every_candidate(vault: Path) -> None:
    registry = load_registry(vault)
    result = route(vault, registry, "brand voice tone")
    assert result.matched
    for candidate in result.candidates:
        assert candidate.reasons


def test_context_chars_counts_characters_not_bytes(vault: Path) -> None:
    page = vault / "ProductWiki/topics/pricing-model.md"
    page.write_text(page.read_text(encoding="utf-8") + "\nPrix: 12 € par mois.\n", "utf-8")
    registry = load_registry(vault)
    result = route(vault, registry, "pricing model")
    expected = sum(
        len((vault / c.path).read_text(encoding="utf-8"))
        for c in result.candidates
        if c.kind != "pointer"
    )
    assert result.context_chars == expected


def test_digest_artifacts_respect_the_context_budget(vault: Path) -> None:
    registry = load_registry(vault)
    registry.budgets.max_context_chars = 1
    result = route(vault, registry, "product pricing brand voice")
    assert result.matched
    assert len(result.candidates) == 1  # the top candidate is always returned
    assert any("context budget reached" in note for note in result.notes)


def test_unreadable_page_still_routes_as_a_candidate(vault: Path) -> None:
    (vault / "ProductWiki/topics/pricing-model.md").write_bytes(b"\xff\xfe not utf-8")
    registry = load_registry(vault)
    result = route(vault, registry, "pricing model")
    assert result.matched
    broken = [c for c in result.candidates if c.path.endswith("pricing-model.md")]
    assert broken and broken[0].chars == 0


def test_unreadable_digest_degrades_instead_of_raising(vault: Path) -> None:
    (vault / "ProductWiki/DIGEST.md").write_bytes(b"\xff\xfe not utf-8")
    registry = load_registry(vault)
    assert route(vault, registry, "pricing model").matched
