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
    with pytest.raises(RegistryError, match="schema"):
        load_registry(tmp_path)


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
