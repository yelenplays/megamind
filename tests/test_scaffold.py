from __future__ import annotations

from pathlib import Path

import pytest

from megamind.card import CARD_PATH
from megamind.registry import ROUTER_FILENAME, WikiEntry, load_registry, save_registry
from megamind.scaffold import InitError, init_vault, init_wiki_root


def test_init_creates_registry_router_and_starter(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    result = init_vault(root)
    assert not result.already_initialized
    assert (root / ".megamind/registry.json").is_file()
    assert (root / ".megamind/proposals").is_dir()
    assert (root / "ROUTER.md").is_file()
    assert (root / "StarterWiki/topics/getting-started.md").is_file()
    registry = load_registry(root)
    assert registry.wikis[0].name == "StarterWiki"


def test_init_never_overwrites_existing_files(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    card = root / "StarterWiki" / "CARD.md"
    card.parent.mkdir(parents=True)
    card.write_text("my own card\n", encoding="utf-8")
    result = init_vault(root)
    assert "StarterWiki/CARD.md" in result.skipped
    assert card.read_text(encoding="utf-8") == "my own card\n"


def test_reinit_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    init_vault(root)
    before = (root / ".megamind/registry.json").read_text(encoding="utf-8")
    result = init_vault(root)
    assert result.already_initialized
    assert (root / ".megamind/registry.json").read_text(encoding="utf-8") == before


def test_reinit_refreshes_generated_router_after_registry_edit(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    init_vault(root)
    registry = load_registry(root)
    (root / "DemoWiki").mkdir()
    registry.wikis.append(WikiEntry(name="DemoWiki", path="DemoWiki", keywords=["demo"]))
    save_registry(root, registry)
    before = (root / ROUTER_FILENAME).read_text(encoding="utf-8")
    result = init_vault(root)
    assert f"{ROUTER_FILENAME} (refreshed)" in result.created
    assert "DemoWiki" in (root / ROUTER_FILENAME).read_text(encoding="utf-8")
    backups = list((root / ".megamind/audit/backups").glob(f"{ROUTER_FILENAME}.*.bak"))
    assert [b.read_text(encoding="utf-8") for b in backups] == [before]


def test_reinit_never_touches_hand_edited_router(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    init_vault(root)
    router = root / ROUTER_FILENAME
    router.write_text("# my own router without the generated header\n", encoding="utf-8")
    result = init_vault(root)
    assert any("hand-edited" in item for item in result.skipped)
    assert router.read_text(encoding="utf-8").startswith("# my own router")


def test_init_no_starter(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    init_vault(root, starter=False)
    registry = load_registry(root)
    assert registry.wikis == []
    assert not (root / "StarterWiki").exists()


def test_init_wiki_refuses_a_registry_vault(tmp_path: Path) -> None:
    """A root is one shape or the other; both cards would leave discovery guessing."""
    root = tmp_path / "vault"
    init_vault(root)
    with pytest.raises(InitError, match="registry vault"):
        init_wiki_root(root, "SoloWiki")
    assert not (root / CARD_PATH).exists()


def test_init_vault_refuses_a_canonical_wiki_root(tmp_path: Path) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    with pytest.raises(InitError, match="canonical wiki card"):
        init_vault(root)
    assert not (root / ".megamind/registry.json").exists()


def test_init_wiki_root_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "SoloWiki"
    init_wiki_root(root, "SoloWiki")
    result = init_wiki_root(root, "SoloWiki")
    assert result.already_initialized
    assert result.created == []
