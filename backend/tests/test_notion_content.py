"""Notion block-tree flattening (pure functions, no I/O). This parser is the
largest piece of content-mapping logic among the connectors, so it gets direct
unit tests against hand-built block trees — the fetch layer just assembles the
same shape from the API."""

from app.domains.connectors.notion.content import (
    block_to_text, page_title, rich_text_to_plain, tree_to_text,
)


def rt(*texts):
    return [{"plain_text": t} for t in texts]


def block(kind, payload=None, children=None, has_children=False):
    b = {"type": kind, kind: payload or {}, "has_children": has_children}
    if children is not None:
        b["__children"] = children
    return b


# ---- rich text ----

def test_rich_text_concatenates_segments():
    assert rich_text_to_plain(rt("Hello ", "world")) == "Hello world"


def test_rich_text_tolerates_junk():
    assert rich_text_to_plain(None) == ""
    assert rich_text_to_plain("nope") == ""
    assert rich_text_to_plain([None, {"plain_text": "ok"}]) == "ok"


# ---- single blocks ----

def test_paragraph_and_headings():
    assert block_to_text(block("paragraph", {"rich_text": rt("plain")})) == "plain"
    assert block_to_text(block("heading_1", {"rich_text": rt("Title")})) == "# Title"
    assert block_to_text(block("heading_2", {"rich_text": rt("Sub")})) == "## Sub"
    assert block_to_text(block("heading_3", {"rich_text": rt("Deep")})) == "### Deep"


def test_list_items_and_todos():
    assert block_to_text(block("bulleted_list_item", {"rich_text": rt("a")})) == "- a"
    assert block_to_text(block("numbered_list_item", {"rich_text": rt("b")})) == "- b"
    assert block_to_text(block("to_do", {"rich_text": rt("ship it"), "checked": True})) == "[x] ship it"
    assert block_to_text(block("to_do", {"rich_text": rt("later"), "checked": False})) == "[ ] later"


def test_quote_callout_code_equation():
    assert block_to_text(block("quote", {"rich_text": rt("wisdom")})) == "> wisdom"
    assert block_to_text(block("callout", {"rich_text": rt("note")})) == "note"
    assert block_to_text(block("code", {"rich_text": rt("x = 1"), "language": "python"})) == "```python\nx = 1\n```"
    assert block_to_text(block("equation", {"expression": "e=mc^2"})) == "e=mc^2"


def test_bookmark_and_table_row():
    assert block_to_text(
        block("bookmark", {"caption": rt("Docs"), "url": "https://x.co"})
    ) == "Docs https://x.co"
    assert block_to_text(
        block("table_row", {"cells": [rt("a"), rt("b"), rt("c")]})
    ) == "a | b | c"


def test_proseless_blocks_yield_nothing():
    assert block_to_text(block("divider")) == ""
    assert block_to_text(block("image", {"file": {"url": "x"}})) == ""
    assert block_to_text(block("child_page", {"title": "Sub-page"})) == ""
    assert block_to_text(block("unsupported")) == ""
    assert block_to_text(block("paragraph", {"rich_text": []})) == ""


# ---- trees ----

def test_tree_flattens_in_order_with_indented_children():
    tree = [
        block("heading_1", {"rich_text": rt("Decision")}),
        block("paragraph", {"rich_text": rt("We chose Postgres.")}),
        block("bulleted_list_item", {"rich_text": rt("reason one")}, children=[
            block("bulleted_list_item", {"rich_text": rt("sub-reason")}),
        ]),
    ]
    assert tree_to_text(tree) == (
        "# Decision\n"
        "We chose Postgres.\n"
        "- reason one\n"
        "  - sub-reason"
    )


def test_tree_skips_children_of_child_pages():
    tree = [
        block("paragraph", {"rich_text": rt("intro")}),
        block("child_page", {"title": "Own doc"}, children=[
            block("paragraph", {"rich_text": rt("must not leak into parent")}),
        ]),
    ]
    assert tree_to_text(tree) == "intro"


def test_tree_descends_through_proseless_containers():
    # column_list/column emit no text themselves but their children must surface
    tree = [
        block("column_list", None, children=[
            block("column", None, children=[
                block("paragraph", {"rich_text": rt("inside a column")}),
            ]),
        ]),
    ]
    assert "inside a column" in tree_to_text(tree)


def test_table_flattens_row_by_row():
    tree = [
        block("table", None, children=[
            block("table_row", {"cells": [rt("h1"), rt("h2")]}),
            block("table_row", {"cells": [rt("v1"), rt("v2")]}),
        ]),
    ]
    assert tree_to_text(tree) == "  h1 | h2\n  v1 | v2"


def test_empty_tree():
    assert tree_to_text([]) == ""
    assert tree_to_text(None) == ""


# ---- page titles ----

def test_page_title_from_title_property():
    page = {"properties": {
        "Name": {"type": "title", "title": rt("Q3 plan")},
        "Status": {"type": "select"},
    }}
    assert page_title(page) == "Q3 plan"


def test_page_title_database_top_level():
    assert page_title({"title": rt("Projects DB"), "properties": {}}) == "Projects DB"


def test_page_title_fallback():
    assert page_title({}) == "Untitled"
