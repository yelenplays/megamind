"""Access-policy derivation/clamping and the standalone wiki card."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from megamind.access import effective_policy, policy_findings
from megamind.card import CARD_PATH, CardError, load_wiki_card, save_wiki_card
from megamind.registry import ModelAccess, WikiEntry


def _entry(privacy: str = "public-reference", **kwargs: object) -> WikiEntry:
    return WikiEntry(name="W", path="W", privacy=privacy, **kwargs)  # type: ignore[arg-type]


# --- derivation defaults -------------------------------------------------------


@pytest.mark.parametrize(
    ("privacy", "local", "cloud", "mode"),
    [
        ("public-reference", "full", "full", "full"),
        ("company-private", "full", "none", "full"),
        ("personal-local", "full", "digest-only", "full"),
        ("digest-only", "digest-only", "digest-only", "full"),
        ("pointer-only", "none", "none", "pointer"),
        ("", "full", "none", "full"),
    ],
)
def test_privacy_derives_restrictive_defaults(
    privacy: str, local: str, cloud: str, mode: str
) -> None:
    policy = effective_policy(_entry(privacy))
    assert (policy.local, policy.cloud, policy.routing_mode) == (local, cloud, mode)
    assert "local" in policy.derived and "cloud" in policy.derived


def test_unclassified_sensitivity_caps_cloud_at_none() -> None:
    policy = effective_policy(_entry("public-reference", sensitivity="unclassified"))
    assert policy.cloud == "none"
    assert policy.local == "full"


def test_personal_sensitivity_caps_cloud_at_digest_only() -> None:
    policy = effective_policy(
        _entry("public-reference", sensitivity="personal-local", catalog_visibility="full")
    )
    assert policy.cloud == "digest-only"


def test_personal_defaults_to_redacted_catalog_visibility() -> None:
    policy = effective_policy(_entry("personal-local"))
    assert policy.catalog_visibility == "redacted"


# --- clamping of contradictory explicit values --------------------------------


def test_explicit_cloud_full_on_personal_is_clamped_and_reported() -> None:
    wiki = _entry(
        "personal-local",
        sensitivity="personal-local",
        model_access=ModelAccess(local="full", cloud="full"),
    )
    policy = effective_policy(wiki)
    assert policy.cloud == "digest-only"
    errors = [f for f in policy_findings(wiki) if f.severity == "error"]
    assert any("cloud" in f.message and "full" in f.message for f in errors)


def test_pointer_privacy_forces_pointer_mode_and_no_content() -> None:
    wiki = _entry("pointer-only", model_access=ModelAccess(local="full", cloud="full"))
    policy = effective_policy(wiki)
    assert (policy.local, policy.cloud, policy.routing_mode) == ("none", "none", "pointer")
    assert any(f.severity == "error" for f in policy_findings(wiki))


def test_digest_only_privacy_caps_explicit_full() -> None:
    wiki = _entry("digest-only", model_access=ModelAccess(local="full"))
    assert effective_policy(wiki).local == "digest-only"


# --- policy findings -----------------------------------------------------------


def test_company_private_without_explicit_cloud_policy_warns() -> None:
    wiki = _entry("company-private")
    messages = [f.message for f in policy_findings(wiki) if f.severity == "warning"]
    assert any("explicit cloud access policy" in message for message in messages)


def test_explicit_company_policy_clears_the_warning() -> None:
    wiki = _entry(
        "company-private",
        sensitivity="company-private",
        model_access=ModelAccess(local="full", cloud="digest-only"),
    )
    assert policy_findings(wiki) == []


def test_pointer_mode_with_digest_warns() -> None:
    wiki = _entry("pointer-only", digest="Box/DIGEST.md")
    assert any("digest" in f.message for f in policy_findings(wiki))


def test_allowlist_without_status_warns() -> None:
    from megamind.registry import SourcePolicy

    wiki = _entry(
        "public-reference",
        source_policy=SourcePolicy(allowlist="W/SOURCES.md", allowlist_status="none"),
    )
    assert any("allowlist" in f.message for f in policy_findings(wiki))


# --- the standalone wiki card ---------------------------------------------------


def _card_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "megamind/wiki-card/v2",
        "version": 2,
        "name": "SoloWiki",
        "purpose": "Answers synthetic solo questions.",
        "index": "wiki/index.md",
    }
    payload.update(overrides)
    return payload


def test_card_round_trip(tmp_path: Path) -> None:
    card = load_wiki_card  # keep the import used before fixtures write files
    path = tmp_path / CARD_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_card_payload()), encoding="utf-8")
    entry = card(tmp_path)
    assert entry.name == "SoloWiki"
    assert entry.path == "."
    assert entry.privacy == ""
    assert entry.index == "wiki/index.md"
    policy = effective_policy(entry)
    assert (policy.local, policy.cloud) == ("full", "none")

    save_wiki_card(tmp_path, entry)
    assert load_wiki_card(tmp_path) == entry


def test_card_rejects_wrong_schema_and_version(tmp_path: Path) -> None:
    path = tmp_path / CARD_PATH
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_card_payload(schema="bogus")), encoding="utf-8")
    with pytest.raises(CardError, match="schema"):
        load_wiki_card(tmp_path)
    path.write_text(json.dumps(_card_payload(version=1)), encoding="utf-8")
    with pytest.raises(CardError, match="version"):
        load_wiki_card(tmp_path)


def test_card_malformed_json_and_bad_enums(tmp_path: Path) -> None:
    path = tmp_path / CARD_PATH
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CardError, match="not valid JSON"):
        load_wiki_card(tmp_path)
    path.write_text(json.dumps(_card_payload(model_access={"cloud": "everything"})))
    with pytest.raises(CardError, match="must be one of"):
        load_wiki_card(tmp_path)


def test_missing_card_is_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(CardError, match="no wiki card"):
        load_wiki_card(tmp_path)
