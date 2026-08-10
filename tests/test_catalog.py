"""Fleet catalog: aggregation, redaction, determinism, projection drift."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from conftest import build_vault
from megamind.card import CARD_PATH
from megamind.catalog import (
    RootRef,
    build_catalog,
    check_projection,
    discover_roots,
    render_projection,
    visible_rows,
)
from megamind.registry import WikiEntry, load_registry, save_registry
from megamind.scaffold import init_wiki_root


def _build_estate(base: Path) -> Path:
    """A synthetic estate: one registry vault, one canonical root, one broken root."""
    estate = base / "estate"
    estate.mkdir(parents=True)
    build_vault(estate)
    init_wiki_root(estate / "SoloWiki", "SoloWiki")

    broken = estate / "BrokenWiki"
    (broken / ".megamind").mkdir(parents=True)
    (broken / ".megamind" / "registry.json").write_text("{not json", encoding="utf-8")
    return estate


def _rows(estate: Path, today: date | None = None) -> list[dict[str, object]]:
    catalog = build_catalog(discover_roots(estate), today=today)
    return visible_rows(catalog)


def test_discovers_registry_vaults_and_canonical_roots(tmp_path: Path) -> None:
    estate = _build_estate(tmp_path)
    refs = discover_roots(estate)
    names = {ref.path.name for ref in refs}
    assert names == {"vault", "SoloWiki", "BrokenWiki"}


def test_catalog_aggregates_every_wiki_with_card_detail(tmp_path: Path) -> None:
    estate = _build_estate(tmp_path)
    rows = _rows(estate, today=date(2026, 8, 10))
    by_name = {str(row.get("name")): row for row in rows if row.get("status") == "ok"}
    assert {
        "StarterWiki",
        "ProductWiki",
        "BrandingWiki",
        "ResearchDigest",
        "ArchiveBox",
        "SoloWiki",
    } <= set(by_name)
    product = by_name["ProductWiki"]
    assert product["root"] == f"{estate}/vault"
    assert product["model_access"] == {"local": "full", "cloud": "full", "derived": []}
    assert product["maintenance"]["doctor"] == "healthy"
    solo = by_name["SoloWiki"]
    assert solo["source"] == "wiki-card"
    assert solo["paths"]["index"] == "wiki/index.md"
    assert solo["model_access"]["cloud"] == "none"  # unclassified defaults restrictively


def test_broken_root_is_an_explicit_entry(tmp_path: Path) -> None:
    estate = _build_estate(tmp_path)
    rows = _rows(estate)
    broken = [row for row in rows if row.get("status") == "broken"]
    assert len(broken) == 1
    assert "BrokenWiki" in str(broken[0]["root"])
    assert "not valid JSON" in str(broken[0]["error"])


def test_unreachable_root_is_an_explicit_entry(tmp_path: Path) -> None:
    refs = [RootRef(label="ghost", path=tmp_path / "ghost")]
    catalog = build_catalog(refs)
    assert catalog.rows[0]["status"] == "unreachable"


def test_hidden_wiki_is_withheld_without_name_or_root(tmp_path: Path) -> None:
    estate = _build_estate(tmp_path)
    vault = estate / "vault"
    registry = load_registry(vault)
    registry.wikis.append(
        WikiEntry(
            name="SecretWiki",
            path="SecretWiki",
            privacy="personal-local",
            keywords=["secret"],
            catalog_visibility="hidden",
        )
    )
    save_registry(vault, registry)
    rows = _rows(estate)
    withheld = [row for row in rows if row.get("status") == "redacted"]
    assert len(withheld) == 1
    assert "SecretWiki" not in json.dumps(withheld)
    assert str(vault) not in json.dumps(withheld)
    assert withheld[0]["catalog_visibility"] == "hidden"


def test_redacted_wiki_keeps_only_safe_fields(tmp_path: Path) -> None:
    estate = _build_estate(tmp_path)
    vault = estate / "vault"
    registry = load_registry(vault)
    registry.wikis.append(
        WikiEntry(
            name="DiaryWiki",
            path="DiaryWiki",
            privacy="personal-local",
            purpose="Synthetic personal notes.",
            keywords=["diary"],
            owners=["person@example.invalid"],
            triggers=["diary"],
        )
    )
    save_registry(vault, registry)
    rows = _rows(estate)
    diary = next(row for row in rows if row.get("name") == "DiaryWiki")
    assert diary["redacted"] is True
    assert diary["purpose"] == "Synthetic personal notes."
    assert "owners" in diary["redacted_fields"]
    assert "triggers" in diary["redacted_fields"]
    assert "owners" not in diary


def test_catalog_is_byte_stable(tmp_path: Path) -> None:
    estate = _build_estate(tmp_path)
    first = build_catalog(discover_roots(estate), today=date(2026, 8, 10))
    second = build_catalog(discover_roots(estate), today=date(2026, 8, 10))
    assert first.rows == second.rows
    assert first.catalog_hash == second.catalog_hash
    assert render_projection(first) == render_projection(second)


def test_projection_drift_check(tmp_path: Path) -> None:
    estate = _build_estate(tmp_path)
    catalog = build_catalog(discover_roots(estate))
    projection = tmp_path / "CATALOG.md"
    assert check_projection(catalog, projection) == "missing"
    projection.write_text(render_projection(catalog), encoding="utf-8")
    assert check_projection(build_catalog(discover_roots(estate)), projection) == "current"

    # A card change must make the checked-in projection drift.
    vault = estate / "vault"
    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    assert product is not None
    product.purpose = "Updated synthetic purpose."
    save_registry(vault, registry)
    drifted = build_catalog(discover_roots(estate))
    assert check_projection(drifted, projection) == "drifted"


def test_catalog_never_reads_page_content(tmp_path: Path) -> None:
    estate = _build_estate(tmp_path)
    rows = _rows(estate, today=date(2026, 8, 10))
    blob = json.dumps(rows)
    page_text = (estate / "vault" / "ProductWiki/topics/pricing-model.md").read_text(
        encoding="utf-8"
    )
    distinctive = "flat monthly plan"
    assert distinctive in page_text
    assert distinctive not in blob
    assert distinctive not in render_projection(build_catalog(discover_roots(estate)))


def test_staleness_is_computed_only_with_a_date(tmp_path: Path) -> None:
    estate = _build_estate(tmp_path)
    vault = estate / "vault"
    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    assert product is not None
    product.freshness.half_life_days = 30
    product.freshness.last_confirmed = "2026-01-01"
    save_registry(vault, registry)

    fresh_catalog = build_catalog(discover_roots(estate))
    row = next(r for r in fresh_catalog.rows if r.get("name") == "ProductWiki")
    assert row["freshness"]["stale"] is None  # type: ignore[index]
    assert "stale" not in row

    dated = build_catalog(discover_roots(estate), today=date(2026, 8, 10))
    row = next(r for r in dated.rows if r.get("name") == "ProductWiki")
    assert row["freshness"]["stale"] is True  # type: ignore[index]
    assert row["stale"] is True


def test_malformed_card_root_is_broken_not_fatal(tmp_path: Path) -> None:
    estate = _build_estate(tmp_path)
    card = estate / "SoloWiki" / CARD_PATH
    card.write_text(json.dumps({"schema": "megamind/wiki-card/v2", "version": 2, "name": 7}))
    rows = _rows(estate)
    broken = [row for row in rows if row.get("status") == "broken"]
    assert any("SoloWiki" in str(row.get("root")) for row in broken)
    assert any(row.get("name") == "ProductWiki" for row in rows)
