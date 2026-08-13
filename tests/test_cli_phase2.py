"""Phase 2 CLI: assess command, route/preflight v2 documents, semantic flags."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import build_vault
from megamind import toon
from test_cli import run_json, run_toon


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return build_vault(tmp_path)


# --- assess claim -------------------------------------------------------------


def test_assess_claim_above_the_floor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, err = run_json(
        capsys,
        "assess",
        "claim",
        "--source",
        "primary:release-notes",
        "--source",
        "primary:changelog",
        "--lifecycle",
        "active",
        "--freshness",
        "fresh",
    )
    assert code == 0
    assert err == ""
    assert doc["schema_version"] == "megamind/confidence-report/v1"
    assert doc["kind"] == "claim"
    assert doc["score"] == 0.9
    assert doc["meets_floor"] is True
    assert doc["reliance_floor"] == 0.75
    assert doc["components"]


def source_json(quality: str, origin: str, **facts: object) -> str:
    return json.dumps({"quality": quality, "origin": origin, **facts})


def test_assess_claim_corroborates_only_by_declared_origin_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Independence has to be declarable at the boundary, or it cannot exist."""
    argv = (
        "assess",
        "claim",
        "--source-json",
        source_json("primary", "https://vendor.example/release-notes", origin_id="vendor-notes"),
        "--source-json",
        source_json("primary", "https://vendor.example/changelog", origin_id="vendor-changelog"),
        "--lifecycle",
        "active",
        "--freshness",
        "fresh",
    )
    code, doc, err = run_json(capsys, *argv)
    assert code == 0
    assert err == ""
    assert doc["score"] == 0.95
    assert doc["meets_floor"] is True
    assert any(
        component["factor"] == "corroboration" and component["effect"] == "+0.05"
        for component in doc["components"]
    )
    code_toon, toon_out, _ = run_toon(capsys, *argv)
    assert code_toon == 0
    assert toon.encode(doc) == toon_out


def test_assess_claim_reposted_urls_share_one_origin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(
        capsys,
        "assess",
        "claim",
        "--source-json",
        source_json("primary", "https://source.example/a", origin_id="wire-report"),
        "--source-json",
        source_json("primary", "https://blog.example/repost-of-a", origin_id="wire-report"),
        "--lifecycle",
        "active",
        "--freshness",
        "fresh",
    )
    assert code == 0
    assert doc["score"] == 0.9
    assert any(
        component["factor"] == "corroboration" and component["effect"] == "+0.00"
        for component in doc["components"]
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param("http://[::1]/docs", "http://[::2]/docs", id="ipv6-literal"),
        pytest.param("docs for std::vector", "docs for std::array", id="cpp-namespace"),
        pytest.param("Space::Page", "Other::Page", id="wiki-namespace"),
    ],
)
def test_assess_claim_origin_separators_never_become_independence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], first: str, second: str
) -> None:
    """A display origin is opaque: no substring of it can buy corroboration."""
    code, doc, _ = run_json(
        capsys,
        "assess",
        "claim",
        "--source",
        f"synthesis:{first}",
        "--source",
        f"synthesis:{second}",
        "--lifecycle",
        "active",
        "--freshness",
        "fresh",
    )
    assert code == 0
    assert doc["score"] == 0.7
    assert doc["meets_floor"] is False
    assert doc["input"]["sources"] == [f"synthesis:{first}", f"synthesis:{second}"]
    assert any(
        component["factor"] == "corroboration" and component["effect"] == "+0.00"
        for component in doc["components"]
    )


def test_assess_claim_json_source_keeps_a_separator_bearing_origin_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(
        capsys,
        "assess",
        "claim",
        "--source-json",
        source_json("primary", "http://[::1]/docs", origin_id="host-a"),
        "--source-json",
        source_json("primary", "http://[::2]/docs", origin_id="host-a"),
        "--lifecycle",
        "active",
        "--freshness",
        "fresh",
    )
    assert code == 0
    assert doc["score"] == 0.9
    assert any(
        "http://[::1]/docs" in component["detail"] or component["factor"] != "source"
        for component in doc["components"]
    )


def test_assess_claim_retracted_source_is_removed_not_downweighted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(
        capsys,
        "assess",
        "claim",
        "--source-json",
        source_json("primary", "study", origin_id="doi:10.1000/xyz", correction_status="retracted"),
        "--lifecycle",
        "active",
        "--freshness",
        "fresh",
    )
    assert code == 0
    assert doc["score"] == "unknown"
    assert doc["meets_floor"] is False
    assert any("retracted, removed" in component["detail"] for component in doc["components"])


def test_assess_claim_json_source_can_be_ineligible(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(
        capsys,
        "assess",
        "claim",
        "--source-json",
        source_json("primary", "restricted", origin_id="vendor-a", eligible=False),
    )
    assert code == 0
    assert doc["score"] == "unknown"
    assert any("ineligible, ignored" in component["detail"] for component in doc["components"])


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("[]", id="not-an-object"),
        pytest.param('{"quality":"primary"}', id="missing-origin"),
        pytest.param('{"quality":"vibes","origin":"x"}', id="unknown-quality"),
        pytest.param('{"quality":"primary","origin":"  "}', id="blank-origin"),
        pytest.param('{"quality":"primary","origin":"x","origin_id":" "}', id="blank-origin-id"),
        pytest.param(
            '{"quality":"primary","origin":"x","correction_status":"probably-fine"}',
            id="unknown-correction-status",
        ),
        pytest.param('{"quality":"primary","origin":"x","eligible":"yes"}', id="non-bool-eligible"),
        pytest.param('{"quality":"primary","origin":"x","api_key":"CANARY"}', id="unknown-field"),
    ],
)
def test_assess_claim_rejects_malformed_source_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    code, doc, _ = run_json(capsys, "assess", "claim", "--source-json", bad)
    assert code == 2
    assert doc["code"] == "usage_error"
    assert "CANARY" not in doc["message"]


def test_assess_claim_unknown_stays_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(capsys, "assess", "claim")
    assert code == 0
    assert doc["score"] == "unknown"
    assert doc["meets_floor"] is False
    assert "never fabricated" in doc["help"][0]


def test_assess_claim_contradiction_freezes_below_the_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(
        capsys,
        "assess",
        "claim",
        "--source",
        "primary:a",
        "--source",
        "primary:b",
        "--lifecycle",
        "active",
        "--freshness",
        "fresh",
        "--contradicted",
    )
    assert code == 0
    assert doc["score"] == 0.5
    assert doc["meets_floor"] is False


def test_assess_claim_rejects_bad_source_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for bad in ("nonsense", "vibes:origin", "primary:", "  :origin"):
        code, doc, _ = run_json(capsys, "assess", "claim", "--source", bad)
        assert code == 2, bad
        assert doc["code"] == "usage_error"


def test_assess_claim_toon_json_parity(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = ("assess", "claim", "--source", "synthesis:wiki-page", "--freshness", "stale")
    code_j, doc, _ = run_json(capsys, *argv)
    code_t, toon_out, _ = run_toon(capsys, *argv)
    assert code_j == code_t == 0
    assert toon.encode(doc) == toon_out


# --- assess answer ------------------------------------------------------------


def test_assess_answer_caps_at_the_weakest_claim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(capsys, "assess", "answer", "--claim", "0.9", "--claim", "0.6")
    assert code == 0
    assert doc["kind"] == "answer"
    assert doc["score"] == 0.6
    assert doc["meets_floor"] is False


def test_assess_answer_inherits_unknown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, doc, _ = run_json(capsys, "assess", "answer", "--claim", "0.9", "--claim", "unknown")
    assert code == 0
    assert doc["score"] == "unknown"
    assert doc["meets_floor"] is False


def test_assess_answer_validates_claim_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for bad in ("1.5", "-0.1", "high"):
        code, doc, _ = run_json(capsys, "assess", "answer", "--claim", bad)
        assert code == 2
        assert doc["code"] == "usage_error"


def test_assess_without_subcommand_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc, _ = run_json(capsys, "assess")
    assert code == 2
    assert doc["code"] == "usage_error"


# --- route v2 -----------------------------------------------------------------


def test_route_v2_document_shape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, err = run_json(capsys, "--root", str(vault), "route", "pricing model")
    assert code == 0
    assert err == ""
    assert doc["schema_version"] == "megamind/route-result/v2"
    assert doc["decision"] == "load"
    assert doc["confidence"] >= 0.75
    assert doc["thresholds"] == {
        "reliance_floor": 0.75,
        "offer_floor": 0.25,
        "ambiguity_band": 0.05,
    }
    assert doc["semantic"]["status"] == "disabled"


def test_route_offer_decision_help(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(capsys, "--root", str(vault), "route", "synthetic knowledge")
    assert code == 0
    assert doc["decision"] == "offer"
    assert doc["matched"] is True
    assert any("choices" in entry for entry in doc["help"])


def test_route_fields_accept_phase2_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    argv = (
        "--root",
        str(vault),
        "route",
        "pricing model",
        "--fields",
        "path,confidence,freshness,semantic_score",
        "--today",
        "2026-08-10",
    )
    code_j, doc, _ = run_json(capsys, *argv)
    code_t, toon_out, _ = run_toon(capsys, *argv)
    assert code_j == code_t == 0
    assert toon.encode(doc) == toon_out
    row = doc["candidates"][0]
    assert row["confidence"] >= 0.75
    assert row["freshness"]["stale"] is True
    assert row["semantic_score"] is None


def test_route_semantic_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, err = run_json(capsys, "--root", str(vault), "route", "pricing model", "--semantic")
    assert code == 0
    assert err == ""
    assert doc["semantic"]["status"] == "ok"
    assert doc["semantic"]["backend"] == "char-ngram"


def test_route_output_is_byte_repeatable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    argv = (
        "--root",
        str(vault),
        "route",
        "brand color palette",
        "--semantic",
        "--today",
        "2026-08-10",
    )
    _, first, _ = run_toon(capsys, *argv)
    _, second, _ = run_toon(capsys, *argv)
    assert first == second


# --- preflight v2 --------------------------------------------------------------


def test_preflight_v2_offer_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    code, doc, _ = run_json(
        capsys, "--root", str(vault), "preflight", "knowledge base", "--model-class", "local"
    )
    assert code == 0
    assert doc["schema_version"] == "megamind/preflight-result/v2"
    assert doc["status"] == "ambiguous"
    assert doc["confidence"] < 0.75
    assert doc["matches"] == []
    assert doc["offers"][0]["confidence"]["meets_floor"] is False
    assert any("choices" in entry for entry in doc["help"])


def test_preflight_semantic_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = build_vault(tmp_path)
    argv = ("--root", str(vault), "preflight", "pricing", "--model-class", "local", "--semantic")
    code_j, doc, _ = run_json(capsys, *argv)
    code_t, toon_out, _ = run_toon(capsys, *argv)
    assert code_j == code_t == 0
    assert toon.encode(doc) == toon_out
    assert doc["semantic"]["status"] == "ok"
    assert doc["matches"][0]["evidence"]["semantic"] is not None


def test_preflight_output_is_byte_repeatable_with_semantic(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vault = build_vault(tmp_path)
    argv = (
        "--root",
        str(vault),
        "preflight",
        "pricing",
        "--model-class",
        "cloud",
        "--semantic",
        "--today",
        "2026-08-10",
    )
    _, first, _ = run_toon(capsys, *argv)
    _, second, _ = run_toon(capsys, *argv)
    assert first == second
