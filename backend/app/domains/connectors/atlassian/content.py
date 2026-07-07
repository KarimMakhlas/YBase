"""Atlassian Document Format (ADF) -> plain text.

Shared by Jira (issue descriptions/comments) and Confluence (page bodies via
the v2 API's body-format=atlas_doc_format), which both emit the same node
schema.
"""

from typing import Any


def adf_to_text(node: Any) -> str:
    """Flatten an ADF document (or any node) to readable plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind == "mention":
        return "@" + (node.get("attrs", {}) or {}).get("text", "user")
    if kind == "emoji":
        return (node.get("attrs", {}) or {}).get("text", "")
    inner = adf_to_text(node.get("content"))
    if kind in ("paragraph", "heading", "codeBlock", "blockquote"):
        return inner + "\n"
    if kind == "listItem":
        return "- " + inner
    return inner
