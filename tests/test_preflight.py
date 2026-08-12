"""Preflight: model-class filtering, explicit states, proof identity, read-only."""

from __future__ import annotations

import hashlib
import json
import shlex
import socket
from datetime import date
from pathlib import Path

import pytest

from conftest import build_vault
from megamind.catalog import discover_roots
from megamind.preflight import run_preflight
from megamind.registry import WikiEntry, load_registry, save_registry
from megamind.scaffold import init_wiki_root

TODAY = date(2026, 8, 10)


def _ref(root: Path):
    from megamind.catalog import RootRef

    return RootRef(label=str(root), path=root)


def _snapshot(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


# --- states and filtering -------------------------------------------------------


def test_matched_full_access_returns_card_level_paths(vault: Path) -> None:
    result = run_preflight([_ref(vault)], "how does pricing work", "cloud")
    assert result.status == "matched"
    best = result.matches[0]
    assert best["name"] == "ProductWiki"
    assert best["access"] == "full"
    assert "ProductWiki/CARD.md" in best["allows"]
    assert "ProductWiki/topics/pricing-model.md" not in best["allows"]  # no page content
    assert "megamind-axi --root" in str(best["follow_up"])
    assert "route" in str(best["follow_up"])


def test_follow_up_command_shell_quotes_the_request(vault: Path) -> None:
    """The request is untrusted: the emitted command must stay exactly one command."""
    request = 'pricing product" ; rm -rf ~ #'
    result = run_preflight([_ref(vault)], request, "local")
    assert result.status == "matched"
    follow_up = str(result.matches[0]["follow_up"])
    command = follow_up.split("`")[1]
    tokens = shlex.split(command)
    assert tokens[-1] == request  # the whole request is a single argument
    assert ";" not in tokens
    assert "rm" not in tokens


def test_follow_up_command_shell_quotes_the_root(tmp_path: Path) -> None:
    """A root path with a space stays one `--root` argument, so the command runs."""
    estate = tmp_path / "My Wikis"
    estate.mkdir()
    root = build_vault(estate)
    result = run_preflight([_ref(root)], "pricing", "local")
    assert result.status == "matched"
    command = str(result.matches[0]["follow_up"]).split("`")[1]
    tokens = shlex.split(command)
    assert tokens[:2] == ["megamind-axi", "--root"]
    assert tokens[2] == str(root)  # the whole root path is a single argument
    assert tokens[3] == "route"


def test_cloud_none_wiki_is_privacy_filtered(vault: Path) -> None:
    cloud = run_preflight([_ref(vault)], "brand color palette", "cloud")
    assert cloud.status == "privacy-filtered"
    assert cloud.matches == []
    assert cloud.filtered[0]["name"] == "BrandingWiki"
    assert "none" in str(cloud.filtered[0]["reason"])
    # no paths leak for a filtered wiki
    assert "BrandingWiki/" not in json.dumps(cloud.filtered)

    local = run_preflight([_ref(vault)], "brand color palette", "local")
    assert local.status == "matched"
    assert local.matches[0]["name"] == "BrandingWiki"


def test_digest_only_match_allows_only_the_digest(vault: Path) -> None:
    result = run_preflight([_ref(vault)], "research interview findings", "local")
    assert result.status == "matched"
    match = result.matches[0]
    assert match["name"] == "ResearchDigest"
    assert match["access"] == "digest-only"
    assert match["allows"] == ["ResearchDigest/DIGEST.md"]


def test_pointer_match_exposes_location_metadata_only(vault: Path) -> None:
    result = run_preflight([_ref(vault)], "archive history", "local")
    assert result.status == "matched"
    match = result.matches[0]
    assert match["name"] == "ArchiveBox"
    assert match["routing_mode"] == "pointer"
    assert match["allows"] == []
    assert "manually" in str(match["follow_up"])


def test_no_match_is_quiet(vault: Path) -> None:
    result = run_preflight([_ref(vault)], "quantum llama farming", "local")
    assert result.status == "no-match"
    assert result.matches == []
    assert result.filtered == []


def test_empty_request_is_a_definitive_no_match(vault: Path) -> None:
    result = run_preflight([_ref(vault)], "the of and", "local")
    assert result.status == "no-match"
    assert "no usable terms" in result.notes[0]


def test_unavailable_when_no_usable_cards(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    estate.mkdir()
    result = run_preflight(discover_roots(estate), "anything", "local")
    assert result.status == "unavailable"


def test_ambiguous_tie_offers_choices_without_loading(vault: Path) -> None:
    registry = load_registry(vault)
    for name in ("ZedAlpha", "ZedBeta"):
        (vault / name).mkdir()
        registry.wikis.append(
            WikiEntry(
                name=name,
                path=name,
                privacy="public-reference",
                keywords=["zeppelin"],
                sensitivity="public-reference",
            )
        )
    save_registry(vault, registry)
    result = run_preflight([_ref(vault)], "zeppelin", "local")
    assert result.status == "ambiguous"
    assert result.matches == []
    assert {str(offer["name"]) for offer in result.offers} == {"ZedAlpha", "ZedBeta"}
    assert all("allows" not in offer for offer in result.offers)


def test_negative_trigger_declines_a_wiki(vault: Path) -> None:
    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    assert product is not None
    product.negative_triggers = ["payroll"]
    save_registry(vault, registry)
    result = run_preflight([_ref(vault)], "payroll pricing", "local")
    assert any(entry["name"] == "ProductWiki" for entry in result.declined)
    assert all(match["name"] != "ProductWiki" for match in result.matches)


def test_hidden_wiki_is_never_named(vault: Path, tmp_path: Path) -> None:
    registry = load_registry(vault)
    registry.wikis.append(
        WikiEntry(
            name="HiddenWiki",
            path="HiddenWiki",
            privacy="personal-local",
            keywords=["diary"],
            catalog_visibility="hidden",
        )
    )
    save_registry(vault, registry)
    result = run_preflight([_ref(vault)], "diary", "local")
    assert result.redacted_count == 1
    assert "HiddenWiki" not in json.dumps(result.matches)
    assert "HiddenWiki" not in json.dumps(result.filtered)


# --- proof identity --------------------------------------------------------------


def test_proof_identity_is_deterministic_and_input_bound(vault: Path) -> None:
    first = run_preflight([_ref(vault)], "pricing", "cloud")
    second = run_preflight([_ref(vault)], "pricing", "cloud")
    assert first.preflight_id == second.preflight_id

    other_request = run_preflight([_ref(vault)], "brand colors", "cloud")
    assert other_request.preflight_id != first.preflight_id

    other_class = run_preflight([_ref(vault)], "pricing", "local")
    assert other_class.preflight_id != first.preflight_id

    registry = load_registry(vault)
    product = registry.wiki_by_name("ProductWiki")
    assert product is not None
    product.purpose = "Changed synthetic purpose."
    save_registry(vault, registry)
    changed_catalog = run_preflight([_ref(vault)], "pricing", "cloud")
    assert changed_catalog.preflight_id != first.preflight_id
    assert changed_catalog.catalog_hash != first.catalog_hash


def test_page_content_cannot_change_preflight_identity(vault: Path) -> None:
    first = run_preflight([_ref(vault)], "pricing", "cloud")
    page = vault / "ProductWiki/topics/pricing-model.md"
    page.write_text("---\nmalformed frontmatter\n---\n\nprivate changed body\n", encoding="utf-8")

    second = run_preflight([_ref(vault)], "pricing", "cloud")

    assert second.catalog_hash == first.catalog_hash
    assert second.preflight_id == first.preflight_id
    assert second.matches == first.matches


def test_proof_binds_request_hash_not_the_raw_request(vault: Path) -> None:
    result = run_preflight([_ref(vault)], "synthetic secret-flavored request", "cloud")
    assert result.request_hash
    assert result.preflight_id
    # the proof inputs carry the hash; nothing is persisted anywhere
    assert (
        not (vault / ".megamind" / "audit" / "log.jsonl")
        .read_text(encoding="utf-8")
        .count("secret-flavored")
    )


# --- safety ----------------------------------------------------------------------


def test_preflight_and_catalog_are_read_only(vault: Path) -> None:
    before = _snapshot(vault)
    run_preflight([_ref(vault)], "pricing", "local")
    from megamind.catalog import build_catalog, render_projection

    render_projection(build_catalog([_ref(vault)], today=TODAY))
    assert _snapshot(vault) == before


def test_no_command_touches_the_network(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)

    registry = load_registry(vault)
    from megamind.catalog import build_catalog
    from megamind.routing import route

    route(vault, registry, "pricing")
    build_catalog([_ref(vault)], today=TODAY)
    run_preflight([_ref(vault)], "pricing", "cloud")


def test_preflight_covers_canonical_card_roots(tmp_path: Path) -> None:
    estate = tmp_path / "estate"
    estate.mkdir()
    init_wiki_root(estate / "SoloWiki", "SoloWiki")
    from megamind.card import load_wiki_card, save_wiki_card

    card = load_wiki_card(estate / "SoloWiki")
    card.purpose = "Answers synthetic cider press questions."
    card.triggers = ["cider"]
    save_wiki_card(estate / "SoloWiki", card)

    result = run_preflight(discover_roots(estate), "cider press care", "local")
    assert result.status == "matched"
    match = result.matches[0]
    assert match["name"] == "SoloWiki"
    assert match["allows"] == ["wiki/index.md"]
    assert "wiki/index.md" in str(match["follow_up"])
    assert "context_budget" not in match

    cloud = run_preflight(discover_roots(estate), "cider press care", "cloud")
    assert cloud.status == "privacy-filtered"  # unclassified: cloud none
