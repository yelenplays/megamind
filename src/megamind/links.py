"""Link extraction and resolution for Markdown and Obsidian-style wikilinks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_MARKDOWN_LINK = re.compile(r"(?<!!)\[(?P<label>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_WIKILINK = re.compile(r"\[\[(?P<target>[^\]|#]+)(?:#[^\]|]*)?(?:\|(?P<label>[^\]]*))?\]\]")
_EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)


@dataclass
class Link:
    target: str
    label: str
    style: str  # "markdown" or "wikilink"


def extract_links(body: str) -> list[Link]:
    """Extract internal links in document order. External URLs are skipped."""
    found: list[tuple[int, Link]] = []
    for match in _MARKDOWN_LINK.finditer(body):
        target = match.group("target")
        if _EXTERNAL.match(target) or target.startswith("#"):
            continue
        found.append((match.start(), Link(target, match.group("label"), "markdown")))
    for match in _WIKILINK.finditer(body):
        target = match.group("target").strip()
        if not target:
            continue
        label = match.group("label") or target
        found.append((match.start(), Link(target, label, "wikilink")))
    found.sort(key=lambda item: item[0])
    return [link for _, link in found]


def resolve_link(root: Path, source_file: Path, link: Link, page_names: dict[str, Path]) -> bool:
    """Return True when a link resolves inside the root.

    Markdown links resolve relative to the source file (with URL fragments and
    anchors stripped). Wikilinks resolve by page name anywhere under the root,
    which mirrors Obsidian's "shortest path when possible" default.
    """
    if link.style == "wikilink":
        name = link.target.strip().removesuffix(".md")
        return name in page_names
    target = link.target.split("#", 1)[0]
    if not target:
        return True
    candidate = (source_file.parent / target).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    if candidate.exists():
        return True
    return candidate.suffix == "" and candidate.with_suffix(".md").exists()


def page_name_table(root: Path) -> dict[str, Path]:
    """Map bare page names (no extension) to paths for wikilink resolution.

    Skips the .megamind control directory and hidden directories.
    """
    table: dict[str, Path] = {}
    for path in sorted(root.rglob("*.md")):
        if any(part.startswith(".") for part in path.relative_to(root).parts[:-1]):
            continue
        table.setdefault(path.stem, path)
    return table
