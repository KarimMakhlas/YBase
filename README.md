# Whybase for Teams

An AI **memory layer** over a company's knowledge sources (Slack, Notion, GitHub, Jira). Not search: it remembers *decisions and their reasoning*, links them across sources and time, and answers "why" questions with full provenance.

> "Why do we use Postgres?" → the original Slack debate, the reasoning at the time, who advocated what, the Jira ticket where it almost got reversed, and the benchmark that settled it — as one coherent, cited answer.

Runs **fully local** (Ollama for both LLM and embeddings, zero API keys) or on **Claude + Voyage** when credentials are present — switching is automatic.

## How it works

```
ingest (API/UI/Slack) ──► dedup (content hash) ──► chunk + embed ──► Postgres (pgvector + FTS)
                       │
                       └─► MEMORY FORMATION JOB QUEUE (one at a time per
                             workspace, workspaces run in parallel, bounded
                             retries + backoff, crash recovery)
                             extracts: decisions (+reasoning, advocates, alternatives, status)
                                       entities, open/resolved questions, conflicts
                             links them into a typed graph:
                             decision ─involves→ person     decision ─about→ topic
                             decision ─revisits→ decision   decision ─resolves→ question
                       └─► CONSOLIDATION: near-duplicate decisions merge by
                             embedding similarity; topicless decisions get
                             fallback topics so the graph never goes edgeless

query (API, SSE) ──► short follow-ups get rewritten as standalone questions
                  ──► hybrid retrieval: vector search + Postgres full-text,
                      fused with reciprocal-rank fusion
                  ──► GRAPH EXPANSION over typed edges (2 hops)
                  ──► pull evidence chunks for discovered decisions/questions,
                      highest-confidence memory first (status × recency × evidence)
                  ──► LLM reasons over chunks + graph + conversation → streamed
                      answer with [C<id>] citations, confidence, timeline,
                      follow-ups, and a retrieval trace
```

The graph expansion step is what makes this memory rather than search: a question that vector-matches the September Slack debate also surfaces the January Jira near-reversal, because the graph edge `revisits` connects them — even when the ticket shares few words with the question.

## Providers

**LLM** (`LLM_PROVIDER`: `auto` | `anthropic` | `ollama`, default `auto`):

| Provider | When | Details |
|---|---|---|
| Anthropic | credentials present | `claude-fable-5`, streaming everywhere, `thinking: {type: "adaptive"}`, `output_config: {effort: "high"}`, structured outputs via json_schema |
| Ollama | otherwise | `qwen3.5` by default (`OLLAMA_MODEL`), native API, streaming chat with `think: false`. Structured extraction embeds the JSON schema in the prompt instead of Ollama's grammar-constrained `format` — on thinking models the grammar only runs after an unbounded thinking pass (which jams the GPU queue), and `format` + `think:false` together silently drop the grammar |

**Embeddings** (`EMBED_PROVIDER`: `auto` | `voyage` | `ollama` | `local`, default `auto` — picked once and pinned so embedding spaces never mix):

| Provider | When | Details |
|---|---|---|
| Voyage AI | `VOYAGE_API_KEY` set | `voyage-3-lite`, 512-dim |
| Ollama | otherwise, if reachable | `nomic-embed-text`, Matryoshka-truncated to 512 dims, `search_document:`/`search_query:` task prefixes |
| local hash | fallback | deterministic lexical embedder, demo-grade |

After switching embedding providers, re-embed the corpus: `backend/.venv/bin/python scripts/reembed.py`.

## Quickstart (one command, full stack)

Prereqs: Docker, [Ollama](https://ollama.com) running on the host.

```bash
cd whybase
ollama pull qwen3.5 && ollama pull nomic-embed-text
docker compose --profile app up -d --build    # db + backend + built UI
# → http://localhost:8100 (first visit bootstraps the owner account)
```

## Quickstart (local dev)

Prereqs: Docker, Python 3.9+, Node 18+, [Ollama](https://ollama.com).

```bash
cd whybase

# 0. Local models
ollama pull qwen3.5 && ollama pull nomic-embed-text

# 1. Database (pgvector on host port 5433)
docker compose up -d

# 2. Backend
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8100          # schema auto-created on startup

# 3. Frontend (separate terminal)
cd ../frontend && npm install && npm run dev          # http://localhost:5173

# 4. Open the UI and complete first-run bootstrap
# http://localhost:5173 creates the first workspace owner account.
# Then open the admin Ops tab to seed demo data, watch formation, and clear failures.

# 5. Demo: ingest 10 docs + run 5 queries (separate terminal)
cd ..
export WHYBASE_EMAIL=owner@example.com
export WHYBASE_PASSWORD='your-bootstrap-password'
backend/.venv/bin/python scripts/demo.py
```

To use Claude instead, `export ANTHROPIC_API_KEY=sk-ant-...` before starting the backend — the provider switches automatically (check authenticated `GET /api/health/details`).

The first browser visit shows a bootstrap screen. That creates the initial workspace, owner user, and assigns any existing local memory to that workspace. Passwords must be at least 12 characters.

The admin **Ops** tab is the MVP readiness center: launch checklist, provider/queue status, source sync health, failed-formation retry, failed/paused sync retry, and a one-click four-document demo seed that runs through the normal ingest pipeline.

The CLI demo ingests sequentially and waits for memory formation on each document so later documents can link to earlier memory (the Jira ticket links to the Slack decision it revisits). Local models form memory slowly — expect 1–3 minutes per document.

## Importing a real Slack workspace

Admins can connect Slack from the **Sources** tab:

1. Set `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET`,
   `SLACK_REDIRECT_BASE_URL`, and `CONNECTOR_SECRET_KEY`.
2. In Slack app settings, add the redirect URL:
   `SLACK_REDIRECT_BASE_URL/api/integrations/slack/oauth/callback`.
3. Give the Slack app bot scopes `channels:read` and `channels:history`, then
   enable Events API delivery to `/api/integrations/slack/events`.
4. Open the app as an owner/admin, connect Slack, select public channels, and run
   the 90-day backfill.

Selected public channels sync into workspace-wide memory. Private channels, DMs,
files, attachments, and source-level ACLs are intentionally outside v1.

Offline Slack exports are still supported as an admin fallback.

Unzip a Slack export (Workspace Settings → Import/Export Data → Export), then:

```bash
backend/.venv/bin/python scripts/import_slack.py /path/to/export \
    --channel engineering --since 2025-01-01 --limit 20 --dry-run   # preview
backend/.venv/bin/python scripts/import_slack.py /path/to/export \
    --channel engineering --since 2025-01-01 --limit 20             # ingest
```

Threads become documents (the natural decision unit); loose chatter rolls into per-day digests kept only when substantial. Mentions, links, and HTML escapes are cleaned; documents ingest oldest-first awaiting formation so revisit links can form.

`scripts/demo.py` and `scripts/import_slack.py` use the authenticated API. Set `WHYBASE_EMAIL` and `WHYBASE_PASSWORD` for an owner/admin user before running them.

## API

| Endpoint | Description |
|---|---|
| `GET /api/auth/bootstrap-status`, `POST /api/auth/bootstrap` | First-run workspace owner setup |
| `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me` | Password login with HTTP-only session cookie |
| `GET/POST/PATCH /api/workspace/users` | Workspace user management (`owner/admin/member`) |
| `GET /api/sources`, `GET /api/sources/slack/install-url` | Admin Slack connector status and OAuth install URL |
| `GET/PATCH /api/sources/{id}/streams` | Admin channel discovery and selected-channel management |
| `POST /api/sources/{id}/sync`, `GET /api/sources/{id}/jobs` | Admin selected-channel backfill and sync job status |
| `POST /api/sources/{id}/jobs/{job_id}/retry` | Admin retry for failed/paused Slack sync jobs |
| `GET /api/ops/overview`, `POST /api/ops/demo-seed`, `POST /api/ops/failed-documents/retry` | Admin MVP readiness dashboard, demo data seed, and failed formation recovery |
| `GET/PATCH/POST /api/memory-review` | Admin memory review: edit, mark reviewed, archive, and unarchive extracted nodes |
| `POST/GET/PATCH /api/answer-feedback` | Members mark/flag saved answers; admins inspect and resolve feedback |
| `POST /api/ingest` | `{source, title, text, author?, created_at?, tags?}` → content-hash dedup, chunks, embeds, stores, enqueues memory formation |
| `POST /api/documents/{id}/reform` / `POST /api/relink` | Re-run formation on one document / re-queue the whole corpus oldest-first so links can form order-independently |
| `POST /api/query` | `{question, history?}` → SSE stream: `status`, `delta` (answer tokens), `metadata` (confidence, citations, timeline, related questions, retrieval trace), `done`. `history` carries prior turns so follow-ups resolve |
| `GET /api/documents` / `GET /api/documents/{id}?full=true` | Ingested docs + formation status + extracted-memory counts; `full` returns the raw text for the document viewer |
| `GET /api/decisions` | Decision log with confidence scores — filter by `topic`, `person`, `status`, `q` |
| `GET /api/timeline` | Documents + decisions + questions over time, with revisited flags |
| `GET /api/graph` / `GET /api/nodes/{id}` | Full memory graph; single node with evidence documents and neighbors |
| `GET /api/search?q=` | Cmd-K search across decisions, questions, people, topics, documents |
| `GET /api/people` / `GET /api/people/{id}` | Person pages: decisions involved in (with their recorded positions), questions raised, source documents |
| `GET /api/stats?since=` | Counts, recent decisions, open + stale questions, relitigation alerts, and a since-you-were-last-here digest |
| `POST /api/integrations/slack/events` | Slack Events API receiver (signed); threads buffer and roll up into documents after going quiet |
| `GET /api/sessions` (+POST, +DELETE, `/{id}/messages`) | Persisted chat sessions |
| `GET /api/health` / `GET /api/health/details` | Public DB/bootstrap status; authenticated admin provider/queue details |

## UI

Twelve views (tabs), plus a ⌘K palette that searches everything in memory and
jumps to it in the right view. Decisions, people, documents, and citations are
clickable everywhere — sources open in a document viewer with the cited chunk
highlighted. Admins get a status footer (provider, formation queue, last
memory write).

- **Home** — counts, recent decisions, open + stale questions, a
  since-you-were-last-here digest, and a relitigation banner when a new
  document revisits a settled decision
- **Ask memory** — streaming chat that carries conversation context
  (follow-ups work), citation chips, confidence, per-answer timeline,
  copy/regenerate, a "how I remembered this" retrieval trace, and a
  structured not-in-memory state; history persisted server-side
- **Timeline** — continuous chronology of documents, decisions, and questions
  with month markers, filters, and revisited badges
- **Decision log** — every extracted decision, collapsible: status, confidence
  meter, positions per person, alternatives considered, revisit chain, graph
  relations, sources
- **People** — person pages: everything someone advocated, decided, or raised,
  with their recorded positions quoted
- **Graph** — interactive force-directed view; drag nodes, click for detail
  and evidence, focus mode shows a node's 2-hop neighborhood
- **Ops** — admin MVP readiness checklist, provider/queue status, demo seed, source health, and recovery actions
- **Review** — admin curation for extracted memory: edit fields, validate JSON data, mark reviewed, archive/unarchive
- **Feedback** — admin queue for member answer feedback with citations, trace nodes, Review/document jumps, and resolution states
- **Sources** — admin Slack OAuth, public channel selection, 90-day backfill, and sync status
- **+ Add** — paste or drop documents into memory; live formation status, extracted-memory counts, retry on failure
- **Settings** — workspace users and roles

## Project structure

```
backend/app/
  main.py          FastAPI app setup, middleware, API router, static UI mount
  api/
    router.py      one place that wires every HTTP router onto the app
    routes/        thin API aliases for routes that now live in domains
  core/            config, db/schema.sql, crypto, mailer, observability,
                   rate limits, and small shared date helpers
  providers/       LLM and embedding provider wrappers
  domains/
    auth/          password auth, sessions, workspace membership, audit log
    documents/     content-hash dedup, chunking, embedding, storage, enqueue
    query/         hybrid retrieval, prompts, SSE streaming, citations
    memory/        graph primitives, formation worker, review, views, scoring
    connectors/    Slack/Jira/GitHub OAuth, API clients, sync, events
    chat/          persisted Ask Memory sessions
    feedback/      answer feedback trust loop
    analytics/     workspace analytics and memory quality
    digest/        digest generation and delivery
    ops/           readiness/recovery endpoints and demo data
  auth.py, ingest.py, llm.py, sources.py, memory/...
                   compatibility shims for existing scripts/tests/imports
backend/tests/     pytest suite (pure helpers + DB-backed graph/queue tests)
frontend/          React + Vite (home, chat, timeline, decisions, people, graph,
                   ops, review, feedback, sources, add, settings + cmd-K,
                   doc viewer, footer)
scripts/
  sample_docs.py   10 simulated documents (Slack/Notion/GitHub/Jira/meeting)
  demo.py          end-to-end demo: ingest all + 5 queries
  eval.py          memory-quality scorecard (formation health, topic/evidence
                   coverage, graph density, duplicate suspects, ground truth)
  import_slack.py  import an unzipped Slack workspace export
  reembed.py       re-embed all chunks after switching embedding providers
docs/diagrams/     tracked architecture/workflow diagrams
```

## Tests & evals

```bash
cd backend && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest            # unit + DB tests (uses whybase_test DB)
cd .. && backend/.venv/bin/python scripts/eval.py   # memory-quality scorecard
```

Run the eval after changing formation prompts, models, or consolidation
thresholds — extraction quality regresses silently otherwise. Exit code is
non-zero when issues are found, so it slots into CI.

## Notes & limits (MVP)

- Graph is adjacency tables in Postgres (per spec) — no Neo4j needed at this scale.
- Memory formation is per-document with the existing-graph digest in context; at large scale you'd shard the digest by similarity to the new document.
- Entity/decision dedup is by normalized label; formation is prompted to reuse exact existing labels so repeated mentions merge and accrete evidence.
- Answer and extraction quality scale with the model: local qwen3.5 is solid, Claude is better — the switch is just an env var.
- Auth/multi-tenancy is workspace-scoped for v1. It does not yet enforce per-document/source ACLs inside a workspace.

## License

Copyright © 2026 Whybase. All rights reserved. This is proprietary software; no license is granted for use, copying, modification, or distribution without prior written permission.
