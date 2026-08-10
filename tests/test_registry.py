from __future__ import annotations

import json
from pathlib import Path

import pytest

from megamind.registry import (
    ROUTER_HEADER,
    Budgets,
    Registry,
    RegistryError,
    WikiEntry,
    generate_router,
    load_registry,
    save_registry,
    validate_registry,
)


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    registry = Registry(wikis=[WikiEntry(name="DemoWiki", path="DemoWiki", keywords=["demo"])])
    save_registry(tmp_path, registry)
    loaded = load_registry(tmp_path)
    assert loaded.wikis[0].name == "DemoWiki"
    assert loaded.budgets.max_candidates == Budgets().max_candidates


def test_missing_registry(tmp_path: Path) -> None:
    with pytest.raises(RegistryError, match="megamind-axi init"):
        load_registry(tmp_path)


def test_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / ".megamind"
    path.mkdir()
    (path / "registry.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match="not valid JSON"):
        load_registry(tmp_path)


def test_absolute_paths_rejected() -> None:
    registry = Registry(wikis=[WikiEntry(name="X", path="/absolute/path")])
    with pytest.raises(RegistryError, match="root-relative"):
        validate_registry(registry)


def test_traversal_in_registry_rejected() -> None:
    registry = Registry(wikis=[WikiEntry(name="X", path="../escape")])
    with pytest.raises(RegistryError, match=r"\.\."):
        validate_registry(registry)


def test_duplicate_names_rejected() -> None:
    registry = Registry(wikis=[WikiEntry(name="X", path="a"), WikiEntry(name="X", path="b")])
    with pytest.raises(RegistryError, match="duplicate"):
        validate_registry(registry)


def test_invalid_privacy_rejected() -> None:
    registry = Registry(wikis=[WikiEntry(name="X", path="x", privacy="secret")])
    with pytest.raises(RegistryError, match="privacy"):
        validate_registry(registry)


def test_nonpositive_budgets_rejected() -> None:
    registry = Registry(budgets=Budgets(max_candidates=0))
    with pytest.raises(RegistryError, match="positive"):
        validate_registry(registry)


def test_unknown_registry_keys_rejected(tmp_path: Path) -> None:
    path = tmp_path / ".megamind"
    path.mkdir()
    payload = {"version": 1, "wikis": [{"name": "X", "path": "x", "surprise": True}]}
    (path / "registry.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RegistryError, match="unknown field"):
        load_registry(tmp_path)


def _write_registry(root: Path, payload: object) -> None:
    directory = root / ".megamind"
    directory.mkdir(exist_ok=True)
    (directory / "registry.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"version": "one"}, "version must be an integer"),
        ({"version": True}, "version must be an integer"),
        ({"version": 1, "budgets": {"max_candidates": "five"}}, "budget max_candidates"),
        ({"version": 1, "budgets": []}, "budgets must be a JSON object"),
        ({"version": 1, "wikis": "nope"}, "wikis must be a list"),
        ({"version": 1, "wikis": [3]}, r"wikis\[0\] must be a JSON object"),
        ({"version": 1, "wikis": [{"name": "W", "path": 7}]}, r"wikis\[0\] path must be a string"),
        (
            {"version": 1, "wikis": [{"name": "W", "path": "W", "keywords": "a"}]},
            r"wikis\[0\] keywords must be a list",
        ),
        (
            {"version": 1, "wikis": [{"name": "W", "path": "W", "keywords": [1]}]},
            r"wikis\[0\] keywords\[0\] must be a string",
        ),
        ([], "registry root must be a JSON object"),
    ],
)
def test_mistyped_registry_values_raise_registry_error(
    tmp_path: Path, payload: object, message: str
) -> None:
    _write_registry(tmp_path, payload)
    with pytest.raises(RegistryError, match=message) as excinfo:
        load_registry(tmp_path)
    assert excinfo.value.code == "registry_invalid"
    assert str(tmp_path) not in str(excinfo.value)


def test_router_generation_is_deterministic_and_sorted() -> None:
    registry = Registry(
        wikis=[
            WikiEntry(name="Zeta", path="Zeta", keywords=["z"]),
            WikiEntry(name="Alpha", path="Alpha", keywords=["a"]),
        ]
    )
    router = generate_router(registry)
    assert router == generate_router(registry)
    assert router.startswith(ROUTER_HEADER)
    assert router.index("## Alpha") < router.index("## Zeta")


# --- schema v2 ---------------------------------------------------------------


def _write_json(root: Path, rel: str, payload: object) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


V2_ENTRY = {
    "name": "LegalWiki",
    "path": "LegalWiki",
    "privacy": "company-private",
    "description": "Synthetic legal knowledge.",
    "keywords": ["legal", "entity"],
    "card": "LegalWiki/CARD.md",
    "digest": "",
    "index": "LegalWiki/INDEX.md",
    "purpose": "Answers questions about the synthetic legal setup.",
    "answers": ["entity form", "contract templates"],
    "does_not_answer": ["tax filing advice"],
    "scope_boundaries": "Synthetic company facts only.",
    "owners": ["team-legal@example.invalid"],
    "sensitivity": "company-private",
    "model_access": {"local": "full", "cloud": "none"},
    "routing_mode": "full",
    "source_policy": {
        "summary": "Synthetic statute excerpts only.",
        "allowlist": "LegalWiki/SOURCES.md",
        "allowlist_status": "approved",
    },
    "freshness": {"half_life_days": 90, "last_confirmed": "2026-08-01"},
    "examples": ["What entity form did we choose?"],
    "triggers": ["legal", "gmbh"],
    "negative_triggers": ["personal taxes"],
    "dependencies": ["ProductWiki"],
    "context_budget": {"max_candidates": 3, "max_context_chars": 4000},
    "catalog_visibility": "full",
}


def test_v2_registry_round_trip_with_card_fields(tmp_path: Path) -> None:
    _write_json(tmp_path, ".megamind/registry.json", {"version": 2, "wikis": [V2_ENTRY]})
    registry = load_registry(tmp_path)
    wiki = registry.wikis[0]
    assert registry.version == 2
    assert wiki.purpose.startswith("Answers questions")
    assert wiki.answers == ["entity form", "contract templates"]
    assert wiki.does_not_answer == ["tax filing advice"]
    assert wiki.owners == ["team-legal@example.invalid"]
    assert wiki.sensitivity == "company-private"
    assert wiki.model_access.cloud == "none"
    assert wiki.source_policy.allowlist_status == "approved"
    assert wiki.freshness.half_life_days == 90
    assert wiki.negative_triggers == ["personal taxes"]
    assert wiki.context_budget.max_context_chars == 4000
    save_registry(tmp_path, registry)
    assert load_registry(tmp_path).wikis[0] == wiki


def test_v1_registry_loads_with_safe_defaults(tmp_path: Path) -> None:
    _write_json(
        tmp_path,
        ".megamind/registry.json",
        {"version": 1, "wikis": [{"name": "Old", "path": "Old", "privacy": "personal-local"}]},
    )
    wiki = load_registry(tmp_path).wikis[0]
    assert wiki.purpose == ""
    assert wiki.sensitivity == ""
    assert wiki.model_access.local == "" and wiki.model_access.cloud == ""
    assert wiki.catalog_visibility == ""


def test_v1_registry_rejects_v2_fields_with_migration_guidance(tmp_path: Path) -> None:
    payload = {"version": 1, "wikis": [{"name": "Old", "path": "Old", "purpose": "oops"}]}
    _write_json(tmp_path, ".megamind/registry.json", payload)
    with pytest.raises(RegistryError, match="megamind-axi migrate"):
        load_registry(tmp_path)


def test_v2_registry_still_rejects_unknown_fields(tmp_path: Path) -> None:
    payload = {"version": 2, "wikis": [{"name": "W", "path": "W", "surprise": 1}]}
    _write_json(tmp_path, ".megamind/registry.json", payload)
    with pytest.raises(RegistryError, match="unknown field"):
        load_registry(tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sensitivity", "top-secret", "invalid sensitivity"),
        ("routing_mode", "sideways", "invalid routing_mode"),
        ("catalog_visibility", "stealth", "invalid catalog_visibility"),
        ("model_access", {"cloud": "everything"}, "must be one of full, digest-only, none"),
        ("source_policy", {"allowlist_status": "maybe"}, "allowlist_status"),
        ("freshness", {"half_life_days": -3}, "half_life_days must be positive"),
        ("freshness", {"last_confirmed": "last week"}, "last_confirmed must be an ISO date"),
        ("context_budget", {"max_candidates": 0}, "max_candidates must be positive"),
    ],
)
def test_v2_field_validation(tmp_path: Path, field: str, value: object, message: str) -> None:
    entry = {"name": "W", "path": "W", field: value}
    _write_json(tmp_path, ".megamind/registry.json", {"version": 2, "wikis": [entry]})
    with pytest.raises(RegistryError, match=message):
        load_registry(tmp_path)


def test_migrate_upgrades_v1_and_is_idempotent(tmp_path: Path) -> None:
    _write_json(
        tmp_path,
        ".megamind/registry.json",
        {
            "version": 1,
            "wikis": [
                {"name": "Pub", "path": "Pub", "privacy": "public-reference"},
                {"name": "Corp", "path": "Corp", "privacy": "company-private"},
                {"name": "Diary", "path": "Diary", "privacy": "personal-local"},
                {"name": "Box", "path": "Box", "privacy": "pointer-only"},
            ],
        },
    )
    from megamind.registry import migrate_registry

    registry, changed, notes = migrate_registry(tmp_path)
    assert changed
    by_name = {wiki.name: wiki for wiki in registry.wikis}
    assert registry.version == 2
    assert by_name["Pub"].model_access.cloud == "full"
    assert by_name["Corp"].model_access.cloud == ""  # explicit owner policy still required
    assert by_name["Corp"].sensitivity == "company-private"
    assert any("Corp" in note for note in notes)
    assert by_name["Diary"].model_access.cloud == "digest-only"
    assert by_name["Box"].model_access.local == "none"
    assert by_name["Box"].routing_mode == "pointer"

    loaded = load_registry(tmp_path)
    assert loaded.version == 2
    again, changed_again, _ = migrate_registry(tmp_path)
    assert not changed_again
    assert again.version == 2
