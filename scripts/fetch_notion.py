#!/usr/bin/env python3
"""Ingest a Notion markdown export into YBase.

How to export from Notion:
  1. Open any page (or the root of your workspace)
  2. Click ··· (top-right) → Export → Markdown & CSV → Export
  3. Unzip the downloaded file
  4. Run: python scripts/fetch_notion.py --export-dir ~/Downloads/your-notion-export/

Each .md file becomes one document in memory. Subdirectory names are used as
tags, and re-running is safe (dedup by content hash means no doubles).

Auth (YBase admin account):
    export YBASE_EMAIL=you@example.com
    export YBASE_PASSWORD=yourpassword
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

BASE = os.environ.get("YBASE_API", "http://localhost:8100")
AUTH_EMAIL = os.environ.get("YBASE_EMAIL")
AUTH_PASSWORD = os.environ.get("YBASE_PASSWORD")

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest Notion markdown export into YBase")
    p.add_argument("--export-dir", required=True,
                   help="path to unzipped Notion markdown export directory")
    p.add_argument("--min-chars", type=int, default=100,
                   help="skip files shorter than this many characters (default: 100)")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would be ingested without actually posting")
    return p.parse_args()


def sb_login(cx: httpx.Client) -> None:
    if not AUTH_EMAIL or not AUTH_PASSWORD:
        print(f"{RED}Set YBASE_EMAIL and YBASE_PASSWORD before running.{RESET}")
        sys.exit(1)
    r = cx.post(f"{BASE}/api/auth/login", json={"email": AUTH_EMAIL, "password": AUTH_PASSWORD})
    if r.status_code != 200:
        print(f"{RED}Login failed: {r.status_code} {r.text[:200]}{RESET}")
        sys.exit(1)


def clean_title(path: Path, root: Path) -> str:
    """Use the filename without extension; strip Notion's appended UUID if present."""
    name = path.stem
    # Notion appends a space + 32-char hex id: "Page Title abcdef1234567890abcdef1234567890"
    import re
    name = re.sub(r"\s+[0-9a-f]{32}$", "", name).strip()
    return name or path.stem


def folder_tags(path: Path, root: Path) -> list[str]:
    """Return parent folder names (relative to root) as tags, max 3."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return ["notion"]
    parts = list(rel.parts[:-1])  # exclude filename
    tags = ["notion"] + [p.rstrip("/") for p in parts if p not in (".", "")]
    return tags[:4]


def mtime_iso(path: Path) -> str:
    ts = path.stat().st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_markdown_files(root: Path) -> list[Path]:
    files = sorted(root.rglob("*.md"))
    # also accept .markdown
    files += sorted(root.rglob("*.markdown"))
    return files


def build_doc(path: Path, root: Path, text: str) -> Dict[str, Any]:
    title = clean_title(path, root)
    tags = folder_tags(path, root)
    return {
        "source": "notion",
        "title": title[:200],
        "text": text,
        "author": None,
        "created_at": mtime_iso(path),
        "tags": tags,
    }


def sb_ingest(cx: httpx.Client, doc: Dict[str, Any]) -> tuple[int, bool]:
    r = cx.post(f"{BASE}/api/ingest", json=doc)
    r.raise_for_status()
    data = r.json()
    return data["document_id"], data["duplicate"]


def main() -> None:
    args = parse_args()
    root = Path(args.export_dir).expanduser().resolve()
    if not root.is_dir():
        print(f"{RED}Directory not found: {root}{RESET}")
        sys.exit(1)

    files = find_markdown_files(root)
    print(f"{BOLD}YBase — Notion import{RESET}")
    print(f"  export dir : {root}")
    print(f"  .md files  : {len(files)} found")
    print(f"  min chars  : {args.min_chars}")
    if args.dry_run:
        print(f"  {YELLOW}dry-run — nothing will be posted{RESET}")
    print()

    if not files:
        print(f"{YELLOW}No markdown files found in {root}. "
              "Make sure you unzipped the Notion export and pointed --export-dir at the folder.{RESET}")
        sys.exit(0)

    with httpx.Client(base_url=BASE, timeout=60, follow_redirects=True) as cx:
        if not args.dry_run:
            sb_login(cx)
            print(f"{GREEN}Logged in to YBase.{RESET}\n")

        ingested = skipped_short = dupes = errors = 0

        for i, path in enumerate(files, 1):
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError as e:
                print(f"  [{i:>4}] {RED}read error: {e}{RESET}")
                errors += 1
                continue

            if len(text) < args.min_chars:
                skipped_short += 1
                continue

            doc = build_doc(path, root, text)
            rel = path.relative_to(root)
            label = str(rel)[:72]
            print(f"  [{i:>4}] {label:<72} ", end="", flush=True)

            if args.dry_run:
                print(f"{DIM}(dry-run){RESET}")
                ingested += 1
                continue

            try:
                doc_id, dup = sb_ingest(cx, doc)
                if dup:
                    print(f"{DIM}skip (duplicate){RESET}")
                    dupes += 1
                else:
                    print(f"{GREEN}✓ ingested (doc {doc_id}){RESET}")
                    ingested += 1
            except httpx.HTTPStatusError as e:
                print(f"{RED}✗ {e.response.status_code} {e.response.text[:80]}{RESET}")
                errors += 1

    verb = "would ingest" if args.dry_run else "ingested"
    print(f"\n{GREEN}{BOLD}Done.{RESET} "
          f"{ingested} {verb}, {dupes} skipped (duplicate), "
          f"{skipped_short} too short, {errors} errors.")
    if not args.dry_run:
        print(f"Open the Ops tab to watch memory formation: {BASE.replace('8100', '5173')}")


if __name__ == "__main__":
    main()
