"""TOON encoder: token-efficient rendering of typed documents.

Megamind builds every result as a plain typed dict; this module renders it as
TOON at the output boundary. JSON output renders the exact same dict, so the
two formats can never drift. The encoder covers the subset Megamind emits:
scalars, nested objects, scalar lists, and uniform tables of scalar rows.
"""

from __future__ import annotations

Scalar = str | int | float | bool | None
JsonValue = Scalar | dict[str, "JsonValue"] | list["JsonValue"]

_QUOTE_IF_CONTAINS = (",", '"', "\n", ":")
_QUOTE_IF_STARTS = ("-", "[", "{", "#")


def _needs_quotes(text: str) -> bool:
    if text == "" or text != text.strip():
        return True
    if any(ch in text for ch in _QUOTE_IF_CONTAINS):
        return True
    if text.startswith(_QUOTE_IF_STARTS):
        return True
    lowered = text.lower()
    if lowered in {"true", "false", "null"}:
        return True
    try:
        float(text)
    except ValueError:
        return False
    return True


def _scalar(value: Scalar) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    if _needs_quotes(value):
        return f'"{escaped}"'
    return escaped


def _cell(value: Scalar) -> str:
    """Render a table cell; empty for None, quoted only when necessary."""
    if value is None:
        return ""
    return _scalar(value)


def _is_scalar(value: JsonValue) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _uniform_table(items: list[JsonValue]) -> list[str] | None:
    """Field names when every item is a dict of scalars with identical keys."""
    if not items:
        return None
    fields: list[str] | None = None
    for item in items:
        if not isinstance(item, dict) or not all(_is_scalar(v) for v in item.values()):
            return None
        keys = list(item.keys())
        if fields is None:
            fields = keys
        elif keys != fields:
            return None
    return fields


def _encode_list(key: str, items: list[JsonValue], indent: int, lines: list[str]) -> None:
    pad = " " * indent
    count = len(items)
    if count == 0:
        lines.append(f"{pad}{key}[0]:")
        return
    fields = _uniform_table(items)
    if fields:
        lines.append(f"{pad}{key}[{count}]{{{','.join(fields)}}}:")
        for item in items:
            assert isinstance(item, dict)
            row = ",".join(_cell(item[field]) for field in fields)  # type: ignore[arg-type]
            lines.append(f"{pad}  {row}")
        return
    if all(_is_scalar(item) for item in items):
        lines.append(f"{pad}{key}[{count}]:")
        for item in items:
            lines.append(f"{pad}  {_scalar(item)}")  # type: ignore[arg-type]
        return
    # Non-uniform structures: one indented block per item.
    lines.append(f"{pad}{key}[{count}]:")
    for index, item in enumerate(items):
        lines.append(f"{pad}  - item {index}:")
        if isinstance(item, dict):
            _encode_dict(item, indent + 4, lines)
        else:
            _encode_list("value", item if isinstance(item, list) else [item], indent + 4, lines)


def _encode_dict(data: dict[str, JsonValue], indent: int, lines: list[str]) -> None:
    pad = " " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{pad}{key}:")
            _encode_dict(value, indent + 2, lines)
        elif isinstance(value, list):
            _encode_list(key, value, indent, lines)
        else:
            lines.append(f"{pad}{key}: {_scalar(value)}")


def encode(document: dict[str, JsonValue]) -> str:
    """Render a typed document as TOON text (no trailing blank lines)."""
    lines: list[str] = []
    _encode_dict(document, 0, lines)
    return "\n".join(lines) + "\n"
