#!/usr/bin/env python3
"""End-to-end demo: ingest 10 sample documents (Slack/Notion/GitHub/Jira/meeting
formats), wait for memory formation on each, then run 5 queries against the
memory layer and print the streamed answers with provenance.

Documents are ingested SEQUENTIALLY and formation is awaited per-document so
that later documents can link to memory nodes created by earlier ones (e.g.
the Jira ticket that revisits the original Slack decision).

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    python scripts/demo.py            # backend must be running on :8100
"""

import json
import os
import sys
import time

import httpx

sys.path.insert(0, __import__("os").path.dirname(__file__))
from sample_docs import SAMPLE_DOCS  # noqa: E402

BASE = os.environ.get("WHYBASE_API", "http://localhost:8100")
AUTH_EMAIL = os.environ.get("WHYBASE_EMAIL")
AUTH_PASSWORD = os.environ.get("WHYBASE_PASSWORD")

QUERIES = [
    "Why did we choose Postgres over MongoDB?",
    "Was the Postgres decision ever revisited? What happened?",
    "Who advocated for what in our database decisions, and did anyone change their mind?",
    "What open questions do we have about scaling the database?",
    "Why do we use pgvector instead of a dedicated vector database like Pinecone?",
]

BOLD, DIM, CYAN, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[0m",
)


def wait_for_backend(cx: httpx.Client) -> dict:
    for _ in range(30):
        try:
            r = cx.get(f"{BASE}/api/health")
            if r.status_code == 200:
                return r.json()
        except httpx.TransportError:
            pass
        time.sleep(1)
    print(f"{RED}Backend not reachable at {BASE}. Start it first:{RESET}")
    print("  cd whybase/backend && .venv/bin/uvicorn app.main:app --port 8100")
    sys.exit(1)


def login(cx: httpx.Client) -> dict:
    if not AUTH_EMAIL or not AUTH_PASSWORD:
        print(f"{RED}Authenticated API required.{RESET}")
        print("Set WHYBASE_EMAIL and WHYBASE_PASSWORD for an owner/admin user.")
        sys.exit(1)
    r = cx.post(f"{BASE}/api/auth/login", json={
        "email": AUTH_EMAIL,
        "password": AUTH_PASSWORD,
    })
    if r.status_code != 200:
        print(f"{RED}Login failed: {r.status_code} {r.text[:200]}{RESET}")
        sys.exit(1)
    return r.json()


def ingest_all(cx: httpx.Client) -> None:
    print(f"\n{BOLD}== 1. Ingestion + memory formation =={RESET}")
    for i, doc in enumerate(SAMPLE_DOCS, 1):
        r = cx.post(f"{BASE}/api/ingest", json=doc)
        r.raise_for_status()
        doc_id = r.json()["document_id"]
        print(f"  [{i:>2}/10] {doc['source']:<8} {doc['title'][:58]:<58} ", end="", flush=True)
        # generous: local Ollama models form memory slowly and serialize requests
        deadline = time.time() + 600
        status = "pending"
        detail = {}
        while time.time() < deadline:
            detail = cx.get(f"{BASE}/api/documents/{doc_id}").json()
            status = detail["formation_status"]
            if status in ("complete", "failed"):
                break
            time.sleep(2)
        if status == "complete":
            counts = detail.get("memory_counts", {})
            parts = [f"{v} {k}{'s' if v != 1 else ''}" for k, v in sorted(counts.items())]
            print(f"{GREEN}✓ memory formed{RESET} {DIM}({', '.join(parts) or 'no nodes'}){RESET}")
        else:
            print(f"{RED}✗ formation {status}{RESET}")
            if detail.get("formation_error"):
                print(f"    {DIM}{detail['formation_error'][:300]}{RESET}")


def parse_sse(resp: httpx.Response):
    event, data = "message", []
    for line in resp.iter_lines():
        if line == "":
            if data:
                try:
                    yield event, json.loads("".join(data))
                except json.JSONDecodeError:
                    pass
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())


def run_query(cx: httpx.Client, question: str) -> None:
    print(f"\n{BOLD}{CYAN}Q: {question}{RESET}")
    with cx.stream("POST", f"{BASE}/api/query", json={"question": question},
                   timeout=httpx.Timeout(300, connect=10)) as resp:
        resp.raise_for_status()
        meta = None
        for event, payload in parse_sse(resp):
            if event == "status":
                print(f"{DIM}   {payload.get('message', '')}{RESET}")
            elif event == "delta":
                print(payload["text"], end="", flush=True)
            elif event == "metadata":
                meta = payload
            elif event == "error":
                print(f"\n{RED}error: {payload.get('message')}{RESET}")
        print()
        if meta:
            conf = meta.get("confidence", "?")
            color = {"high": GREEN, "medium": YELLOW}.get(conf, RED)
            print(f"\n   {color}confidence: {conf}{RESET}")
            if meta.get("timeline"):
                print(f"   {BOLD}timeline:{RESET}")
                for t in meta["timeline"]:
                    print(f"     {DIM}{t.get('date', '????-??-??')}{RESET}  {t.get('event', '')}")
            if meta.get("citations"):
                print(f"   {BOLD}sources cited:{RESET}")
                for c in meta["citations"]:
                    print(f"     [C{c['chunk_id']}] {c['source']:<8} \"{c['title']}\" "
                          f"— {c['author'] or 'unknown'} ({c['date'] or 'undated'})")
            if meta.get("related_questions"):
                print(f"   {BOLD}worth asking next:{RESET}")
                for q in meta["related_questions"]:
                    print(f"     • {q}")
    print(f"{DIM}{'─' * 78}{RESET}")


def main() -> None:
    with httpx.Client(timeout=60) as cx:
        wait_for_backend(cx)
        auth = login(cx)
        health = cx.get(f"{BASE}/api/health/details").json()
        provider = health.get("llm_provider", "anthropic")
        if provider in {"anthropic", "nvidia"} and not health.get("llm_credentials"):
            key = "ANTHROPIC_API_KEY" if provider == "anthropic" else "NVIDIA_API_KEY"
            print(f"{RED}No {provider} credentials found by the backend.{RESET}")
            print(f"Set {key} in the backend's environment and restart it, "
                  "or run a local Ollama server to use it as the provider.")
            sys.exit(1)
        print(f"{GREEN}Backend healthy{RESET} — db ok, "
              f"LLM: {provider} ({health.get('llm_model', '?')}), "
              f"workspace: {auth.get('workspace', {}).get('name', '?')}.")

        existing = cx.get(f"{BASE}/api/documents").json()
        if existing:
            print(f"{YELLOW}Note: {len(existing)} documents already ingested; "
                  f"ingesting the sample corpus again will duplicate memory.{RESET}")
            if input("Continue anyway? [y/N] ").strip().lower() != "y":
                sys.exit(0)

        t0 = time.time()
        ingest_all(cx)

        print(f"\n{BOLD}== 2. Querying the memory layer =={RESET}")
        for q in QUERIES:
            run_query(cx, q)

        print(f"\n{GREEN}Demo complete in {time.time() - t0:.0f}s.{RESET} "
              f"Open the UI at http://localhost:5173 — try the Timeline and Decision log tabs.")


if __name__ == "__main__":
    main()
