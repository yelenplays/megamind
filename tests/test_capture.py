from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from megamind.capture import CaptureError, capture, list_proposals
from megamind.models import parse_document
from megamind.registry import load_registry

TODAY = date(2026, 3, 1)


def test_capture_creates_proposal_with_provenance(vault: Path) -> None:
    registry = load_registry(vault)
    result = capture(
        vault,
        registry,
        "Pricing changes to two tiers next quarter.",
        source="conversation with team",
        knowledge_type="decision",
        today=TODAY,
    )
    assert result.created
    proposal_file = vault / result.path
    assert proposal_file.is_file()
    document = parse_document(proposal_file.read_text(encoding="utf-8"))
    assert document.frontmatter["megamind"] == "proposal"
    assert document.frontmatter["id"] == result.proposal_id
    assert document.frontmatter["status"] == "proposed"
    assert document.frontmatter["type"] == "decision"
    assert document.frontmatter["source"] == "conversation with team"
    assert document.frontmatter["captured"] == "2026-03-01"
    assert "two tiers" in document.body


def test_capture_suggests_destination_from_router(vault: Path) -> None:
    registry = load_registry(vault)
    result = capture(
        vault,
        registry,
        "The brand color palette gains a third accent color.",
        source="design review",
        today=TODAY,
    )
    assert result.suggested_destination == "BrandingWiki/topics/color-palette.md"


def test_capture_unmatched_content_is_uncategorized(vault: Path) -> None:
    registry = load_registry(vault)
    result = capture(
        vault, registry, "Zeppelin maintenance schedule draft.", source="note", today=TODAY
    )
    assert result.suggested_destination == "uncategorized"


def test_capture_is_idempotent(vault: Path) -> None:
    registry = load_registry(vault)
    first = capture(vault, registry, "Same note text.", source="a", today=TODAY)
    second = capture(vault, registry, "Same note text.\n", source="b", today=TODAY)
    assert first.created and not second.created
    assert first.proposal_id == second.proposal_id
    assert len(list_proposals(vault)) == 1


def test_capture_rejects_empty_and_unknown_type(vault: Path) -> None:
    registry = load_registry(vault)
    with pytest.raises(CaptureError, match="empty"):
        capture(vault, registry, "   \n  ", source="x", today=TODAY)
    with pytest.raises(CaptureError, match="knowledge type"):
        capture(vault, registry, "text", source="x", knowledge_type="rumor", today=TODAY)


def test_capture_never_touches_wiki_pages(vault: Path) -> None:
    registry = load_registry(vault)
    before = {
        p: p.read_text(encoding="utf-8") for p in vault.rglob("*.md") if ".megamind" not in p.parts
    }
    capture(vault, registry, "A new pricing idea to consider.", source="note", today=TODAY)
    after = {
        p: p.read_text(encoding="utf-8") for p in vault.rglob("*.md") if ".megamind" not in p.parts
    }
    assert before == after
