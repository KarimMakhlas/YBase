#!/usr/bin/env python3
"""Answer-quality trial: ask known-answer questions against the memory layer and
grade each answer on required concepts + whether it cited sources.

This is the trust metric that structural checks (scripts/eval.py) can't give:
does Ask Memory actually answer correctly? It runs against the sample corpus
(scripts/sample_docs.py), which has known ground truth — including adversarial
cases a naive RAG fails (the activity feed ended up on Postgres JSONB, not the
much-discussed MongoDB).

Grading is a deterministic keyword + citation heuristic, so it's a gate, not a
judge — every full answer is printed so you can spot-check alongside the score.

Usage:
    export WHYBASE_EMAIL=owner@team.com WHYBASE_PASSWORD=...
    backend/.venv/bin/python scripts/eval_answers.py --seed   # ingest corpus first
    backend/.venv/bin/python scripts/eval_answers.py          # score only
"""

import argparse
import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from demo import BASE, ingest_all, login, parse_sse, wait_for_backend  # noqa: E402

BOLD, DIM, CYAN, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[0m",
)

# Each "expect" entry is a concept the answer must convey; the inner list holds
# acceptable phrasings (any one counts). "avoid" terms hard-fail if present.
GROUND_TRUTH = [
    {
        "q": "Why did we choose Postgres over MongoDB?",
        "expect": [["transaction", "acid", "billing", "relational"],
                   ["team", "experience", "operated", "operational"],
                   ["jsonb", "pgvector", "escape hatch"]],
    },
    {
        "q": "What database stores the activity feed today?",
        "expect": [["postgres", "postgresql"], ["jsonb", "activity_events"]],
        "avoid": ["feed is on mongodb", "moved to mongodb", "uses mongodb"],
    },
    {
        "q": "Was the decision to use Postgres ever reversed?",
        "expect": [["reaffirm", "kept", "upheld", "stood", "not reversed", "stands"],
                   ["plat-214", "activity feed", "benchmark", "won't do"]],
    },
    {
        "q": "What is our public API rate limiting approach?",
        "expect": [["token bucket"], ["redis"], ["100", "per minute", "req/min", "per api key"]],
    },
    {
        "q": "Why did we pick pgvector instead of a dedicated vector DB like Pinecone?",
        "expect": [["single", "one datastore", "existing postgres", "same database", "one database"],
                   ["acl", "join", "filter"]],
    },
    {
        "q": "Who owns the database scaling investigation and what is its status?",
        "expect": [["priya"], ["open", "rfc", "q2", "not a decision", "ongoing", "517"]],
    },
    {
        "q": "What caused the INC-31 connection pool outage?",
        "expect": [["pgbouncer", "pooler", "pooling"],
                   ["connection", "max_connections", "exhaust", "direct connection"]],
    },
    {
        "q": "Is PgBouncer mandatory for services now?",
        "expect": [["mandatory", "required", "must", "enforced"], ["ci", "template"]],
    },
    {
        "q": "What caching layer do we use and for what?",
        "expect": [["redis"], ["aggregate", "read-through", "derived", "dashboard"]],
    },
    {
        "q": "Who advocated for MongoDB in the database debate?",
        "expect": [["dev", "patel"]],
    },
    {
        "q": "Have we adopted a second database besides Postgres?",
        "expect": [["no", "single", "only postgres", "stayed", "within postgres", "one datastore"]],
    },
    {
        "q": "What scaling questions are still unresolved?",
        "expect": [["10m", "10 million", "scaling", "growth"],
                   ["shard", "partition", "replica"]],
    },
]


def collect_answer(cx: httpx.Client, question: str):
    parts, meta = [], {}
    with cx.stream("POST", f"{BASE}/api/query", json={"question": question},
                   timeout=httpx.Timeout(300, connect=10)) as resp:
        resp.raise_for_status()
        for event, payload in parse_sse(resp):
            if event == "delta":
                parts.append(payload.get("text", ""))
            elif event == "metadata":
                meta = payload
            elif event == "error":
                parts.append(f"[error: {payload.get('message')}]")
    return "".join(parts), meta


def grade(answer: str, meta: dict, spec: dict):
    a = answer.lower()
    groups = spec["expect"]
    hits = sum(1 for g in groups if any(s.lower() in a for s in g))
    cited = len(meta.get("citations") or [])
    bad = [b for b in spec.get("avoid", []) if b.lower() in a]
    full = hits == len(groups) and cited > 0 and not bad
    if full:
        verdict = "PASS"
    elif bad or hits == 0:
        verdict = "FAIL"
    else:
        verdict = "PARTIAL"
    return verdict, hits, len(groups), cited, bad


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true",
                    help="ingest the sample corpus (await formation) before scoring")
    args = ap.parse_args()

    with httpx.Client(timeout=60) as cx:
        wait_for_backend(cx)
        auth = login(cx)
        health = cx.get(f"{BASE}/api/health/details").json()
        print(f"{BOLD}Answer-quality trial{RESET} — workspace "
              f"\"{auth.get('workspace', {}).get('name', '?')}\", "
              f"LLM: {health.get('llm_provider')} ({health.get('llm_model', '?')})")
        if args.seed:
            ingest_all(cx)

        print(f"\n{BOLD}== Scoring {len(GROUND_TRUTH)} known-answer questions =={RESET}")
        verdicts = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}
        for spec in GROUND_TRUTH:
            answer, meta = collect_answer(cx, spec["q"])
            verdict, hits, total, cited, bad = grade(answer, meta, spec)
            verdicts[verdict] += 1
            color = {"PASS": GREEN, "PARTIAL": YELLOW, "FAIL": RED}[verdict]
            print(f"\n{color}{verdict}{RESET} {BOLD}{spec['q']}{RESET}")
            print(f"  {DIM}concepts {hits}/{total} · {cited} citation(s)"
                  + (f" · {RED}says: {bad}{DIM}" if bad else "") + RESET)
            print(f"  {DIM}{answer.strip()[:280].replace(chr(10), ' ')}…{RESET}")

        n = len(GROUND_TRUTH)
        correct = verdicts["PASS"]
        print(f"\n{BOLD}Scorecard:{RESET} "
              f"{GREEN}{verdicts['PASS']} pass{RESET}, "
              f"{YELLOW}{verdicts['PARTIAL']} partial{RESET}, "
              f"{RED}{verdicts['FAIL']} fail{RESET}  "
              f"→ {BOLD}{correct}/{n} correct-and-cited ({100*correct//n}%){RESET}")
        print(f"{DIM}Bar for beta: ≥80% correct-and-cited and 0 fails on adversarial "
              f"items. Heuristic grade — skim the answers above to confirm.{RESET}")
        sys.exit(0 if correct >= int(0.8 * n) and verdicts["FAIL"] == 0 else 1)


if __name__ == "__main__":
    main()
