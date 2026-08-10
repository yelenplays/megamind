from __future__ import annotations

from pathlib import Path

from conftest import write
from megamind.doctor import has_errors, run_doctor
from megamind.registry import ModelAccess, load_registry, save_registry


def _errors(findings: list[object]) -> list[str]:
    return [f.message for f in findings if f.severity == "error"]  # type: ignore[attr-defined]


def test_healthy_vault_has_no_errors(vault: Path) -> None:
    findings = run_doctor(vault)
    assert not has_errors(findings), _errors(findings)


def test_missing_registry_is_an_error(tmp_path: Path) -> None:
    findings = run_doctor(tmp_path)
    assert has_errors(findings)
    assert findings[0].check == "registry"


def test_router_out_of_sync_is_an_error(vault: Path) -> None:
    router = vault / "ROUTER.md"
    router.write_text(router.read_text(encoding="utf-8") + "\nextra line\n", encoding="utf-8")
    findings = run_doctor(vault)
    assert any(f.check == "router" and f.severity == "error" for f in findings)


def test_unknown_status_and_type_are_errors(vault: Path) -> None:
    write(
        vault,
        "ProductWiki/topics/bad-meta.md",
        "---\ntitle: Bad\ntype: rumor\nstatus: vibing\n---\n\n# Bad\n",
    )
    findings = run_doctor(vault)
    messages = _errors(list(findings))
    assert any("unknown knowledge type" in m for m in messages)
    assert any("unknown lifecycle status" in m for m in messages)


def test_superseded_without_target_is_an_error(vault: Path) -> None:
    write(
        vault,
        "ProductWiki/topics/orphan.md",
        "---\ntitle: Orphan\ntype: fact\nstatus: superseded\n---\n\n# Orphan\n",
    )
    findings = run_doctor(vault)
    assert any("superseded_by" in f.message for f in findings if f.severity == "error")


def test_dead_link_is_an_error(vault: Path) -> None:
    write(
        vault,
        "ProductWiki/topics/broken.md",
        "---\ntitle: Broken\ntype: fact\nstatus: active\nprovenance:\n  - synthetic\n---\n\n"
        "See [gone](gone.md).\n",
    )
    findings = run_doctor(vault)
    assert any("dead link" in f.message for f in findings if f.severity == "error")


def test_bad_date_is_an_error(vault: Path) -> None:
    write(
        vault,
        "ProductWiki/topics/bad-date.md",
        "---\ntitle: Bad date\ntype: fact\nstatus: active\nupdated: someday\n"
        "provenance:\n  - synthetic\n---\n\n# Bad date\n",
    )
    findings = run_doctor(vault)
    assert any("ISO date" in f.message for f in findings if f.severity == "error")


def test_unsafe_symlink_is_an_error(vault: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-secret"
    outside.mkdir()
    (vault / "ProductWiki" / "leak").symlink_to(outside)
    findings = run_doctor(vault)
    assert any(f.check == "symlinks" and f.severity == "error" for f in findings)


def test_missing_provenance_is_a_warning_not_error(vault: Path) -> None:
    write(
        vault,
        "ProductWiki/topics/no-prov.md",
        "---\ntitle: No prov\ntype: fact\nstatus: active\n---\n\n# No prov\n",
    )
    findings = run_doctor(vault)
    provenance = [f for f in findings if f.check == "provenance"]
    assert provenance and all(f.severity == "warning" for f in provenance)


def test_malformed_proposal_is_an_error(vault: Path) -> None:
    write(
        vault,
        ".megamind/proposals/deadbeef0000.md",
        "---\nmegamind: proposal\nid: mismatch\nstatus: wat\ntype: rumor\n---\n\nBody.\n",
    )
    findings = run_doctor(vault)
    messages = _errors(list(findings))
    assert any("does not match filename" in m for m in messages)
    assert any("unknown proposal status" in m for m in messages)
    assert any("lacks a source" in m for m in messages)


def test_unicode_digit_frontmatter_does_not_crash_doctor(vault: Path) -> None:
    page = vault / "ProductWiki/topics/pricing-model.md"
    page.write_text(
        "---\ntitle: Pricing\ntype: fact\nstatus: active\nqty: ²\n---\n\n# Pricing\n",
        encoding="utf-8",
    )
    findings = run_doctor(vault)
    assert not [f for f in findings if "pricing-model.md" in f.path and "frontmatter" in f.message]


def test_doctor_warns_on_v1_registry_with_migration_guidance(tmp_path: Path) -> None:
    import json

    directory = tmp_path / ".megamind"
    directory.mkdir()
    payload = {"version": 1, "wikis": [{"name": "W", "path": "W", "keywords": ["w"]}]}
    (directory / "registry.json").write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "W").mkdir()
    findings = run_doctor(tmp_path)
    matches = [f for f in findings if "schema v1" in f.message]
    assert len(matches) == 1
    assert matches[0].severity == "warning"
    assert "megamind-axi migrate" in matches[0].message


def test_doctor_reports_access_policy_invariants(vault: Path) -> None:
    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    assert product is not None
    product.model_access = ModelAccess(local="full", cloud="full")
    product.sensitivity = "personal-local"  # contradiction: cloud can never be full
    save_registry(vault, registry)
    findings = run_doctor(vault)
    access_errors = [f for f in findings if f.check == "access" and f.severity == "error"]
    assert any("restrictive value wins" in f.message for f in access_errors)


def test_doctor_warns_when_company_wiki_lacks_explicit_cloud_policy(vault: Path) -> None:
    registry = load_registry(vault)
    brand = registry.wiki_by_name("BrandingWiki")
    assert brand is not None
    brand.model_access = ModelAccess()  # unset: derived restrictive default
    save_registry(vault, registry)
    findings = run_doctor(vault)
    assert any(
        f.check == "access" and "explicit cloud access policy" in f.message for f in findings
    )
