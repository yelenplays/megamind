from __future__ import annotations

from pathlib import Path

import pytest

from megamind.links import (
    Link,
    encode_link_target,
    extract_links,
    link_target_path,
    page_name_table,
    resolve_link,
)


@pytest.mark.parametrize(
    "target",
    [
        "topics/pricing (2026).md",
        "topics/q3 plan.md",
        "topics/100% margin.md",
        "topics/notes#draft.md",
        "topics/große entscheidung.md",
        "../ProductWiki/topics/pricing-model.md",
    ],
)
def test_encoded_targets_round_trip_through_extraction(target: str) -> None:
    """A target Megamind writes must read back as exactly that path, or nothing routes."""
    links = extract_links(f"- [Label]({encode_link_target(target)})")
    assert [link_target_path(link.target) for link in links] == [target]


def test_encoding_leaves_ordinary_targets_untouched() -> None:
    assert encode_link_target("topics/pricing-model.md") == "topics/pricing-model.md"
    assert link_target_path("topics/pricing-model.md#tiers") == "topics/pricing-model.md"


def test_percent_encoded_link_resolves_to_the_page_it_names(vault: Path) -> None:
    """Obsidian writes `%20` for a space, so that link names a real page, not a dead one."""
    page = vault / "ProductWiki/topics/pricing model (v2).md"
    page.write_text("# Pricing model v2\n", encoding="utf-8")
    link = Link(target="topics/pricing%20model%20%28v2%29.md", label="v2", style="markdown")
    names = page_name_table(vault)
    assert resolve_link(vault, vault / "ProductWiki/INDEX.md", link, names)
