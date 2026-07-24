"""Init: non-destructive vault initialization with a synthetic starter structure.

Init never overwrites anything. Existing files are skipped and reported; an
already-initialized vault is left untouched.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .fsops import MEGAMIND_DIR, append_audit, atomic_write
from .registry import (
    ROUTER_FILENAME,
    ROUTER_HEADER,
    Budgets,
    Registry,
    WikiEntry,
    generate_router,
    load_registry,
    registry_file,
    save_registry,
)

STARTER_WIKI = "StarterWiki"

STARTER_CARD = """---
megamind: routing-card
wiki: StarterWiki
privacy: public-reference
keywords: [starter, example, getting started, megamind]
---

# StarterWiki routing card

Answers questions about this synthetic starter wiki and how Megamind vaults
are laid out. Replace it with your own wikis as your vault grows.

Answers: what a routing card, digest, and index are; how topic pages look.
Does not answer: anything about your real projects yet.
"""

STARTER_DIGEST = """---
megamind: digest
wiki: StarterWiki
---

# StarterWiki digest

One synthetic wiki demonstrating the Megamind ladder: routing card, digest,
index, topic pages. The only topic so far explains how to grow a vault from
captured proposals into confirmed knowledge.
"""

STARTER_INDEX = """---
megamind: index
wiki: StarterWiki
---

# StarterWiki index

- [Getting started](topics/getting-started.md) - how a vault grows from proposals
"""

STARTER_TOPIC = """---
title: Getting started
type: guidance
status: active
created: 2026-01-01
updated: 2026-01-01
provenance:
  - synthetic starter content
---

# Getting started

Capture turns notes into proposals under `.megamind/proposals/`. Review lists
what needs attention. Evolve merges an approved proposal into a topic page,
or supersedes an outdated one. Doctor validates the vault.

Nothing becomes permanent wiki content without an explicit approval step.
"""


@dataclass
class InitResult:
    root: str
    already_initialized: bool
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _write_if_missing(root: Path, rel: str, content: str, result: InitResult) -> None:
    target = root.resolve() / rel
    if target.exists():
        result.skipped.append(rel)
        return
    atomic_write(root, rel, content)
    result.created.append(rel)


def init_vault(root: Path, starter: bool = True) -> InitResult:
    """Initialize a vault at root. Safe to re-run; never overwrites content."""
    root.mkdir(parents=True, exist_ok=True)
    result = InitResult(root=root.name or ".", already_initialized=False)
    if registry_file(root).is_file():
        result.already_initialized = True
        _refresh_router(root, result)
        return result

    wikis: list[WikiEntry] = []
    if starter:
        wikis.append(
            WikiEntry(
                name=STARTER_WIKI,
                path=STARTER_WIKI,
                privacy="public-reference",
                description="Synthetic starter wiki showing the Megamind vault layout.",
                keywords=["starter", "example", "getting started"],
                card=f"{STARTER_WIKI}/CARD.md",
                digest=f"{STARTER_WIKI}/DIGEST.md",
                index=f"{STARTER_WIKI}/INDEX.md",
            )
        )
    registry = Registry(version=1, budgets=Budgets(), wikis=wikis)
    save_registry(root, registry)
    result.created.append(f"{MEGAMIND_DIR}/registry.json")

    (root.resolve() / MEGAMIND_DIR / "proposals").mkdir(parents=True, exist_ok=True)
    (root.resolve() / MEGAMIND_DIR / "audit").mkdir(parents=True, exist_ok=True)

    _write_if_missing(root, ROUTER_FILENAME, generate_router(registry), result)
    if starter:
        _write_if_missing(root, f"{STARTER_WIKI}/CARD.md", STARTER_CARD, result)
        _write_if_missing(root, f"{STARTER_WIKI}/DIGEST.md", STARTER_DIGEST, result)
        _write_if_missing(root, f"{STARTER_WIKI}/INDEX.md", STARTER_INDEX, result)
        _write_if_missing(root, f"{STARTER_WIKI}/topics/getting-started.md", STARTER_TOPIC, result)

    append_audit(root, "init", {"created": list(result.created), "skipped": list(result.skipped)})
    return result


def _refresh_router(root: Path, result: InitResult) -> None:
    """Regenerate ROUTER.md on re-init when the registry changed.

    Only files carrying the generated-file header are ever rewritten; a
    hand-edited router is left alone (doctor will flag the missing header).
    """
    registry = load_registry(root)
    expected = generate_router(registry)
    router_path = root.resolve() / ROUTER_FILENAME
    if not router_path.is_file():
        atomic_write(root, ROUTER_FILENAME, expected)
        result.created.append(ROUTER_FILENAME)
        append_audit(root, "router-refresh", {"path": ROUTER_FILENAME, "reason": "missing"})
        return
    actual = router_path.read_text(encoding="utf-8")
    if actual == expected:
        return
    if ROUTER_HEADER not in actual:
        result.skipped.append(f"{ROUTER_FILENAME} (hand-edited, not touched)")
        return
    atomic_write(root, ROUTER_FILENAME, expected)
    result.created.append(f"{ROUTER_FILENAME} (refreshed)")
    append_audit(root, "router-refresh", {"path": ROUTER_FILENAME, "reason": "out-of-sync"})
