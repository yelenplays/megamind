from __future__ import annotations

import pytest

from megamind.models import (
    Document,
    FrontmatterError,
    as_string_list,
    parse_document,
    parse_frontmatter,
    serialize_frontmatter,
)


def test_parse_scalars_and_lists() -> None:
    data = parse_frontmatter(
        "title: Pricing model\n"
        "count: 3\n"
        "draft: true\n"
        "tags: [alpha, beta]\n"
        "provenance:\n"
        "  - synthetic example\n"
        "  - second source\n"
    )
    assert data["title"] == "Pricing model"
    assert data["count"] == 3
    assert data["draft"] is True
    assert data["tags"] == ["alpha", "beta"]
    assert data["provenance"] == ["synthetic example", "second source"]


def test_parse_quoted_and_empty_list() -> None:
    data = parse_frontmatter('name: "quoted: value"\nempty: []\n')
    assert data["name"] == "quoted: value"
    assert data["empty"] == []


def test_parse_errors() -> None:
    with pytest.raises(FrontmatterError):
        parse_frontmatter("- item without key\n")
    with pytest.raises(FrontmatterError):
        parse_frontmatter("no delimiter here\n")


def test_round_trip() -> None:
    original = {
        "title": "Feature flags",
        "type": "procedure",
        "tags": ["a", "b"],
        "active": True,
    }
    rendered = serialize_frontmatter(original)  # type: ignore[arg-type]
    assert parse_frontmatter(rendered) == original


def test_parse_document_with_and_without_frontmatter() -> None:
    doc = parse_document("---\ntitle: X\n---\nbody text\n")
    assert doc.frontmatter == {"title": "X"}
    assert doc.body == "body text\n"
    plain = parse_document("just text\n")
    assert plain.frontmatter == {}
    assert plain.body == "just text\n"


def test_document_render_round_trip() -> None:
    doc = Document(frontmatter={"title": "X", "tags": ["a"]}, body="\n# X\n")
    assert parse_document(doc.render()).frontmatter == doc.frontmatter
    assert parse_document(doc.render()).body == doc.body


def test_unterminated_frontmatter() -> None:
    with pytest.raises(FrontmatterError):
        parse_document("---\ntitle: X\nno closing fence\n")


def test_unsafe_scalars_round_trip_without_injecting_keys() -> None:
    hostile = {
        "source": "note\nstatus: applied\napplied_to: Wiki/page.md",
        "status": "proposed",
        "empty": "",
        "padded": "  spaced  ",
        "numeric_string": "42",
        "boolish": "true",
        "flow_like": "[not, a, list]",
        "escaped": 'back\\slash and "quotes"',
    }
    rendered = serialize_frontmatter(hostile)  # type: ignore[arg-type]
    parsed = parse_frontmatter(rendered)
    assert parsed == hostile
    assert parsed["status"] == "proposed"
    assert "applied_to" not in parsed


def test_unsafe_list_items_round_trip() -> None:
    original = {"provenance": ["proposal abc", "source: x\nstatus: applied", "- dash", ""]}
    parsed = parse_frontmatter(serialize_frontmatter(original))  # type: ignore[arg-type]
    assert parsed == original


def test_unsupported_key_is_rejected() -> None:
    with pytest.raises(FrontmatterError):
        serialize_frontmatter({"bad\nkey": "value"})


def test_as_string_list() -> None:
    assert as_string_list(None) == []
    assert as_string_list("one") == ["one"]
    assert as_string_list(["a", "b"]) == ["a", "b"]


def test_unicode_digit_forms_stay_strings() -> None:
    data = parse_frontmatter("qty: ²\nreal: 12\nnegative: -3\n")
    assert data["qty"] == "²"
    assert data["real"] == 12
    assert data["negative"] == -3
    assert parse_frontmatter(serialize_frontmatter(data)) == data


def test_single_quoted_scalars_are_literal() -> None:
    data = parse_frontmatter("path: 'C:\\temp\\new'\nname: 'it''s here'\n")
    assert data["path"] == "C:\\temp\\new"
    assert data["name"] == "it's here"


def test_double_quoted_scalars_are_unescaped() -> None:
    data = parse_frontmatter('path: "C:\\\\temp"\nmulti: "a\\nb"\n')
    assert data["path"] == "C:\\temp"
    assert data["multi"] == "a\nb"
