"""Capture: turn raw text into an auditable proposal draft.

Capture never publishes. It writes a proposal file under ``.megamind/proposals/``
with provenance, a lifecycle status of ``proposed``, and a suggested destination
from the deterministic router. Publishing happens later, through ``evolve``,
after human review.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .fsops import MEGAMIND_DIR, append_audit, atomic_write, content_hash, resolve_contained
from .models import KNOWLEDGE_TYPES, Document, parse_document
from .registry import Registry
from .routing import route

PROPOSALS_DIR = Path(MEGAMIND_DIR) / "proposals"


class CaptureError(ValueError):
    """The capture input is unusable (empty content, unknown knowledge type)."""

    code = "capture_invalid"


@dataclass
class CaptureResult:
    created: bool
    proposal_id: str
    path: str
    suggested_destination: str
    route_reasons: list[str]


def normalize_content(text: str) -> str:
    """Whitespace-normalized content used for hashing and duplicate detection."""
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return "\n".join(lines)


def proposal_path(root: Path, proposal_id: str) -> Path:
    return resolve_contained(root, PROPOSALS_DIR / f"{proposal_id}.md")


def find_existing(root: Path, proposal_id: str) -> Path | None:
    path = proposal_path(root, proposal_id)
    return path if path.is_file() else None


def list_proposals(root: Path) -> list[Path]:
    directory = resolve_contained(root, PROPOSALS_DIR)
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.md"))


def capture(
    root: Path,
    registry: Registry,
    text: str,
    source: str,
    knowledge_type: str = "fact",
    today: date | None = None,
) -> CaptureResult:
    """Create a proposal from text. Idempotent: identical content is captured once."""
    if knowledge_type not in KNOWLEDGE_TYPES:
        raise CaptureError(
            f"unknown knowledge type: {knowledge_type} "
            f"(expected one of {', '.join(KNOWLEDGE_TYPES)})"
        )
    content = normalize_content(text)
    if not content:
        raise CaptureError("cannot capture empty content")
    proposal_id = content_hash(content)

    existing = find_existing(root, proposal_id)
    if existing is not None:
        document = parse_document(existing.read_text(encoding="utf-8"))
        destination = str(document.frontmatter.get("suggested_destination", "uncategorized"))
        return CaptureResult(
            created=False,
            proposal_id=proposal_id,
            path=(PROPOSALS_DIR / f"{proposal_id}.md").as_posix(),
            suggested_destination=destination,
            route_reasons=["duplicate: proposal with identical content already exists"],
        )

    routed = route(root, registry, content)
    if routed.candidates:
        best = routed.candidates[0]
        destination = best.path if best.kind == "page" else f"{best.wiki}/"
        reasons = best.reasons[:5]
    else:
        destination = "uncategorized"
        reasons = ["no wiki matched: needs manual categorization"]

    captured_on = (today or date.today()).isoformat()
    document = Document(
        frontmatter={
            "megamind": "proposal",
            "id": proposal_id,
            "type": knowledge_type,
            "status": "proposed",
            "source": source,
            "captured": captured_on,
            "suggested_destination": destination,
            "route_reasons": reasons,
        },
        body=f"\n{content}\n",
    )
    rel_path = PROPOSALS_DIR / f"{proposal_id}.md"
    atomic_write(root, rel_path, document.render())
    append_audit(
        root,
        "capture",
        {
            "proposal_id": proposal_id,
            "path": rel_path.as_posix(),
            "source": source,
            "type": knowledge_type,
            "suggested_destination": destination,
        },
    )
    return CaptureResult(
        created=True,
        proposal_id=proposal_id,
        path=rel_path.as_posix(),
        suggested_destination=destination,
        route_reasons=reasons,
    )
