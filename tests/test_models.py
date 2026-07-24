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


def test_as_string_list() -> None:
    assert as_string_list(None) == []
    assert as_string_list("one") == ["one"]
    assert as_string_list(["a", "b"]) == ["a", "b"]
