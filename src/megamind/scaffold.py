"""Init: non-destructive vault initialization with a synthetic starter structure.

Init never overwrites anything. Existing files are skipped and reported; an
already-initialized vault is left untouched.

Init also scaffolds canonical single-wiki roots (``init <path> --wiki Name``):
the Karpathy layout of ``AGENTS.md``, an immutable human-curated ``raw/``
layer, the AI-maintained compiled ``wiki/`` layer, and ``.megamind/`` state
headed by the authoritative ``wiki-card.json``.

A directory is one root shape or the other, never both: a registry vault and a
canonical wiki root disagree about which card is authoritative, and discovery
would have to guess. Init therefore refuses to add the second shape to a root
that already carries the first, exactly as adopt does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .card import card_file, serialize_wiki_card
from .fsops import MEGAMIND_DIR, append_audit, atomic_write, backup_existing
from .registry import (
    CURRENT_VERSION,
    ROUTER_FILENAME,
    ROUTER_HEADER,
    Budgets,
    ModelAccess,
    Registry,
    WikiEntry,
    generate_router,
    load_registry,
    registry_file,
    save_registry,
)

STARTER_WIKI = "StarterWiki"


class InitError(ValueError):
    """The initialization request would produce an ambiguous or unsafe root."""

    code = "init_invalid"


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


def canonical_agents_md(name: str) -> str:
    """The domain schema and workflow contract of a canonical wiki root."""
    return f"""# {name}

A Megamind canonical wiki root.

## Layout

- `raw/` - human-curated source documents. Immutable: Megamind reads and
  cites sources but never modifies, moves, renames, or deletes anything here.
- `raw/assets/` - optional source images and files; same immutability.
- `wiki/` - the AI-maintained compiled knowledge layer: interlinked topic,
  entity, and synthesis pages built from the raw sources.
- `wiki/index.md` - the complete content-oriented catalog of compiled pages.
- `wiki/log.md` - the structured append-only event history.
- `.megamind/wiki-card.json` - the authoritative routing, policy, and
  ownership card for this wiki.
- `.megamind/proposals/` - knowledge proposals awaiting approval.
- `.megamind/gaps.jsonl` - durable deduplicated knowledge-gap records.
- `.megamind/audit/` - mutation records, backups, and rollback material.

## Workflows

- Sources enter `raw/` by human decision only.
- Compiled knowledge changes are proposed first and applied only through the
  `megamind-axi` approval flow; nothing is published automatically.
- Every substantive event (query, ingest, mutation, validation, rollback)
  appends a structured entry to `wiki/log.md` with a privacy-safe summary.
"""


def canonical_index_md(name: str) -> str:
    return f"""# {name} index

The complete content-oriented catalog of this wiki's compiled pages.

<!-- One Markdown link per compiled page with a short hint. -->
"""


def canonical_log_md(name: str) -> str:
    return f"""# {name} log

Structured append-only event history. Newest events go at the end.

Each event is a block with stable fields so simple tools can parse it:

    ## YYYY-MM-DD <event-type>
    - summary: <privacy-safe one-line summary>
    - pages: [<affected compiled pages>]
    - sources: [<eligible sources used>]
    - confidence: <high | offer | low | unknown>
    - outcome: <what happened>
    - audit: <audit or rollback reference>

The log never records credentials, secrets, or verbatim sensitive prompts.
"""


def canonical_card_entry(name: str) -> WikiEntry:
    """The restrictive-default authoritative card for a brand-new wiki root."""
    return WikiEntry(
        name=name,
        path=".",
        privacy="",
        description="",
        index="wiki/index.md",
    )


def init_wiki_root(root: Path, name: str) -> InitResult:
    """Scaffold a canonical single-wiki root. Safe to re-run; never overwrites."""
    if registry_file(root).is_file():
        raise InitError(
            "target already has a Megamind registry; it is a registry vault, not a "
            "canonical wiki root. Run init without --wiki, or add the wiki to the "
            "registry instead"
        )
    root.mkdir(parents=True, exist_ok=True)
    result = InitResult(root=root.name or ".", already_initialized=False)
    if card_file(root).is_file():
        result.already_initialized = True
        return result

    directories = [
        "raw/",
        f"{MEGAMIND_DIR}/proposals/",
        f"{MEGAMIND_DIR}/audit/",
    ]
    for rel in directories:
        target = root.resolve() / rel
        if target.exists():
            result.skipped.append(rel)
            continue
        target.mkdir(parents=True, exist_ok=True)
        result.created.append(rel)

    files = {
        "AGENTS.md": canonical_agents_md(name),
        "wiki/index.md": canonical_index_md(name),
        "wiki/log.md": canonical_log_md(name),
        f"{MEGAMIND_DIR}/wiki-card.json": serialize_wiki_card(canonical_card_entry(name)),
        f"{MEGAMIND_DIR}/gaps.jsonl": "",
    }
    for rel, content in files.items():
        _write_if_missing(root, rel, content, result)

    append_audit(root, "init", {"layout": "canonical-wiki", "wiki": name})
    return result


def _write_if_missing(root: Path, rel: str, content: str, result: InitResult) -> None:
    target = root.resolve() / rel
    if target.exists():
        result.skipped.append(rel)
        return
    atomic_write(root, rel, content)
    result.created.append(rel)


def init_vault(root: Path, starter: bool = True) -> InitResult:
    """Initialize a vault at root. Safe to re-run; never overwrites content."""
    if card_file(root).is_file() and not registry_file(root).is_file():
        raise InitError(
            "target already has a canonical wiki card; it is a single-wiki root, not "
            "a registry vault. Keep using the card, or initialize the vault in a "
            "separate directory"
        )
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
                sensitivity="public-reference",
                model_access=ModelAccess(local="full", cloud="full"),
            )
        )
    registry = Registry(version=CURRENT_VERSION, budgets=Budgets(), wikis=wikis)
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
    backup = backup_existing(root, ROUTER_FILENAME)
    atomic_write(root, ROUTER_FILENAME, expected)
    result.created.append(f"{ROUTER_FILENAME} (refreshed)")
    append_audit(
        root,
        "router-refresh",
        {
            "path": ROUTER_FILENAME,
            "reason": "out-of-sync",
            "backup": backup.name if backup else None,
        },
    )
