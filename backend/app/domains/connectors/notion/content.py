"""Notion block trees -> plain text.

Pure functions, no I/O: the client fetches block children (paginated at every
level) and injects them under the "__children" key, then tree_to_text
flattens the result. Kept separate from the API client so the ~20 block-type
cases can be unit-tested against hand-built trees — this is the largest piece
of content-parsing logic among all connectors.
"""

from typing import Any, Dict, List

# Block types whose children belong to a *different* document (their own page),
# so recursion must not descend into them.
PAGE_LIKE = {"child_page", "child_database"}

_HEADING_PREFIX = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### "}


def rich_text_to_plain(rich_text: Any) -> str:
    if not isinstance(rich_text, list):
        return ""
    return "".join((rt or {}).get("plain_text", "") for rt in rich_text)


def _payload(block: Dict[str, Any]) -> Dict[str, Any]:
    return block.get(block.get("type", ""), {}) or {}


def block_to_text(block: Dict[str, Any]) -> str:
    """Flatten one block (without its children) to a text line. Returns ""
    for blocks that carry no prose (images, embeds, dividers, ...)."""
    kind = block.get("type", "")
    payload = _payload(block)
    text = rich_text_to_plain(payload.get("rich_text"))

    if kind in _HEADING_PREFIX:
        return _HEADING_PREFIX[kind] + text if text else ""
    if kind in ("paragraph", "toggle"):
        return text
    if kind in ("bulleted_list_item", "numbered_list_item"):
        return f"- {text}" if text else ""
    if kind == "to_do":
        mark = "x" if payload.get("checked") else " "
        return f"[{mark}] {text}" if text else ""
    if kind == "quote":
        return f"> {text}" if text else ""
    if kind == "callout":
        return text
    if kind == "code":
        lang = payload.get("language") or ""
        return f"```{lang}\n{text}\n```" if text else ""
    if kind == "equation":
        return payload.get("expression") or ""
    if kind == "bookmark":
        caption = rich_text_to_plain(payload.get("caption"))
        url = payload.get("url") or ""
        return f"{caption} {url}".strip()
    if kind == "table_row":
        cells = payload.get("cells") or []
        return " | ".join(rich_text_to_plain(c) for c in cells)
    if kind == "child_page":
        return ""  # its own document; the sync walks it separately
    # divider, image, video, embed, table (container), column_list, column,
    # synced_block, unsupported, ... — no prose of their own
    return ""


def tree_to_text(blocks: List[Dict[str, Any]], depth: int = 0) -> str:
    """Flatten a fetched block tree (children injected under "__children")
    into readable plain text. Children of a block are indented one level so
    nested lists and toggles keep their structure."""
    lines: List[str] = []
    indent = "  " * depth
    for block in blocks or []:
        line = block_to_text(block)
        if line:
            lines.append(indent + line if depth else line)
        if block.get("type") in PAGE_LIKE:
            continue
        children = block.get("__children")
        if children:
            inner = tree_to_text(children, depth + 1)
            if inner:
                lines.append(inner)
    return "\n".join(lines)


def page_title(page: Dict[str, Any]) -> str:
    """Extract a page's title from its properties (the property typed "title"),
    falling back to a database row's first text-ish property."""
    props = page.get("properties") or {}
    for prop in props.values():
        if (prop or {}).get("type") == "title":
            title = rich_text_to_plain(prop.get("title"))
            if title:
                return title
    # databases carry the name at the top level
    title = rich_text_to_plain(page.get("title"))
    return title or "Untitled"
