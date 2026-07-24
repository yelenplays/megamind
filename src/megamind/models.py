"""Core data model: knowledge types, lifecycle states, privacy classes, frontmatter.

Frontmatter uses a deliberately small, deterministic YAML subset:
scalar values, flow lists ("[a, b]"), and block lists of scalars.
That keeps the core dependency-free and round-trip stable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FrontmatterValue = str | int | bool | list[str]
Frontmatter = dict[str, FrontmatterValue]

KNOWLEDGE_TYPES: tuple[str, ...] = (
    "fact",
    "decision",
    "hypothesis",
    "procedure",
    "example",
    "guidance",
)

LIFECYCLE_STATUSES: tuple[str, ...] = (
    "proposed",
    "confirmed",
    "active",
    "shaky",
    "rejected",
    "superseded",
)

PRIVACY_CLASSES: tuple[str, ...] = (
    "public-reference",
    "company-private",
    "personal-local",
    "digest-only",
    "pointer-only",
)

# Privacy classes whose page bodies may be quoted into routed context.
# digest-only exposes only the digest; pointer-only exposes only paths.
CONTENT_VISIBLE_PRIVACY: tuple[str, ...] = ("public-reference", "company-private", "personal-local")

FRONTMATTER_DELIMITER = "---"


class FrontmatterError(ValueError):
    """Raised when a frontmatter block cannot be parsed with the supported subset."""


@dataclass
class Document:
    """A Markdown document split into frontmatter and body."""

    frontmatter: Frontmatter = field(default_factory=dict)
    body: str = ""

    def render(self) -> str:
        if not self.frontmatter:
            return self.body
        return (
            f"{FRONTMATTER_DELIMITER}\n"
            f"{serialize_frontmatter(self.frontmatter)}"
            f"{FRONTMATTER_DELIMITER}\n"
            f"{self.body}"
        )


def _parse_scalar(raw: str) -> str | int | bool:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        return text[1:-1]
    if text == "true":
        return True
    if text == "false":
        return False
    if text.lstrip("-").isdigit() and text not in {"", "-"}:
        return int(text)
    return text


def _parse_flow_list(raw: str) -> list[str]:
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    return [str(_parse_scalar(part)) for part in inner.split(",")]


def parse_frontmatter(text: str) -> Frontmatter:
    """Parse the supported YAML subset into a dict. Raises FrontmatterError on misuse."""
    result: Frontmatter = {}
    pending_list_key: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            if pending_list_key is None:
                raise FrontmatterError(f"line {line_number}: list item without a key")
            current = result[pending_list_key]
            assert isinstance(current, list)
            current.append(str(_parse_scalar(stripped[2:])))
            continue
        pending_list_key = None
        if ":" not in stripped:
            raise FrontmatterError(f"line {line_number}: expected 'key: value'")
        key, _, raw_value = stripped.partition(":")
        key = key.strip()
        if not key:
            raise FrontmatterError(f"line {line_number}: empty key")
        value = raw_value.strip()
        if value == "":
            result[key] = []
            pending_list_key = key
        elif value.startswith("[") and value.endswith("]"):
            result[key] = _parse_flow_list(value)
        else:
            result[key] = _parse_scalar(value)
    return result


def serialize_frontmatter(data: Frontmatter) -> str:
    """Serialize a frontmatter dict deterministically (insertion order preserved)."""
    lines: list[str] = []
    for key, value in data.items():
        if isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {item}" for item in value)
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        else:
            lines.append(f"{key}: {value}")
    return "".join(f"{line}\n" for line in lines)


def parse_document(text: str) -> Document:
    """Split a Markdown file into frontmatter and body.

    A document without a leading frontmatter fence is returned with empty frontmatter.
    """
    if not text.startswith(f"{FRONTMATTER_DELIMITER}\n"):
        return Document(frontmatter={}, body=text)
    rest = text[len(FRONTMATTER_DELIMITER) + 1 :]
    closing = rest.find(f"\n{FRONTMATTER_DELIMITER}\n")
    if closing == -1:
        if rest.rstrip("\n").endswith(FRONTMATTER_DELIMITER) and rest.count("\n") >= 1:
            block = rest.rstrip("\n")[: -len(FRONTMATTER_DELIMITER)]
            return Document(frontmatter=parse_frontmatter(block), body="")
        raise FrontmatterError("unterminated frontmatter block")
    block = rest[:closing]
    body = rest[closing + len(FRONTMATTER_DELIMITER) + 2 :]
    return Document(frontmatter=parse_frontmatter(block), body=body)


def as_string_list(value: FrontmatterValue | None) -> list[str]:
    """Coerce a frontmatter value to a list of strings (missing -> empty)."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]
