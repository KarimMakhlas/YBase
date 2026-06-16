#!/usr/bin/env python3
"""Import a Slack workspace export into Whybase.

Point this at an unzipped Slack export directory (Workspace Settings →
Import/Export Data → Export). Threads become documents (they're the natural
decision unit); non-threaded chatter is grouped into per-day digests and only
kept when substantial. Documents are ingested oldest-first and formation is
awaited per document so later threads can link to memory formed from earlier
ones (revisits / resolves edges).

Usage:
    backend/.venv/bin/python scripts/import_slack.py /path/to/export \
        [--channel general --channel eng] [--since 2025-01-01] \
        [--limit 20] [--no-wait] [--dry-run]
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BASE = os.environ.get("WHYBASE_API", "http://localhost:8100")
AUTH_EMAIL = os.environ.get("WHYBASE_EMAIL")
AUTH_PASSWORD = os.environ.get("WHYBASE_PASSWORD")
FORMATION_DEADLINE = 600  # local models form memory slowly

SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "bot_add", "bot_remove",
}

BOLD, DIM, GREEN, YELLOW, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m"


def load_users(export_dir: Path) -> dict:
    path = export_dir / "users.json"
    if not path.exists():
        return {}
    users = {}
    for u in json.loads(path.read_text()):
        profile = u.get("profile", {})
        users[u["id"]] = (
            profile.get("display_name") or profile.get("real_name")
            or u.get("real_name") or u.get("name") or u["id"]
        )
    return users


def clean_text(text: str, users: dict) -> str:
    text = re.sub(r"<@(\w+)(?:\|[^>]*)?>", lambda m: "@" + users.get(m.group(1), m.group(1)), text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"<#\w+\|([^>]*)>", r"#\1", text)
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def read_channel_messages(channel_dir: Path, users: dict) -> list:
    msgs = []
    for day_file in sorted(channel_dir.glob("*.json")):
        try:
            day = json.loads(day_file.read_text())
        except json.JSONDecodeError:
            print(f"{YELLOW}  skipping unreadable {day_file.name}{RESET}")
            continue
        for m in day:
            if m.get("type") != "message" or m.get("subtype") in SKIP_SUBTYPES:
                continue
            text = clean_text(m.get("text", ""), users)
            if not text:
                continue
            msgs.append({
                "ts": float(m["ts"]),
                "thread_ts": float(m["thread_ts"]) if m.get("thread_ts") else None,
                "author": users.get(m.get("user", ""), m.get("username", "unknown")),
                "text": text,
            })
    msgs.sort(key=lambda m: m["ts"])
    return msgs


def group_documents(channel: str, msgs: list) -> list:
    """Threads (≥2 messages) become documents; loose messages become per-day
    digests, kept only when there's enough substance to hold memory."""
    roots = {m["thread_ts"] for m in msgs if m["thread_ts"]}
    threads: dict = {}
    loose = []
    for m in msgs:
        if m["thread_ts"] or m["ts"] in roots:
            threads.setdefault(m["thread_ts"] or m["ts"], []).append(m)
        else:
            loose.append(m)

    docs = []
    for root_ts, thread in sorted(threads.items()):
        if len(thread) < 2:
            loose.extend(thread)
            continue
        docs.append(_make_doc(channel, thread, kind="thread"))

    by_day: dict = {}
    for m in loose:
        day = datetime.fromtimestamp(m["ts"], tz=timezone.utc).date().isoformat()
        by_day.setdefault(day, []).append(m)
    for day, day_msgs in sorted(by_day.items()):
        total_chars = sum(len(m["text"]) for m in day_msgs)
        if len(day_msgs) >= 3 or total_chars >= 400:
            docs.append(_make_doc(channel, day_msgs, kind="digest"))

    docs.sort(key=lambda d: d["created_at"])
    return docs


def _make_doc(channel: str, msgs: list, kind: str) -> dict:
    first = msgs[0]
    started = datetime.fromtimestamp(first["ts"], tz=timezone.utc)
    snippet = first["text"].split("\n")[0][:60]
    if kind == "thread":
        title = f"#{channel} — {snippet}"
    else:
        title = f"#{channel} — {started.date().isoformat()} discussion"
    body = "\n\n".join(f"{m['author']}: {m['text']}" for m in msgs)
    return {
        "source": "slack",
        "title": title,
        "text": body,
        "author": first["author"],
        "created_at": started.isoformat(),
        "tags": [channel],
    }


def ingest(docs: list, wait: bool) -> None:
    with httpx.Client(timeout=60) as cx:
        if not AUTH_EMAIL or not AUTH_PASSWORD:
            print(f"{RED}Authenticated API required.{RESET}")
            print("Set WHYBASE_EMAIL and WHYBASE_PASSWORD for an owner/admin user.")
            sys.exit(1)
        login = cx.post(f"{BASE}/api/auth/login", json={
            "email": AUTH_EMAIL,
            "password": AUTH_PASSWORD,
        })
        if login.status_code != 200:
            sys.exit(f"{RED}Login failed: {login.status_code} {login.text[:200]}{RESET}")
        health = cx.get(f"{BASE}/api/health/details").json()
        print(f"Backend ok — LLM: {health.get('llm_provider')} ({health.get('llm_model')}), "
              f"embeddings: {health.get('embeddings')}\n")
        for i, doc in enumerate(docs, 1):
            r = cx.post(f"{BASE}/api/ingest", json=doc)
            r.raise_for_status()
            doc_id = r.json()["document_id"]
            print(f"  [{i:>3}/{len(docs)}] {doc['title'][:64]:<64} ", end="", flush=True)
            if not wait:
                print(f"{DIM}scheduled{RESET}")
                continue
            status, detail = "pending", {}
            deadline = time.time() + FORMATION_DEADLINE
            while time.time() < deadline:
                detail = cx.get(f"{BASE}/api/documents/{doc_id}").json()
                status = detail["formation_status"]
                if status in ("complete", "failed"):
                    break
                time.sleep(3)
            if status == "complete":
                counts = detail.get("memory_counts", {})
                parts = [f"{v} {k}{'s' if v != 1 else ''}" for k, v in sorted(counts.items())]
                print(f"{GREEN}✓{RESET} {DIM}({', '.join(parts) or 'no nodes'}){RESET}")
            else:
                print(f"{RED}✗ formation {status}{RESET}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Import a Slack export into Whybase")
    ap.add_argument("export_dir", type=Path)
    ap.add_argument("--channel", action="append", help="only these channels (repeatable)")
    ap.add_argument("--since", help="only messages on/after this date (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, help="max documents to ingest")
    ap.add_argument("--no-wait", action="store_true", help="don't await formation per doc")
    ap.add_argument("--dry-run", action="store_true", help="show what would be ingested")
    args = ap.parse_args()

    if not args.export_dir.is_dir():
        sys.exit(f"not a directory: {args.export_dir}")
    users = load_users(args.export_dir)
    since_ts = (
        datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp()
        if args.since else 0.0
    )

    all_docs = []
    for channel_dir in sorted(p for p in args.export_dir.iterdir() if p.is_dir()):
        channel = channel_dir.name
        if args.channel and channel not in args.channel:
            continue
        msgs = [m for m in read_channel_messages(channel_dir, users) if m["ts"] >= since_ts]
        docs = group_documents(channel, msgs)
        print(f"#{channel}: {len(msgs)} messages → {len(docs)} documents")
        all_docs.extend(docs)

    all_docs.sort(key=lambda d: d["created_at"])
    if args.limit:
        all_docs = all_docs[: args.limit]
    print(f"\n{BOLD}{len(all_docs)} documents to ingest{RESET}\n")

    if args.dry_run:
        for d in all_docs:
            print(f"  {d['created_at'][:10]}  {d['title'][:70]}  {DIM}{len(d['text'])} chars{RESET}")
        return
    ingest(all_docs, wait=not args.no_wait)


if __name__ == "__main__":
    main()
