from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from megamind.capture import capture
from megamind.doctor import has_errors, run_doctor
from megamind.evolve import EvolveError, apply_plan, plan
from megamind.links import extract_links, link_target_path
from megamind.models import parse_document
from megamind.registry import load_registry
from megamind.review import review
from megamind.routing import route

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
    assert applied == [
        "FleetWiki/topics/fleet-notes.md",
        "FleetWiki/CARD.md",
        "FleetWiki/INDEX.md",
        ".megamind/registry.json",
        "ROUTER.md",
    ]


def test_approved_new_wiki_is_registered_and_visible(vault: Path) -> None:
    """An approved new top-level wiki lands registered, routable, and doctor-clean."""
    registry = load_registry(vault)
    proposal_id = _capture(vault, "# Fleet notes\n\nSynthetic fleet knowledge.")
    computed = plan(vault, registry, proposal_id, destination="FleetWiki/topics/fleet-notes.md")
    planned_paths = [change.path for change in computed.changes]
    assert planned_paths == [
        "FleetWiki/topics/fleet-notes.md",
        "FleetWiki/CARD.md",
        "FleetWiki/INDEX.md",
        ".megamind/registry.json",
        "ROUTER.md",
    ]
    apply_plan(
        vault,
        registry,
        computed,
        approved_plan_id=computed.plan_id,
        approve_new_wiki=True,
        today=TODAY,
    )

    updated = load_registry(vault)
    entry = updated.wiki_by_name("FleetWiki")
    assert entry is not None
    assert entry.path == "FleetWiki"
    assert entry.privacy == "company-private"  # restrictive default for a new wiki
    assert entry.card == "FleetWiki/CARD.md"
    assert entry.index == "FleetWiki/INDEX.md"
    assert (vault / "FleetWiki/CARD.md").is_file()
    assert (vault / "FleetWiki/INDEX.md").is_file()
    assert "FleetWiki" in (vault / "ROUTER.md").read_text(encoding="utf-8")

    routed = route(vault, updated, "fleet notes")
    assert routed.matched
    assert any(candidate.wiki == "FleetWiki" for candidate in routed.candidates)

    report = review(vault, updated, today=TODAY)
    assert all("FleetWiki" not in str(item) for item in report.promotion_candidates)

    findings = run_doctor(vault)
    assert not has_errors(findings), [f.message for f in findings if f.severity == "error"]
    assert not any(f.check == "registration" for f in findings)


def test_unregistered_wiki_shaped_directory_is_a_doctor_warning(vault: Path) -> None:
    """A wiki-shaped directory that was never registered must not stay invisible."""
    (vault / "GhostWiki/topics").mkdir(parents=True)
    (vault / "GhostWiki/topics/ghost.md").write_text("# Ghost\n", encoding="utf-8")
    findings = run_doctor(vault)
    matches = [f for f in findings if f.check == "registration" and f.path == "GhostWiki"]
    assert len(matches) == 1
    assert matches[0].severity == "warning"
    assert "not registered" in matches[0].message


def test_bracketed_heading_still_yields_an_extractable_index_link(vault: Path) -> None:
    """An index line only routes if `extract_links` can read the link back out."""
    registry = load_registry(vault)
    destination = "ProductWiki/topics/pricing-tiers.md"
    proposal_id = _capture(vault, "# Pricing tiers [Q3]\n\nThree synthetic tiers apply.")
    computed = plan(vault, registry, proposal_id, destination=destination)
    apply_plan(vault, registry, computed, approved_plan_id=computed.plan_id, today=TODAY)

    index = parse_document((vault / "ProductWiki/INDEX.md").read_text(encoding="utf-8"))
    links = extract_links(index.body)
    assert "topics/pricing-tiers.md" in [link.target for link in links]
    assert "Pricing tiers (Q3)" in [link.label for link in links]

    routed = route(vault, load_registry(vault), "pricing tiers")
    assert any(candidate.path == destination for candidate in routed.candidates)

    # The extractable link is also what stops a later proposal appending a duplicate.
    second = _capture(vault, "# Pricing tiers [Q3]\n\nA later synthetic revision.")
    replan = plan(vault, load_registry(vault), second, destination=destination)
    assert [change.path for change in replan.changes] == [destination]


def test_evolved_page_with_spaces_and_parentheses_routes_and_dedupes(vault: Path) -> None:
    """A filename Markdown cannot carry literally must still route back from the index."""
    registry = load_registry(vault)
    destination = "ProductWiki/topics/pricing (2026).md"
    proposal_id = _capture(vault, "# Pricing tiers\n\nThree synthetic tiers apply.")
    computed = plan(vault, registry, proposal_id, destination=destination)
    apply_plan(vault, registry, computed, approved_plan_id=computed.plan_id, today=TODAY)

    index = parse_document((vault / "ProductWiki/INDEX.md").read_text(encoding="utf-8"))
    assert "(topics/pricing%20%282026%29.md)" in index.body
    assert "topics/pricing (2026).md" in [
        link_target_path(link.target) for link in extract_links(index.body)
    ]

    routed = route(vault, load_registry(vault), "pricing tiers")
    assert any(candidate.path == destination for candidate in routed.candidates)

    second = _capture(vault, "# Pricing tiers\n\nA later synthetic revision.")
    replan = plan(vault, load_registry(vault), second, destination=destination)
    assert [change.path for change in replan.changes] == [destination]

    findings = run_doctor(vault)
    assert not has_errors(findings), [f.message for f in findings if f.severity == "error"]


def test_wiki_without_a_usable_index_says_the_page_is_not_index_routable(vault: Path) -> None:
    """A created page that route cannot reach must not be reported as if it could."""
    registry = load_registry(vault)
    undeclared = "ResearchDigest/topics/interview-cadence.md"
    proposal_id = _capture(vault, "# Interview cadence\n\nSynthetic research cadence.")
    computed = plan(vault, registry, proposal_id, destination=undeclared)
    assert [change.path for change in computed.changes] == [undeclared]
    assert any("ResearchDigest declares no index" in note for note in computed.notes)
    assert any("will not be index-routable" in note for note in computed.notes)

    (vault / "ProductWiki/INDEX.md").unlink()
    missing = _capture(vault, "# Support rota\n\nSupport rotates weekly.")
    orphaned = plan(vault, registry, missing, destination="ProductWiki")
    assert [change.path for change in orphaned.changes] == ["ProductWiki/topics/support-rota.md"]
    assert any("ProductWiki/INDEX.md is missing" in note for note in orphaned.notes)
    assert any("will not be index-routable" in note for note in orphaned.notes)


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


def test_bare_top_level_page_destination_is_refused_before_any_write(vault: Path) -> None:
    """A new wiki needs a directory; a loose `note.md` would be its own wiki path."""
    registry = load_registry(vault)
    proposal_id = _capture(vault, "# Loose note\n\nSynthetic loose knowledge.")
    before = {path.relative_to(vault) for path in vault.rglob("*")}
    with pytest.raises(EvolveError, match="--dest <WikiName>"):
        plan(vault, registry, proposal_id, destination="loose-note.md")
    assert {path.relative_to(vault) for path in vault.rglob("*")} == before
    assert not (vault / "loose-note.md").exists()

    nested = plan(vault, registry, proposal_id, destination="LooseWiki/loose-note.md")
    assert nested.creates_new_wiki
    assert [change.path for change in nested.changes] == [
        "LooseWiki/loose-note.md",
        "LooseWiki/CARD.md",
        "LooseWiki/INDEX.md",
        ".megamind/registry.json",
        "ROUTER.md",
    ]


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
