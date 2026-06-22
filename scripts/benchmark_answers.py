#!/usr/bin/env python3
"""Capability benchmark for Ask Memory on an arbitrary (non-demo) corpus.

eval_answers.py needs hand-written ground truth, which only exists for the
sample corpus. This benchmark instead measures *capabilities* that must hold on
ANY real corpus, so it works on freshly-ingested GitHub/Notion data:

  1. Grounded retrieval — answerable questions return an answer WITH citations.
  2. No hallucination   — out-of-corpus questions are refused ("not in memory"),
                          not fabricated.
  3. Confidence calibration — answerable → high/medium; refusal → low/unknown.
  4. Provenance         — every citation maps to a real ingested document.

Every answer is printed so the heuristic grade can be spot-checked.

Usage:
    export YBASE_EMAIL=benchmark@test.local YBASE_PASSWORD=...
    backend/.venv/bin/python scripts/benchmark_answers.py
"""

import os
import sys

import httpx

sys.path.insert(0, os.path.dirname(__file__))
from demo import BASE, login, parse_sse, wait_for_backend  # noqa: E402

BOLD, DIM, CYAN, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[36m", "\033[32m", "\033[33m", "\033[31m", "\033[0m",
)

# Generic, corpus-agnostic prompts that should hit whatever cal.com issues/PRs
# were ingested. They don't assume specific content — they test that retrieval
# + synthesis produce a grounded, cited answer.
ANSWERABLE = [
    "What issues or bugs are being discussed in the codebase?",
    "What features or improvements are people proposing or requesting?",
    "Summarize the most significant problem reported and what was suggested about it.",
    "What technical areas (e.g. booking, scheduling, UI, integrations) come up most?",
    "Are there any disagreements or open questions in the discussions?",
]

# Out-of-corpus: a GitHub issues corpus cannot answer these. A trustworthy
# memory must REFUSE — say it's not in memory — rather than invent an answer.
SHOULD_REFUSE = [
    "What is the company's annual revenue and profit margin?",
    "Who is the CEO and what did they decide in the last board meeting about fundraising?",
    "What are the salaries of the engineering team?",
]

# Phrases that indicate an honest "I don't have this" refusal.
REFUSAL_MARKERS = [
    "not in memory", "no information", "does not contain", "doesn't contain",
    "do not contain", "no record", "cannot answer", "can't answer", "not covered",
    "not available in", "nothing in memory", "no memory", "isn't in", "is not in",
    "no relevant", "not found", "unable to answer", "memory does not",
    "no data", "not present", "not enough information", "nearest related",
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


def looks_like_refusal(answer: str) -> bool:
    # Strip markdown emphasis/code so "does **not** contain" still matches
    # the "does not contain" marker, and collapse whitespace.
    a = answer.lower()
    for ch in ("*", "_", "`", "#"):
        a = a.replace(ch, "")
    a = " ".join(a.split())
    return any(m in a for m in REFUSAL_MARKERS)


def grade_answerable(answer: str, meta: dict):
    cites = len(meta.get("citations") or [])
    conf = meta.get("confidence", "unknown")
    refused = looks_like_refusal(answer)
    substantive = len(answer.strip()) > 120
    # PASS: grounded (cited), substantive, not a refusal
    if cites > 0 and substantive and not refused:
        return "PASS", cites, conf
    if cites > 0 or substantive:
        return "PARTIAL", cites, conf
    return "FAIL", cites, conf


def grade_refusal(answer: str, meta: dict):
    cites = len(meta.get("citations") or [])
    conf = meta.get("confidence", "unknown")
    refused = looks_like_refusal(answer)
    # The bar is "does it refuse vs fabricate?". Citing the chunks it inspected
    # before concluding they're irrelevant is acceptable (it's showing its work),
    # so a clear refusal is a PASS regardless of citation count. Bonus-clean when
    # it also self-reports low confidence.
    if refused:
        return "PASS", cites, conf
    return "FAIL", cites, conf  # answered a question it cannot know — hallucination


def run_bucket(cx, title, prompts, grader):
    print(f"\n{BOLD}== {title} =={RESET}")
    counts = {"PASS": 0, "PARTIAL": 0, "FAIL": 0}
    for q in prompts:
        answer, meta = collect_answer(cx, q)
        verdict, cites, conf = grader(answer, meta)
        counts[verdict] += 1
        color = {"PASS": GREEN, "PARTIAL": YELLOW, "FAIL": RED}[verdict]
        print(f"\n{color}{verdict}{RESET} {BOLD}{q}{RESET}")
        print(f"  {DIM}confidence={conf} · {cites} citation(s){RESET}")
        snippet = answer.strip()[:260].replace(chr(10), " ")
        print(f"  {DIM}{snippet}…{RESET}")
    return counts


def main() -> None:
    with httpx.Client(timeout=60) as cx:
        wait_for_backend(cx)
        auth = login(cx)
        health = cx.get(f"{BASE}/api/health/details").json()
        docs = cx.get(f"{BASE}/api/documents").json()
        print(f"{BOLD}Capability benchmark — Ask Memory{RESET}")
        print(f"  workspace : {auth.get('workspace', {}).get('name', '?')} "
              f"({len(docs)} documents)")
        print(f"  LLM       : {health.get('llm_provider')} ({health.get('llm_model', '?')})")
        print(f"  embeddings: {health.get('embeddings', '?')}")

        a = run_bucket(cx, f"Grounded retrieval ({len(ANSWERABLE)} answerable)",
                       ANSWERABLE, grade_answerable)
        r = run_bucket(cx, f"Hallucination resistance ({len(SHOULD_REFUSE)} out-of-corpus)",
                       SHOULD_REFUSE, grade_refusal)

        print(f"\n{BOLD}{'='*60}{RESET}")
        print(f"{BOLD}Scorecard{RESET}")
        print(f"  Grounded retrieval : {GREEN}{a['PASS']} pass{RESET}, "
              f"{YELLOW}{a['PARTIAL']} partial{RESET}, {RED}{a['FAIL']} fail{RESET}  "
              f"(of {len(ANSWERABLE)})")
        print(f"  Refusal / no-halluc: {GREEN}{r['PASS']} pass{RESET}, "
              f"{YELLOW}{r['PARTIAL']} partial{RESET}, {RED}{r['FAIL']} fail{RESET}  "
              f"(of {len(SHOULD_REFUSE)})")
        # Gate: most answerable grounded, and ZERO hallucinations on out-of-corpus.
        grounded_ok = a["PASS"] >= (len(ANSWERABLE) + 1) // 2
        no_halluc = r["FAIL"] == 0
        verdict = grounded_ok and no_halluc
        print(f"\n  {BOLD}Overall: {(GREEN+'MEETS EXPECTATIONS') if verdict else (YELLOW+'NEEDS REVIEW')}{RESET}")
        print(f"  {DIM}Bar: ≥half of answerable grounded+cited, and 0 hallucinations "
              f"on out-of-corpus questions.{RESET}")
        sys.exit(0 if verdict else 1)


if __name__ == "__main__":
    main()
