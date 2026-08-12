"""The standalone wiki card: ``<wiki-root>/.megamind/wiki-card.json``.

A canonical wiki root (the Karpathy layout: ``AGENTS.md``, immutable ``raw/``,
AI-maintained ``wiki/``, ``.megamind/`` state) carries exactly one
authoritative card instead of a multi-wiki registry. The card is the same v2
field set as a registry entry, with the wiki path fixed to the root itself.
Catalog and preflight read cards read-only; nothing in this module mutates.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from .fsops import MEGAMIND_DIR, atomic_write, resolve_contained
from .registry import (
    CURRENT_VERSION,
    RegistryError,
    WikiEntry,
    _validate_wiki_entry,
    _wiki_from_json,
    _wiki_to_data,
)

CARD_PATH = Path(MEGAMIND_DIR) / "wiki-card.json"
CARD_SCHEMA = "megamind/wiki-card/v2"
CANONICAL_RAW_DIR = "raw"
CANONICAL_COMPILED_DIR = "wiki"
# Never compiled pages under a canonical root, whatever its card declares: the
# immutable source layer, the control directory, and the agent instructions.
NON_PAGE_ENTRIES = (CANONICAL_RAW_DIR, MEGAMIND_DIR, "AGENTS.md")


def compiled_page_dir(entry: WikiEntry) -> str:
    """The directory that holds a wiki entry's compiled pages.

    A canonical card is rooted at "." and declares where its pages live through
    its index: a scaffolded root keeps them under ``wiki/``, while a root
    adopted around a legacy ``README.md`` hub keeps them at the root itself.
    Deriving the directory from the index is the rule ``routing`` already
    applies to index entries, so a surface that walks a wiki as a page tree
    reaches exactly the pages ``route`` can offer.
    """
    if entry.path != ".":
        return entry.path
    if entry.index:
        return PurePosixPath(entry.index).parent.as_posix()
    return CANONICAL_COMPILED_DIR


def is_canonical_page(page_rel: str) -> bool:
    """Whether a root-relative path under a canonical root is a compiled page.

    Only a card whose compiled directory is the root itself can reach the raw
    layer or the control state, so this exclusion belongs beside the directory
    derivation rather than in each caller.
    """
    parts = PurePosixPath(page_rel).parts
    return bool(parts) and parts[0] not in NON_PAGE_ENTRIES


class CardError(ValueError):
    """The wiki card is missing, unreadable, or violates the schema."""

    code = "card_invalid"


def card_file(root: Path) -> Path:
    return resolve_contained(root, CARD_PATH)


def load_wiki_card(root: Path) -> WikiEntry:
    """Load the canonical card as a WikiEntry rooted at ".". Raises CardError."""
    path = card_file(root)
    if not path.is_file():
        raise CardError(f"no wiki card at {CARD_PATH.as_posix()}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CardError(f"wiki card is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise CardError("wiki card root must be a JSON object")
    schema = raw.get("schema")
    if schema != CARD_SCHEMA:
        raise CardError(f"wiki card schema must be {CARD_SCHEMA}, got: {schema!r}")
    version = raw.get("version", 0)
    if version != CURRENT_VERSION:
        raise CardError(f"wiki card version must be {CURRENT_VERSION}, got: {version!r}")
    data = {key: value for key, value in raw.items() if key not in ("schema", "version", "path")}
    try:
        entry = _wiki_from_json(0, data, CURRENT_VERSION)
    except RegistryError as error:
        raise CardError(str(error)) from error
    if "privacy" not in data:
        # A card without a privacy class stays unclassified: locally readable,
        # cloud-restrictive. It must not inherit the registry's public default.
        entry.privacy = ""
    entry.path = "."
    seen: set[str] = set()
    try:
        _validate_wiki_entry(0, entry, CURRENT_VERSION, seen, allow_unset_privacy=True)
    except RegistryError as error:
        raise CardError(str(error)) from error
    return entry


def card_to_data(entry: WikiEntry) -> dict[str, object]:
    data = _wiki_to_data(entry, CURRENT_VERSION)
    data.pop("path", None)
    if not entry.privacy:
        data["privacy"] = ""  # keep the unclassified posture explicit on disk
    return {"schema": CARD_SCHEMA, "version": CURRENT_VERSION, **data}


def serialize_wiki_card(entry: WikiEntry) -> str:
    return json.dumps(card_to_data(entry), indent=2, sort_keys=True) + "\n"


def save_wiki_card(root: Path, entry: WikiEntry) -> Path:
    return atomic_write(root, CARD_PATH, serialize_wiki_card(entry))
