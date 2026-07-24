from __future__ import annotations

from megamind.toon import encode


def test_scalars_and_quoting() -> None:
    doc = {
        "plain": "hello world",
        "colon": "a: b",
        "comma": "a, b",
        "number": 42,
        "boolish": "true",
        "real_bool": True,
        "none": None,
        "numeric_string": "3.14",
        "empty": "",
    }
    assert encode(doc) == (
        "plain: hello world\n"
        'colon: "a: b"\n'
        'comma: "a, b"\n'
        "number: 42\n"
        'boolish: "true"\n'
        "real_bool: true\n"
        "none: \n"
        'numeric_string: "3.14"\n'
        'empty: ""\n'
    )


def test_uniform_table() -> None:
    doc = {
        "items": [
            {"path": "a.md", "kind": "page", "score": 7},
            {"path": "b.md", "kind": "digest", "score": 2},
        ]
    }
    assert encode(doc) == ("items[2]{path,kind,score}:\n  a.md,page,7\n  b.md,digest,2\n")


def test_table_cells_quote_commas_and_empties() -> None:
    doc = {"rows": [{"a": "x, y", "b": None}]}
    assert encode(doc) == 'rows[1]{a,b}:\n  "x, y",\n'


def test_scalar_list_and_empty_list() -> None:
    doc = {"help": ["Run `x` now", "Second step"], "none_found": []}
    assert encode(doc) == ("help[2]:\n  Run `x` now\n  Second step\nnone_found[0]:\n")


def test_nested_object() -> None:
    doc = {"budgets": {"max": 5, "inner": {"deep": "v"}}}
    assert encode(doc) == "budgets:\n  max: 5\n  inner:\n    deep: v\n"


def test_non_uniform_list_falls_back_to_blocks() -> None:
    doc = {"mixed": [{"a": 1}, {"b": 2}]}
    out = encode(doc)
    assert out.startswith("mixed[2]:\n")
    assert "a: 1" in out and "b: 2" in out


def test_newline_and_quote_escaping() -> None:
    doc = {"text": 'line one\nwith "quotes"'}
    assert encode(doc) == 'text: "line one\\nwith \\"quotes\\""\n'


def test_deterministic() -> None:
    doc = {"a": 1, "b": [{"x": "y"}], "c": {"d": True}}
    assert encode(doc) == encode(doc)


def test_backslash_values_are_quoted_and_escaped() -> None:
    out = encode({"v": "a\\b"})
    assert out == 'v: "a\\\\b"\n'


def test_plain_values_are_never_escaped_in_place() -> None:
    assert encode({"v": "plain value"}) == "v: plain value\n"
