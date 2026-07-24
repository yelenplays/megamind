from __future__ import annotations

from pathlib import Path

from conftest import write
from megamind.doctor import has_errors, run_doctor


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
