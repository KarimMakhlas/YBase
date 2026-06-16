#!/usr/bin/env python3
"""Memory-quality scorecard. Reads the database directly (no auth needed) and
scores what formation actually produced — because extraction quality regresses
silently when prompts or models change.

Generic checks (any corpus):
  - formation completion rate, stuck/failed documents
  - decisions per document, topic coverage (no edgeless decisions)
  - evidence coverage (every decision/question carries chunk links)
  - graph connectivity (edges per decision; revisit/resolve links present)
  - duplicate suspects (embedding similarity between decision labels)

Ground-truth checks (when the sample corpus from scripts/sample_docs.py has
been ingested): the planted Postgres decision, the MongoDB revisit chain, and
the activity-feed resolution must exist.

Usage:
    backend/.venv/bin/python scripts/eval.py [--workspace SLUG]
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
sys.path.insert(0, os.path.dirname(__file__))  # for sample_docs ground truth

from app.core import db  # noqa: E402
from app.providers.embeddings import embed_texts  # noqa: E402
from app.domains.memory.consolidate import _signature, similar_pairs  # noqa: E402
from app.domains.memory.scoring import node_score  # noqa: E402

LOW_CONFIDENCE = 0.4  # keep in sync with routes/analytics.py memory_quality

BOLD, DIM, GREEN, YELLOW, RED, RESET = (
    "\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m",
)


def mark(ok: bool, warn: bool = False) -> str:
    if ok:
        return f"{GREEN}✓{RESET}"
    return f"{YELLOW}~{RESET}" if warn else f"{RED}✗{RESET}"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=None,
                        help="workspace slug (default: the one holding the most documents)")
    parser.add_argument("--sim-threshold", type=float, default=0.86)
    args = parser.parse_args()

    pool = await db.get_pool()
    async with pool.acquire() as conn:
        if args.workspace:
            ws = await conn.fetchrow(
                "SELECT id, name FROM workspaces WHERE lower(slug)=lower($1)", args.workspace
            )
        else:
            ws = await conn.fetchrow(
                "SELECT w.id, w.name FROM workspaces w "
                "LEFT JOIN documents d ON d.workspace_id = w.id "
                "GROUP BY w.id ORDER BY count(d.id) DESC, w.id LIMIT 1"
            )
        if ws is None:
            print(f"{RED}workspace not found{RESET}")
            sys.exit(2)
        wid = ws["id"]

        docs = await conn.fetch(
            "SELECT id, title, formation_status FROM documents WHERE workspace_id=$1", wid
        )
        decisions = await conn.fetch(
            "SELECT id, label, summary, status, data FROM memory_nodes "
            "WHERE workspace_id=$1 AND kind='decision' AND archived_at IS NULL", wid
        )
        questions = await conn.fetch(
            "SELECT id, label, status FROM memory_nodes "
            "WHERE workspace_id=$1 AND kind='question' AND archived_at IS NULL", wid
        )
        topic_cov = await conn.fetch(
            "SELECT n.id, n.label, count(e.id) FILTER (WHERE t.kind='topic') AS topics "
            "FROM memory_nodes n "
            "LEFT JOIN memory_edges e ON (e.src=n.id OR e.dst=n.id) "
            "LEFT JOIN memory_nodes t ON t.id = CASE WHEN e.src=n.id THEN e.dst ELSE e.src END "
            "WHERE n.workspace_id=$1 AND n.kind='decision' AND n.archived_at IS NULL "
            "GROUP BY n.id", wid
        )
        evidence_cov = await conn.fetch(
            "SELECT n.id, n.label, count(cl.chunk_id) AS evidence FROM memory_nodes n "
            "LEFT JOIN chunk_links cl ON cl.node_id = n.id "
            "WHERE n.workspace_id=$1 AND n.kind IN ('decision', 'question') "
            "AND n.archived_at IS NULL GROUP BY n.id", wid
        )
        edge_counts = await conn.fetchrow(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE relation='revisits') AS revisits, "
            "count(*) FILTER (WHERE relation='resolves') AS resolves, "
            "count(*) FILTER (WHERE relation='about') AS about "
            "FROM memory_edges WHERE workspace_id=$1", wid
        )

    print(f"{BOLD}Memory quality — workspace “{ws['name']}”{RESET}\n")

    # 1. formation health
    total = len(docs)
    complete = sum(1 for d in docs if d["formation_status"] == "complete")
    failed = [d for d in docs if d["formation_status"] == "failed"]
    stuck = [d for d in docs if d["formation_status"] in ("pending", "processing")]
    print(f"{mark(complete == total and total > 0)} formation: {complete}/{total} complete"
          + (f", {len(failed)} failed" if failed else "")
          + (f", {len(stuck)} in flight" if stuck else ""))
    for d in failed:
        print(f"    {RED}failed:{RESET} [{d['id']}] {d['title'][:60]}")

    # 2. extraction density
    per_doc = len(decisions) / total if total else 0
    print(f"{mark(0.4 <= per_doc <= 4, warn=per_doc > 0)} decisions per document: "
          f"{per_doc:.1f} ({len(decisions)} decisions, {len(questions)} questions)")

    # 3. topic coverage — an edgeless decision is invisible to graph retrieval
    topicless = [r for r in topic_cov if r["topics"] == 0]
    print(f"{mark(not topicless)} topic coverage: "
          f"{len(topic_cov) - len(topicless)}/{len(topic_cov)} decisions carry topics")
    for r in topicless:
        print(f"    {YELLOW}no topics:{RESET} [{r['id']}] {r['label'][:60]}")

    # 4. evidence coverage
    bare = [r for r in evidence_cov if r["evidence"] == 0]
    print(f"{mark(not bare)} evidence coverage: "
          f"{len(evidence_cov) - len(bare)}/{len(evidence_cov)} nodes cite chunks")

    # 5. graph connectivity
    epd = edge_counts["total"] / len(decisions) if decisions else 0
    print(f"{mark(epd >= 2, warn=epd >= 1)} graph density: {edge_counts['total']} edges "
          f"({epd:.1f}/decision) — about: {edge_counts['about']}, "
          f"revisits: {edge_counts['revisits']}, resolves: {edge_counts['resolves']}")

    # 5b. confidence — decisions scoring low are easy to mistrust
    evidence_by_id = {r["id"]: r["evidence"] for r in evidence_cov}
    low_conf = []
    for d in decisions:
        score = node_score(d["status"], d["data"], evidence_count=evidence_by_id.get(d["id"], 0))
        if score < LOW_CONFIDENCE:
            low_conf.append((d["label"], score))
    print(f"{mark(not low_conf, warn=True)} confidence: "
          f"{len(decisions) - len(low_conf)}/{len(decisions)} decisions above "
          f"{LOW_CONFIDENCE:.0%}")
    for label, score in low_conf:
        print(f"    {YELLOW}{score:.0%}{RESET} {label[:60]}")

    # 6. duplicate suspects
    if len(decisions) >= 2:
        texts = [_signature(r["label"], r["summary"] or "") for r in decisions]
        vecs = await embed_texts(texts)
        pairs = similar_pairs(
            [(r["id"], v) for r, v in zip(decisions, vecs)], args.sim_threshold
        )
        label = {r["id"]: r["label"] for r in decisions}
        print(f"{mark(not pairs)} duplicate suspects above {args.sim_threshold}: {len(pairs)}")
        for keep, drop, sim in pairs:
            print(f"    {YELLOW}{sim:.2f}{RESET} “{label[keep][:40]}” ↔ “{label[drop][:40]}”")
    else:
        pairs = []
        print(f"{mark(True)} duplicate suspects: corpus too small to compare")

    # 7. sample-corpus ground truth — only meaningful when most of the demo
    #    corpus is loaded (the revisit chain spans documents 1 → 6)
    truth_failures = 0
    try:
        from sample_docs import SAMPLE_DOCS
        sample_titles = {d["title"] for d in SAMPLE_DOCS}
    except ImportError:
        sample_titles = set()
    titles = {d["title"] for d in docs}
    if sample_titles and len(titles & sample_titles) >= len(sample_titles) - 2:
        print(f"\n{BOLD}Sample-corpus ground truth{RESET}")
        labels = " | ".join(r["label"].lower() for r in decisions)
        has_pg = "postgres" in labels
        print(f"{mark(has_pg)} planted Postgres decision extracted")
        has_revisit = edge_counts["revisits"] > 0
        print(f"{mark(has_revisit)} revisit chain formed "
              f"(PLAT-214 challenges the Postgres decision)")
        has_resolve = edge_counts["resolves"] > 0 or any(
            q["status"] == "resolved" for q in questions)
        print(f"{mark(has_resolve)} at least one question resolved "
              f"(activity-feed debate settles)")
        truth_failures = sum(1 for ok in (has_pg, has_revisit, has_resolve) if not ok)

    problems = bool(
        total == 0 or complete < total or failed or stuck or topicless or bare or pairs
        or truth_failures
    )
    print(f"\n{BOLD}{'⚠ issues found' if problems else '✓ memory looks healthy'}{RESET}")
    await db.close_pool()
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    asyncio.run(main())
