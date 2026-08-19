from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from megamind.registry import Registry, WikiEntry, load_registry
from megamind.routing import route, tokenize


def test_tokenize_filters_stopwords_and_singularizes() -> None:
    assert tokenize("What is the pricing for features?") == ["pricing", "feature"]
    assert tokenize("") == []
    assert tokenize("the is a of") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pros", ["pros"]),
        ("pro", ["pro"]),
        ("logos", ["logo"]),
        ("names", ["name"]),
    ],
)
def test_tokenize_conservatively_handles_ambiguous_final_s(text: str, expected: list[str]) -> None:
    assert tokenize(text) == expected


def test_tokenize_folds_german_umlauts_to_transliterations() -> None:
    """An umlaut word must stay one token and meet its ae/oe/ue/ss spelling,
    so a card written either way matches a query typed either way."""
    assert tokenize("Vermögensaufteilung") == ["vermoegensaufteilung"]
    assert tokenize("Vermoegensaufteilung") == ["vermoegensaufteilung"]
    assert tokenize("Straße") == ["strasse"]
    assert tokenize("Ernährung") == ["ernaehrung"]


def test_tokenize_folds_decomposed_unicode_umlauts() -> None:
    """macOS and web inputs can deliver NFD; a combining diaeresis must fold
    identically to the precomposed character."""
    decomposed = unicodedata.normalize("NFD", "Blätter")
    assert len(decomposed) == 8  # the diaeresis really is its own codepoint
    assert tokenize(decomposed) == ["blaetter"]


def test_tokenize_filters_german_stopwords() -> None:
    assert tokenize("welche Bilder passen für den Beitrag") == ["bilder", "passen", "beitrag"]
    assert tokenize("und oder nicht mit von der die das") == []


def test_tokenize_filters_german_request_scaffolding() -> None:
    """Verbs and adjectives that only phrase a request ("wie finde ich einen
    guten...") are noise for coverage, exactly like English how/what/which."""
    assert tokenize("wie finde ich einen guten Markennamen") == ["markennamen"]
    assert tokenize("Titelbild für das neue Video erstellen") == ["titelbild", "video"]


def _with_picture_trigger(vault: Path) -> Registry:
    registry = load_registry(vault)
    registry.wikis.append(
        WikiEntry(
            name="PictureWiki",
            path="PictureWiki",
            privacy="pointer-only",
            keywords=["pro"],
        )
    )
    return registry


def test_pros_does_not_match_a_pro_trigger(vault: Path) -> None:
    result = route(vault, _with_picture_trigger(vault), "pros")
    assert not result.matched
    assert result.candidates == []


def test_explicit_pro_still_matches_exactly(vault: Path) -> None:
    result = route(vault, _with_picture_trigger(vault), "pro")
    assert result.matched
    assert [(candidate.wiki, candidate.path) for candidate in result.candidates] == [
        ("PictureWiki", "PictureWiki")
    ]


def test_incident_prompt_does_not_match_a_pro_trigger(vault: Path) -> None:
    prompt = "Once we are pros in Bochum, how should we proceed?"
    result = route(vault, _with_picture_trigger(vault), prompt)
    assert not result.matched
    assert result.candidates == []


def test_explicit_branding_wiki_naming_prompt_still_routes(vault: Path) -> None:
    result = route(vault, _with_picture_trigger(vault), "BrandingWiki brand name")
    assert result.matched
    assert result.candidates[0].wiki == "BrandingWiki"


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


def test_index_candidates_use_canonical_root_relative_paths(vault: Path) -> None:
    """Index links that climb out of the index dir resolve to canonical paths."""
    from megamind.registry import WikiEntry, save_registry

    (vault / "_meta/routing/deep").mkdir(parents=True)
    (vault / "_meta/routing/deep/INDEX.md").write_text(
        "---\nmegamind: index\nwiki: DeepWiki\n---\n\n# DeepWiki index\n\n"
        "- [Release process](../../../ProductWiki/topics/release-process.md) - weekly train\n",
        encoding="utf-8",
    )
    registry = load_registry(vault)
    registry.wikis.append(
        WikiEntry(
            name="DeepWiki",
            path="_meta",
            privacy="public-reference",
            description="Synthetic wiki whose index lives in a nested sidecar.",
            keywords=["deep", "release"],
            index="_meta/routing/deep/INDEX.md",
        )
    )
    save_registry(vault, registry)

    registry = load_registry(vault)
    result = route(vault, registry, "release process")
    paths = [c.path for c in result.candidates]
    assert "ProductWiki/topics/release-process.md" in paths
    assert all(".." not in path.split("/") for path in paths)
