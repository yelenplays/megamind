from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from conftest import write
from megamind.capture import capture
from megamind.registry import load_registry
from megamind.review import review

TODAY = date(2026, 3, 3)


def test_clean_vault_reports_nothing(vault: Path) -> None:
    registry = load_registry(vault)
    report = review(vault, registry, today=TODAY)
    assert report.is_clean()


def test_uncategorized_and_open_proposals(vault: Path) -> None:
    registry = load_registry(vault)
    capture(vault, registry, "Zeppelin maintenance schedule.", source="note", today=TODAY)
    capture(vault, registry, "Pricing gains a student discount.", source="note", today=TODAY)
    report = review(vault, registry, today=TODAY)
    assert len(report.open_proposals) == 2
    assert len(report.uncategorized_proposals) == 1


def test_stale_pages_flagged_by_age_and_status(vault: Path) -> None:
    registry = load_registry(vault)
    write(
        vault,
        "ProductWiki/topics/old-idea.md",
        "---\ntitle: Old idea\ntype: hypothesis\nstatus: shaky\n"
        "created: 2024-01-01\nupdated: 2024-01-01\n---\n\n# Old idea\n\nUnverified.\n",
    )
    report = review(vault, registry, today=TODAY)
    reasons = {entry["page"]: entry["reason"] for entry in report.stale_pages}
    assert "ProductWiki/topics/old-idea.md" in reasons
    assert "shaky" in str(reasons["ProductWiki/topics/old-idea.md"])


def test_dead_links_reported(vault: Path) -> None:
    registry = load_registry(vault)
    write(
        vault,
        "ProductWiki/topics/broken.md",
        "---\ntitle: Broken\ntype: fact\nstatus: active\nprovenance:\n  - synthetic\n---\n\n"
        "# Broken\n\nSee [missing](missing-page.md) and [[GhostPage]].\n",
    )
    report = review(vault, registry, today=TODAY)
    targets = {entry["target"] for entry in report.dead_links}
    assert "missing-page.md" in targets
    assert "GhostPage" in targets


def test_superseded_still_linked_reported(vault: Path) -> None:
    registry = load_registry(vault)
    write(
        vault,
        "ProductWiki/topics/pricing-v2.md",
        "---\ntitle: Pricing v2\ntype: decision\nstatus: active\nprovenance:\n  - synthetic\n"
        "---\n\n# Pricing v2\n\nTwo tiers.\n",
    )
    old = vault / "ProductWiki/topics/pricing-model.md"
    text = old.read_text(encoding="utf-8").replace(
        "status: active", "status: superseded\nsuperseded_by: ProductWiki/topics/pricing-v2.md"
    )
    old.write_text(text, encoding="utf-8")
    report = review(vault, registry, today=TODAY)
    assert any(
        entry["superseded"] == "ProductWiki/topics/pricing-model.md"
        for entry in report.superseded_still_linked
    )


def test_promotion_candidates(vault: Path) -> None:
    registry = load_registry(vault)
    for index in range(4):
        write(
            vault,
            f"ProductWiki/topics/integrations/tool-{index}.md",
            f"---\ntitle: Tool {index}\ntype: example\nstatus: active\n"
            f"provenance:\n  - synthetic\n---\n\n# Tool {index}\n\nSynthetic.\n",
        )
    report = review(vault, registry, today=TODAY)
    candidates = {entry["path"]: entry["kind"] for entry in report.promotion_candidates}
    assert candidates.get("ProductWiki/topics/integrations") == "micro-wiki"


def test_duplicate_titles_reported(vault: Path) -> None:
    registry = load_registry(vault)
    write(
        vault,
        "BrandingWiki/topics/pricing-model.md",
        "---\ntitle: Pricing model\ntype: guidance\nstatus: active\n"
        "provenance:\n  - synthetic\n---\n\n# Pricing model\n\nDuplicate title.\n",
    )
    report = review(vault, registry, today=TODAY)
    assert any(entry["title"] == "pricing model" for entry in report.duplicate_titles)


def test_review_is_read_only(vault: Path) -> None:
    registry = load_registry(vault)
    before = sorted(str(p) for p in vault.rglob("*"))
    review(vault, registry, today=TODAY)
    assert sorted(str(p) for p in vault.rglob("*")) == before


def test_research_artifacts_reported_from_an_unresolved_root(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented default invocation runs inside the vault, so root is `.`."""
    registry = load_registry(vault)
    write(
        vault,
        ".megamind/research/packets/abc123abc123.json",
        '{"schema": "megamind/research-packet/v1", "packet_id": "abc123abc123"}\n',
    )
    write(
        vault,
        ".megamind/research/evidence/e1.json",
        '{"decision": "deferred", "correction_status": "expression_of_concern"}\n',
    )
    monkeypatch.chdir(vault)
    report = review(Path("."), registry, today=TODAY)
    assert report.research_packets == [".megamind/research/packets/abc123abc123.json"]
    assert report.pending_source_rights == [".megamind/research/evidence/e1.json"]
    assert report.contradictions == [".megamind/research/evidence/e1.json"]
