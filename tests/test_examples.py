"""The committed examples vault must stay healthy and demonstrative."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from megamind.cli import main
from megamind.doctor import has_errors, run_doctor
from megamind.registry import load_registry
from megamind.review import review
from megamind.routing import route

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples" / "vault"


def test_examples_vault_passes_doctor() -> None:
    findings = run_doctor(EXAMPLES)
    errors = [f for f in findings if f.severity == "error"]
    assert not has_errors(findings), [f.message for f in errors]


def test_axi_worked_route_example_matches_real_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """docs/axi.md is the product contract, so its example is the real document."""
    contract = (REPO / "docs" / "axi.md").read_text(encoding="utf-8")
    blocks = re.findall(
        r"Example \(`megamind-axi route \"pricing\"` on the examples vault\):\n\n"
        r"```toon\n(.*?)```",
        contract,
        re.DOTALL,
    )
    assert len(blocks) == 1
    assert main(["--root", str(EXAMPLES), "route", "pricing"]) == 0
    assert capsys.readouterr().out == blocks[0]


def test_examples_vault_routes_to_current_pricing() -> None:
    registry = load_registry(EXAMPLES)
    result = route(EXAMPLES, registry, "how does pricing work now")
    assert result.matched
    assert result.candidates[0].path == "ProductWiki/topics/pricing-v2.md"


def test_examples_vault_respects_privacy_boundaries() -> None:
    registry = load_registry(EXAMPLES)
    research = route(EXAMPLES, registry, "research findings about notifications")
    assert research.candidates[0].kind == "digest"
    archive = route(EXAMPLES, registry, "old archived history")
    assert archive.candidates[0].kind == "pointer"
    assert archive.candidates[0].chars == 0


def test_examples_vault_demonstrates_supersession() -> None:
    old = (EXAMPLES / "ProductWiki/topics/pricing-model.md").read_text(encoding="utf-8")
    assert "status: superseded" in old
    assert "superseded_by: ProductWiki/topics/pricing-v2.md" in old


def test_examples_vault_shows_open_work_in_review() -> None:
    registry = load_registry(EXAMPLES)
    from datetime import date

    report = review(EXAMPLES, registry, today=date(2026, 6, 1))
    assert report.open_proposals
    assert any(c["kind"] == "micro-wiki" for c in report.promotion_candidates)
