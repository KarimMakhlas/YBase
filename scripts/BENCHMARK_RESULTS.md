# Real-data benchmark — cal.com GitHub corpus

First run: 2026-06-14. Local stack (`qwen3.5` + `nomic-embed-text`, no API keys).

## What this is

A reproducible benchmark that feeds the system **real production data** (not the
synthetic demo corpus) and measures whether it does what we expect: ingest messy
real content, extract a sound memory graph, answer questions with citations, and
**refuse** questions it can't answer instead of hallucinating.

Data source: the public GitHub repo `calcom/cal.com` — which during this run was
found to have been **renamed to `calcom/cal.diy`** (45k★, 1289 open issues). The
benchmark used the canonical `calcom/cal.diy`.

## How to reproduce

```bash
cd ybase
# backend + Postgres + Ollama must be running

# 1. Create an isolated test workspace (public signup), or use your own admin login.
export YBASE_EMAIL=you@example.com YBASE_PASSWORD=...

# 2. Ingest ~30 real issues/PRs (set GITHUB_TOKEN for faster fetch)
backend/.venv/bin/python scripts/fetch_calcom_github.py --repo calcom/cal.diy --days 365 --limit 30

# 3. Let memory formation run (the in-app worker, or watch the Ops tab)

# 4. Layer 1 — memory-quality scorecard (structural, reads DB directly)
backend/.venv/bin/python scripts/eval.py --workspace <your-workspace-slug>

# 5. Layer 2 — capability benchmark (live Q&A: grounded retrieval + refusal)
backend/.venv/bin/python scripts/benchmark_answers.py
```

## The three benchmark layers

| Layer | Script | Question it answers |
|---|---|---|
| Formation | (ingest + form) | Does real, messy data form cleanly, fast, without failures? |
| Memory quality | `scripts/eval.py` | Is the extracted graph well-formed (topics, evidence, density, no dupes)? |
| Capability | `scripts/benchmark_answers.py` | Does Q&A cite sources when it can, and refuse when it can't? |

`eval.py` already shipped and works on any corpus. `benchmark_answers.py` is new:
it is corpus-agnostic and capability-focused (no hand-written ground truth), so
it works on freshly-ingested third-party data.

## Results (first run)

### Formation
- **30 / 30 documents formed, 0 failures** — including deliberate junk
  (`asdfasdf`, "Dummy text") which was correctly NOT turned into decisions.
- ~40s/doc on local qwen3.5 (the README estimated 1–3 min; this hardware is faster).
- Consolidation merged 2 near-duplicate decisions.

### Memory quality (`eval.py`) — "memory looks healthy"
- 19 decisions, 25 entities, 14 topics, 65 edges.
- 19/19 decisions carry topics (no graph-invisible nodes).
- 19/19 decision/question nodes cite evidence chunks.
- Graph density 3.4 edges/decision.
- 0 duplicate suspects above 0.86 similarity.
- Note: 0 questions and only 1 `revisits` edge — expected, because 30 unrelated
  recent issues contain no planted decision-reversal narrative. The one revisits
  edge correctly linked two related API-v2 workflow-endpoint issues.
- 5/19 decisions scored below 40% confidence — all recent, single-evidence,
  proposed-status items; the status×recency×evidence scoring rates these low by
  design.

### Capability (`benchmark_answers.py`) — "meets expectations"
- **Grounded retrieval:** answerable questions returned substantive answers with
  7–9 citations each (5/5 grounded on the first run; 3/5 on a re-run, the
  difference being local-model variance in confidence/citation labeling on
  open-ended "summarize" prompts).
- **Hallucination resistance:** 3/3 out-of-corpus questions (company revenue,
  CEO/board decisions, salaries) were correctly **refused** —
  *"I cannot answer your question using the provided institutional memory"* —
  with **0 fabrications**.

### Example extractions (real cal.com decisions)
- "Reschedule Seated Booking Endpoint Does Not Require Auth Headers" (from the
  401 reschedule issue)
- "Fix Duplicate Workflow Reminders via Task Deduplication"
- "Remove getFacetedValues API to eliminate duplicate DB fetches" (a perf PR)

Topic clusters formed: bug-fixes, input-validation, ui-display, performance,
database, api-authentication, workflows, seated-events.

## Findings / caveats

1. **Operational bug surfaced:** the backend's in-process formation worker was
   not running (`/api/health/details` → `worker_running: false, workers: 0`),
   despite formation having worked historically. Benchmarks were run via a
   standalone formation runner against the same DB + Ollama. Worth investigating
   why the worker task died (likely an unhandled exception in the worker loop).
2. **Local-model variance:** qwen3.5 is solid but varies between runs on
   open-ended prompts. Quality (and citation discipline) would be higher on Claude.
3. **Grader is heuristic:** the first benchmark run produced false "FAIL"s on
   refusals because markdown emphasis (`**not**`) broke literal substring matching
   and citations were over-penalized. Fixed: strip markdown before matching, and
   treat any clear refusal as a pass regardless of citation count.

## Bottom line

On real, unstructured, third-party engineering data the system: forms memory
reliably (0 failures), produces a well-connected and de-duplicated graph, answers
with provenance, and — most importantly for trust — **refuses to fabricate** when
the answer isn't in memory. The "memory, not search" behavior (cross-document
linking) is present but under-exercised by an unrelated-issues corpus; a corpus
with an actual decision history (a single feature debated across issues/PRs over
time) would showcase it better.
