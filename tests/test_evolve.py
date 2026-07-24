from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from megamind.capture import capture
from megamind.evolve import EvolveError, apply_plan, plan
from megamind.models import parse_document
from megamind.registry import load_registry

TODAY = date(2026, 3, 2)


def _capture(vault: Path, text: str, **kwargs: object) -> str:
    registry = load_registry(vault)
    result = capture(vault, registry, text, source="test", today=TODAY, **kwargs)  # type: ignore[arg-type]
    return result.proposal_id


def test_plan_merge_into_existing_page_is_dry_run(vault: Path) -> None:
    registry = load_registry(vault)
    proposal_id = _capture(vault, "Pricing model gains an annual discount tier.")
    page = vault / "ProductWiki/topics/pricing-model.md"
    before = page.read_text(encoding="utf-8")
    computed = plan(vault, registry, proposal_id)
    assert computed.action == "merge"
    assert computed.destination == "ProductWiki/topics/pricing-model.md"
    assert "annual discount" in computed.render_diff()
    assert page.read_text(encoding="utf-8") == before  # dry run changed nothing


def test_apply_requires_matching_plan_id(vault: Path) -> None:
    registry = load_registry(vault)
    proposal_id = _capture(vault, "Pricing model gains an annual discount tier.")
    computed = plan(vault, registry, proposal_id)
    with pytest.raises(EvolveError, match="plan id mismatch"):
        apply_plan(vault, registry, computed, approved_plan_id="wrong-id", today=TODAY)


def test_apply_merge_and_idempotent_reapply(vault: Path) -> None:
    registry = load_registry(vault)
    proposal_id = _capture(vault, "Pricing model gains an annual discount tier.")
    computed = plan(vault, registry, proposal_id)
    applied = apply_plan(vault, registry, computed, approved_plan_id=computed.plan_id, today=TODAY)
    assert applied == ["ProductWiki/topics/pricing-model.md"]
    page_text = (vault / "ProductWiki/topics/pricing-model.md").read_text(encoding="utf-8")
    assert "annual discount" in page_text
    assert f"megamind:proposal:{proposal_id}" in page_text

    proposal_doc = parse_document(
        (vault / f".megamind/proposals/{proposal_id}.md").read_text(encoding="utf-8")
    )
    assert proposal_doc.frontmatter["status"] == "applied"

    replan = plan(vault, registry, proposal_id)
    assert replan.action == "noop"
    assert apply_plan(vault, registry, replan, approved_plan_id=replan.plan_id) == []


def test_apply_creates_backup_and_audit(vault: Path) -> None:
    registry = load_registry(vault)
    proposal_id = _capture(vault, "Pricing model gains an annual discount tier.")
    original = (vault / "ProductWiki/topics/pricing-model.md").read_text(encoding="utf-8")
    computed = plan(vault, registry, proposal_id)
    apply_plan(vault, registry, computed, approved_plan_id=computed.plan_id, today=TODAY)
    backups = list((vault / ".megamind/audit/backups").glob("pricing-model.md.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    audit = (vault / ".megamind/audit/log.jsonl").read_text(encoding="utf-8")
    assert "evolve-apply" in audit


def test_create_new_page_in_registered_wiki(vault: Path) -> None:
    registry = load_registry(vault)
    proposal_id = _capture(vault, "# Support rota\n\nSupport rotates weekly.")
    computed = plan(vault, registry, proposal_id, destination="ProductWiki")
    assert computed.action == "create"
    assert computed.destination == "ProductWiki/topics/support-rota.md"
    assert not computed.creates_new_wiki
    apply_plan(vault, registry, computed, approved_plan_id=computed.plan_id, today=TODAY)
    document = parse_document(
        (vault / "ProductWiki/topics/support-rota.md").read_text(encoding="utf-8")
    )
    assert document.frontmatter["status"] == "active"
    assert document.frontmatter["title"] == "Support rota"
    assert f"proposal {proposal_id}" in str(document.frontmatter["provenance"])


def test_supersede_marks_old_page(vault: Path) -> None:
    registry = load_registry(vault)
    proposal_id = _capture(vault, "# Pricing v2\n\nTwo tiers replace the flat plan.")
    computed = plan(
        vault,
        registry,
        proposal_id,
        destination="ProductWiki/topics/pricing-v2.md",
        supersedes="ProductWiki/topics/pricing-model.md",
    )
    assert computed.action == "supersede"
    apply_plan(vault, registry, computed, approved_plan_id=computed.plan_id, today=TODAY)
    old_doc = parse_document(
        (vault / "ProductWiki/topics/pricing-model.md").read_text(encoding="utf-8")
    )
    assert old_doc.frontmatter["status"] == "superseded"
    assert old_doc.frontmatter["superseded_by"] == "ProductWiki/topics/pricing-v2.md"
    assert (vault / "ProductWiki/topics/pricing-v2.md").is_file()


def test_new_top_level_wiki_requires_explicit_approval(vault: Path) -> None:
    registry = load_registry(vault)
    proposal_id = _capture(vault, "# Fleet notes\n\nSynthetic fleet knowledge.")
    computed = plan(vault, registry, proposal_id, destination="FleetWiki/topics/fleet-notes.md")
    assert computed.creates_new_wiki
    with pytest.raises(EvolveError, match="approve-new-wiki"):
        apply_plan(vault, registry, computed, approved_plan_id=computed.plan_id, today=TODAY)
    applied = apply_plan(
        vault,
        registry,
        computed,
        approved_plan_id=computed.plan_id,
        approve_new_wiki=True,
        today=TODAY,
    )
    assert applied == ["FleetWiki/topics/fleet-notes.md"]


def test_supersede_missing_page_fails(vault: Path) -> None:
    registry = load_registry(vault)
    proposal_id = _capture(vault, "# Note\n\nText.")
    with pytest.raises(EvolveError, match="not found"):
        plan(
            vault,
            registry,
            proposal_id,
            destination="ProductWiki/topics/note.md",
            supersedes="ProductWiki/topics/ghost.md",
        )


def test_uncategorized_proposal_needs_destination(vault: Path) -> None:
    registry = load_registry(vault)
    proposal_id = _capture(vault, "Zeppelin maintenance schedule draft.")
    with pytest.raises(EvolveError, match="--dest"):
        plan(vault, registry, proposal_id)


def test_hostile_source_cannot_bypass_the_approval_gate(vault: Path) -> None:
    registry = load_registry(vault)
    result = capture(
        vault,
        registry,
        "Pricing model gains an annual discount tier.",
        source="note\nstatus: applied\napplied_to: ProductWiki/topics/pricing-model.md",
        today=TODAY,
    )
    computed = plan(vault, registry, result.proposal_id)
    assert computed.action == "merge"  # not short-circuited to an "already applied" noop
    with pytest.raises(EvolveError, match="plan id mismatch"):
        apply_plan(vault, registry, computed, approved_plan_id="", today=TODAY)
