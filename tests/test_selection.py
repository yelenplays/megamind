"""Explicit selection of a currently offered preflight wiki.

The public seams under test are the ``select-offer`` AXI command and the
``select_offer`` library function. The original regression is a sub-floor
single offer: rephrasing the request adds lexical evidence but still cannot
cross the reliance floor, while selection of the original offer must authorize
only its already-declared access surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from megamind import toon
from megamind.registry import ContextBudget, ModelAccess, WikiEntry, load_registry, save_registry
from test_cli import run_json, run_toon


def _finance_vault(vault: Path) -> Path:
    registry = load_registry(vault)
    wiki = vault / "FinanzWiki"
    wiki.mkdir()
    (wiki / "DIGEST.md").write_text("# Synthetic finance digest\n", encoding="utf-8")
    registry.wikis.append(
        WikiEntry(
            name="FinanzWiki",
            path="FinanzWiki",
            privacy="personal-local",
            purpose="Answers allocation and retirement questions.",
            sensitivity="personal-local",
            model_access=ModelAccess(local="full", cloud="digest-only"),
            catalog_visibility="full",
            digest="FinanzWiki/DIGEST.md",
            context_budget=ContextBudget(max_candidates=1, max_context_chars=1440),
        )
    )
    save_registry(vault, registry)
    return vault


def _preflight(
    capsys: pytest.CaptureFixture[str], vault: Path, request: str, model_class: str = "cloud"
) -> dict[str, Any]:
    code, document, error = run_json(
        capsys,
        "--root",
        str(vault),
        "preflight",
        request,
        "--model-class",
        model_class,
        "--full",
    )
    assert code == 0
    assert error == ""
    return document


def _evidence(tmp_path: Path, document: dict[str, Any]) -> Path:
    path = tmp_path / "preflight.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _select_args(
    vault: Path, evidence: Path, request: str, wiki: str = "FinanzWiki", model_class: str = "cloud"
) -> tuple[str, ...]:
    return (
        "--root",
        str(vault),
        "select-offer",
        wiki,
        "--request",
        request,
        "--preflight-result",
        str(evidence),
        "--model-class",
        model_class,
    )


def test_user_can_select_the_original_single_digest_offer(
    vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    request = "allocation retirement strategy"
    vault = _finance_vault(vault)
    original = _preflight(capsys, vault, request)
    assert original["status"] == "ambiguous"
    assert original["confidence"] == 0.4467
    assert [offer["name"] for offer in original["offers"]] == ["FinanzWiki"]

    # The historical mask: naming the wiki raises lexical coverage but still
    # leaves no authorized load path. Selection must bind the original request,
    # not treat this second request as authority.
    rephrased = _preflight(capsys, vault, "use FinanzWiki for allocation retirement strategy")
    assert rephrased["status"] == "ambiguous"
    assert rephrased["confidence"] == 0.66
    assert rephrased["matches"] == []

    code, selected, error = run_json(
        capsys, *_select_args(vault, _evidence(tmp_path, original), request)
    )

    assert code == 0
    assert error == ""
    assert selected["schema_version"] == "megamind/preflight-selection-result/v1"
    assert selected["status"] == "authorized"
    assert selected["preflight_id"] == original["preflight_id"]
    assert selected["request_hash"] == original["request_hash"]
    assert selected["catalog_hash"] == original["catalog_hash"]
    assert selected["model_class"] == "cloud"
    assert selected["selection"] == {
        "status": "explicit-user-selection",
        "basis": "selected-current-offer",
        "source_disposition": "offer",
        "source_status": "ambiguous",
        "preflight_id": original["preflight_id"],
        "confidence_changed": False,
    }
    wiki = selected["selected"]
    assert wiki["name"] == "FinanzWiki"
    assert wiki["confidence"] == original["offers"][0]["confidence"]
    assert wiki["evidence"] == original["offers"][0]["evidence"]
    assert wiki["reasons"] == original["offers"][0]["reasons"]
    assert wiki["access"] == "digest-only"
    assert wiki["allows"] == ["FinanzWiki/DIGEST.md"]
    assert wiki["context_budget"] == {"max_candidates": 1, "max_context_chars": 1440}
    assert selected["selection_id"]
    assert selected["help"]


def _add_offer(
    vault: Path,
    name: str,
    *,
    keyword: str = "allocation",
    privacy: str = "digest-only",
    provisional: bool = False,
    digest: bool = True,
    pointer: bool = False,
) -> None:
    registry = load_registry(vault)
    root = vault / name
    root.mkdir()
    digest_path = f"{name}/DIGEST.md" if digest else ""
    if digest:
        (root / "DIGEST.md").write_text(f"# Synthetic {name} digest\n", encoding="utf-8")
    registry.wikis.append(
        WikiEntry(
            name=name,
            path=name,
            privacy="pointer-only" if pointer else privacy,
            purpose=f"Synthetic {name} allocation scope.",
            keywords=[keyword],
            sensitivity="public-reference" if privacy == "public-reference" else "",
            model_access=ModelAccess(
                local="none" if pointer else "digest-only",
                cloud="none" if pointer else "digest-only",
            ),
            routing_mode="pointer" if pointer else "full",
            digest=digest_path,
            provisional=provisional,
        )
    )
    save_registry(vault, registry)


def test_selected_full_offer_uses_only_its_existing_bounded_ladder(
    vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    assert product is not None
    product.context_budget = ContextBudget(max_candidates=2, max_context_chars=2222)
    save_registry(vault, registry)
    request = "knowledge base"
    original = _preflight(capsys, vault, request, model_class="local")
    assert [offer["name"] for offer in original["offers"]] == ["ProductWiki"]

    code, selected, _ = run_json(
        capsys,
        *_select_args(
            vault,
            _evidence(tmp_path, original),
            request,
            wiki="ProductWiki",
            model_class="local",
        ),
    )

    assert code == 0
    wiki = selected["selected"]
    assert wiki["access"] == "full"
    assert wiki["allows"] == [
        "ProductWiki/CARD.md",
        "ProductWiki/DIGEST.md",
        "ProductWiki/INDEX.md",
    ]
    assert "topics/" not in json.dumps(wiki)
    assert "route" in wiki["follow_up"]
    assert wiki["context_budget"] == {"max_candidates": 2, "max_context_chars": 2222}


def test_user_can_choose_exactly_one_of_multiple_current_offers(
    vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_offer(vault, "AlphaFinance")
    _add_offer(vault, "BetaFinance")
    request = "allocation"
    original = _preflight(capsys, vault, request)
    assert original["status"] == "ambiguous"
    assert [entry["name"] for entry in original["offers"]] == ["AlphaFinance", "BetaFinance"]
    evidence = _evidence(tmp_path, original)

    code, selected, _ = run_json(
        capsys, *_select_args(vault, evidence, request, wiki="BetaFinance")
    )

    assert code == 0
    assert selected["selected"]["name"] == "BetaFinance"
    assert selected["selected"]["allows"] == ["BetaFinance/DIGEST.md"]
    assert "AlphaFinance" not in json.dumps(selected["selected"])


@pytest.mark.parametrize("change", ["request", "model", "catalog", "unknown", "duplicate"])
def test_selection_refuses_stale_swapped_or_unknown_evidence(
    vault: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    change: str,
) -> None:
    _add_offer(vault, "AlphaFinance")
    request = "allocation planning review"
    original = _preflight(capsys, vault, request)
    evidence = _evidence(tmp_path, original)
    selected_request = request
    model_class = "cloud"
    wiki = "AlphaFinance"
    if change == "request":
        selected_request = "rephrased allocation planning"
    elif change == "model":
        model_class = "local"
    elif change == "catalog":
        registry = load_registry(vault)
        current = registry.wiki_by_name("AlphaFinance")
        assert current is not None
        current.purpose = "Changed synthetic scope."
        save_registry(vault, registry)
    elif change == "unknown":
        wiki = "UnknownFinance"
    else:
        original["offers"].append(dict(original["offers"][0]))
        proof = {
            "request_hash": original["request_hash"],
            "catalog_hash": original["catalog_hash"],
            "model_class": original["model_class"],
            "status": original["status"],
            "matches": [item["name"] for item in original["matches"]],
            "offers": [item["name"] for item in original["offers"]],
            "filtered": [item["name"] for item in original["filtered"]],
            "redacted_count": original["redacted_count"],
        }
        from megamind.fsops import content_hash

        original["preflight_id"] = content_hash(json.dumps(proof, sort_keys=True))
        evidence = _evidence(tmp_path, original)

    code, document, error = run_json(
        capsys,
        *_select_args(vault, evidence, selected_request, wiki=wiki, model_class=model_class),
    )

    assert code == 1
    assert error == ""
    assert document["schema_version"] == "megamind/error/v1"
    assert document["code"] == "selection_invalid"
    assert document["help"]


def test_filtered_wiki_cannot_be_inserted_as_an_offer(
    vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = _preflight(capsys, vault, "brand color palette")
    assert original["status"] == "privacy-filtered"
    filtered = original["filtered"][0]
    original["offers"] = [
        {
            "name": filtered["name"],
            "root": filtered["root"],
            "score": 1,
            "confidence": {"score": 1.0, "meets_floor": True},
            "freshness": {},
            "provisional": False,
            "evidence": {},
            "reasons": [],
        }
    ]
    evidence = _evidence(tmp_path, original)

    code, document, _ = run_json(
        capsys,
        *_select_args(vault, evidence, "brand color palette", wiki="BrandingWiki"),
    )

    assert code == 1
    assert document["code"] == "selection_invalid"
    assert "path" not in json.dumps(document).lower()


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("provisional", "provisional"), ("pointer", "pointer"), ("absent-digest", "digest")],
)
def test_selection_cannot_override_non_loadable_offer_governance(
    vault: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    expected: str,
) -> None:
    if kind == "provisional":
        _add_offer(vault, "GuardedWiki", provisional=True)
    elif kind == "pointer":
        _add_offer(vault, "GuardedWiki", pointer=True)
    else:
        _add_offer(vault, "GuardedWiki", digest=False)
    request = "allocation planning review"
    original = _preflight(capsys, vault, request)
    assert [item["name"] for item in original["offers"]] == ["GuardedWiki"]

    code, document, _ = run_json(
        capsys,
        *_select_args(vault, _evidence(tmp_path, original), request, wiki="GuardedWiki"),
    )

    assert code == 1
    assert document["code"] == "selection_invalid"
    assert expected in document["message"]


def test_selection_refuses_an_offer_whose_wiki_root_disappeared(
    vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_offer(vault, "AlphaFinance")
    request = "allocation planning review"
    original = _preflight(capsys, vault, request)
    (vault / "AlphaFinance/DIGEST.md").unlink()
    (vault / "AlphaFinance").rmdir()

    code, document, _ = run_json(
        capsys,
        *_select_args(vault, _evidence(tmp_path, original), request, wiki="AlphaFinance"),
    )

    assert code == 1
    assert document["code"] == "selection_invalid"
    assert "catalog changed" in document["message"]


def test_tampered_offer_evidence_is_refused(
    vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_offer(vault, "AlphaFinance")
    request = "allocation planning review"
    original = _preflight(capsys, vault, request)
    original["offers"][0]["confidence"]["score"] = 0.99
    evidence = _evidence(tmp_path, original)

    code, document, _ = run_json(
        capsys, *_select_args(vault, evidence, request, wiki="AlphaFinance")
    )

    assert code == 1
    assert document["code"] == "selection_invalid"
    assert "malformed" in document["message"]


def test_semantic_preflight_offer_can_be_replayed_exactly(
    vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_offer(vault, "AlphaFinance")
    request = "allocation planning review"
    code, original, _ = run_json(
        capsys,
        "--root",
        str(vault),
        "preflight",
        request,
        "--model-class",
        "cloud",
        "--semantic",
        "--full",
    )
    assert code == 0
    assert original["semantic"] == {"status": "ok", "backend": "char-ngram"}
    assert original["offers"][0]["evidence"]["semantic"] is not None

    code, selected, _ = run_json(
        capsys,
        *_select_args(vault, _evidence(tmp_path, original), request, wiki="AlphaFinance"),
    )

    assert code == 0
    assert selected["selected"]["evidence"] == original["offers"][0]["evidence"]


def test_selection_refuses_a_digest_symlink_escape(
    vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_offer(vault, "GuardedWiki")
    outside = tmp_path / "outside.md"
    outside.write_text("synthetic outside\n", encoding="utf-8")
    digest = vault / "GuardedWiki/DIGEST.md"
    digest.unlink()
    digest.symlink_to(outside)
    request = "allocation planning review"
    original = _preflight(capsys, vault, request)

    code, document, _ = run_json(
        capsys,
        *_select_args(vault, _evidence(tmp_path, original), request, wiki="GuardedWiki"),
    )

    assert code == 1
    assert document["code"] == "selection_invalid"
    assert "symlink" in document["message"]
    assert str(outside) not in json.dumps(document)


@pytest.mark.parametrize("payload", ["[]", "null", "3", "{", '"text"'])
def test_malformed_preflight_input_is_one_typed_document(
    vault: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: str,
) -> None:
    evidence = tmp_path / "bad.json"
    evidence.write_text(payload, encoding="utf-8")
    code, document, error = run_json(
        capsys, *_select_args(vault, evidence, "allocation", wiki="Anything")
    )
    assert code == 1
    assert error == ""
    assert document["schema_version"] == "megamind/error/v1"
    assert document["code"] == "selection_invalid"


def test_selection_is_byte_stable_and_toon_json_equivalent(
    vault: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _add_offer(vault, "AlphaFinance")
    request = "allocation planning review"
    evidence = _evidence(tmp_path, _preflight(capsys, vault, request))
    args = _select_args(vault, evidence, request, wiki="AlphaFinance")

    code_json, document, error_json = run_json(capsys, *args)
    code_toon, toon_output, error_toon = run_toon(capsys, *args)
    code_replay, replay_output, error_replay = run_toon(capsys, *args)

    assert code_json == code_toon == code_replay == 0
    assert error_json == error_toon == error_replay == ""
    assert toon.encode(document) == toon_output == replay_output
