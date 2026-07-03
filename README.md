<div align="center">
  <img src="frontend/public/ybase-mark.svg" alt="YBase logo" width="84" height="84">

  # YBase

  **The AI memory layer for teams that need to remember why.**

  YBase turns scattered conversations, tickets, documents, and code discussions
  into a connected, cited record of decisions.

  [![CI](https://github.com/KarimMakhlas/YBase/actions/workflows/ci.yml/badge.svg)](https://github.com/KarimMakhlas/YBase/actions/workflows/ci.yml)
  ![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
  ![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-009688?logo=fastapi&logoColor=white)
  ![React](https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=111827)
  ![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-4169E1?logo=postgresql&logoColor=white)
  ![License](https://img.shields.io/badge/license-proprietary-6D6FEA)

  [Quick start](#quick-start) ·
  [Architecture](#architecture) ·
  [Data model](#data-model) ·
  [Configuration](#configuration) ·
  [Development](#development) ·
  [Deployment](#deployment) ·
  [All diagrams](docs/architecture.html)
</div>

---

## Why YBase?

Company knowledge is easy to find and hard to understand. Search can retrieve a
message or ticket, but it rarely reconstructs the full story:

> Why did we choose Postgres? Who argued for it? What alternatives were rejected?
> Was that decision revisited later?

YBase preserves that story. It extracts decisions, reasoning, people, topics,
questions, and evidence; connects them over time; and answers with citations
back to the original source material.

| Traditional knowledge search | YBase |
|---|---|
| Finds matching words | Reconstructs decisions and reasoning |
| Returns isolated documents | Expands through a typed memory graph |
| Treats every result equally | Ranks by relevance, status, recency, and evidence |
| Loses follow-up context | Carries conversation history across questions |
| Produces opaque answers | Streams cited answers with confidence and provenance |

### The memory graph, visually

```mermaid
flowchart LR
  D["Decision<br/>status: decided / proposed /<br/>revisited / reversed / reaffirmed"]:::green
  E["Entity<br/>person / project / system / feature / team"]:::blue
  T["Topic"]:::amber
  Q["Question<br/>status: open / resolved"]:::blue
  Doc[("Document + Chunk<br/>(evidence)")]:::purple

  D -->|involves| E
  D -->|about| T
  D -->|revisits| D
  D -->|resolves| Q
  D -->|relates_to| D
  Q -->|about| T
  Q -->|raised_by| E
  Q -->|relates_to| Q
  D -.evidence.-> Doc
  Q -.evidence.-> Doc
  E -.evidence.-> Doc

  classDef blue   fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:1.4px;
  classDef green  fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:1.4px;
  classDef amber  fill:#fef3c7,stroke:#d97706,color:#78350f,stroke-width:1.4px;
  classDef purple fill:#f3e8ff,stroke:#9333ea,color:#581c87,stroke-width:1.4px;
```

Four node kinds, connected by typed edges that carry meaning — a `revisits`
edge is why a question that matches an old Slack debate can also surface a
later Jira reversal, even when the two sources share almost no words in
common. Every node traces back to the document and chunk that produced it.

## Product at a glance

| | Capability | What it gives the team |
|---|---|---|
| 🧠 | **Decision memory** | Decisions, rationale, alternatives, advocates, status, and revisit history |
| 🔎 | **Hybrid retrieval** | pgvector similarity, PostgreSQL full-text search, reciprocal-rank fusion, and graph expansion |
| 🔗 | **Connected context** | Typed links between decisions, people, topics, questions, and source evidence |
| 💬 | **Cited conversations** | Streaming answers, follow-ups, timelines, confidence, and retrieval traces |
| 🔌 | **Source integrations** | Slack, GitHub, Jira, direct upload, and offline Slack export |
| 🛡️ | **Workspace controls** | Cookie sessions, roles, audit events, rate limits, encrypted connector tokens, and billing gates |
| 🧰 | **Operational tooling** | Queue health, retries, demo seeding, memory review, feedback triage, and quality evaluation |
| 🏠 | **Flexible inference** | Anthropic, NVIDIA NIM, or fully local Ollama; Voyage, Ollama, or local embeddings |

## Architecture

One container runs everything: FastAPI serves the built React SPA as static
files and shares its PostgreSQL connection pool with an in-process asyncio
formation worker. There's no separate queue service or graph database — a
bounded worker loop and adjacency tables in Postgres carry the whole system at
current scale.

```mermaid
flowchart LR
  subgraph Client["Browser"]
    SPA["React SPA (Vite, JSX)<br/>hash-based routing, SSE client"]:::blue
  end
  subgraph Server["Single container — Docker / Fly.io machine"]
    API["FastAPI app<br/>app/main.py"]:::blue
    Worker["Formation worker<br/>in-process asyncio loop<br/>domains/memory/worker.py"]:::blue
    Static["Static file mount<br/>StaticFiles(html=True)"]:::gray
  end
  DB[(PostgreSQL 16 + pgvector<br/>Neon / Fly Postgres, pooled)]:::purple
  LLMP{{"LLM providers"}}:::purple
  EmbedP{{"Embedding providers"}}:::purple
  Ext["Slack / GitHub / Jira APIs"]:::gray

  SPA -->|fetch /api/*, credentials include| API
  SPA -->|POST /api/query, SSE| API
  API --> Static
  API <-->|asyncpg pool, max 20, 30s stmt timeout| DB
  Worker <-->|claim jobs: FOR UPDATE SKIP LOCKED| DB
  API -->|schedule_formation call| Worker
  Worker --> LLMP
  Worker --> EmbedP
  API --> EmbedP
  Worker -->|sync jobs, token refresh| Ext

  classDef blue   fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:1.4px;
  classDef purple fill:#f3e8ff,stroke:#9333ea,color:#581c87,stroke-width:1.4px;
  classDef gray   fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.4px;
```

For the domain-by-domain module map, request lifecycle, and job-queue state
machine, see the [full architecture documentation](#full-architecture-documentation).

## Quick start

### Option A — full stack with Docker

This starts PostgreSQL, builds the React UI, and serves everything from FastAPI
at `http://localhost:8100`.

**Prerequisites:** Docker and [Ollama](https://ollama.com) running on the host.

```bash
git clone https://github.com/KarimMakhlas/YBase.git
cd YBase

ollama pull qwen3.5
ollama pull nomic-embed-text

docker compose --profile app up -d --build
curl http://localhost:8100/api/health
```

Open **http://localhost:8100**. On a fresh database, the public landing page
guides the first owner through account and workspace setup.

Stop the stack with:

```bash
docker compose --profile app down
```

### Option B — local development

**Prerequisites:** Python 3.11+, Node.js 20+, Docker, and either Ollama or a
configured hosted LLM provider.

```bash
git clone https://github.com/KarimMakhlas/YBase.git
cd YBase

# 1. Local PostgreSQL + pgvector
docker compose up -d db

# 2. Backend
cp backend/.env.example backend/.env
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload --port 8100
```

In a second terminal:

```bash
cd YBase/frontend
npm ci
npm run dev
```

| Service | URL |
|---|---|
| Web application | http://localhost:5173 |
| Backend health | http://localhost:8100/api/health |
| Interactive API docs | http://localhost:8100/docs |
| PostgreSQL | `localhost:5433` |

To use an existing Neon database, put its pooled `DATABASE_URL` in
`backend/.env` and skip the local database command. See
[DEPLOY-neon.md](DEPLOY-neon.md) for production guidance.

## How the memory engine works

### Formation

1. Content is deduplicated by hash.
2. Documents are split into evidence-sized chunks and embedded.
3. A bounded worker queue processes documents sequentially per workspace while
   allowing different workspaces to run in parallel.
4. The selected LLM extracts decisions, reasoning, alternatives, people,
   topics, open questions, and conflicts.
5. Memory nodes are linked to evidence chunks and to one another through typed
   graph edges.
6. Near-duplicate decisions are consolidated by embedding similarity.

```mermaid
sequenceDiagram
    participant Src as Source: Slack, GitHub, Jira, Upload
    participant Ingest as ingestion.ingest_document
    participant DB as Postgres: documents, chunks
    participant Queue as worker._loop / _claim
    participant Form as formation.run_formation
    participant LLM as LLM provider
    participant Graph as graph.upsert_node / add_edge
    participant Cons as consolidate.merge_similar_decisions

    Src->>Ingest: raw content
    Ingest->>Ingest: content_hash dedup check
    alt duplicate - hash or external_ref match
        Ingest-->>Src: existing document_id, duplicate=true
    else new document
        Ingest->>Ingest: chunk_text - 900-1500 char, paragraph-aware
        Ingest->>DB: embed_texts chunks, INSERT document + chunks
        Ingest->>Queue: schedule_formation doc_id
    end
    rect rgba(37,99,235,0.06)
    loop worker loop - 1 doc per workspace in flight, N workspaces parallel
        Queue->>DB: claim pending doc - FOR UPDATE SKIP LOCKED
        Queue->>Form: run_formation doc_id
        Form->>DB: fetch chunks + existing-memory digest - recency + relevance blend, cap 150
        Form->>LLM: structured_call - FORMATION_SYSTEM, FORMATION_SCHEMA
        LLM-->>Form: decisions, entities, questions, links
        Form->>Graph: upsert_node / add_edge / link_chunk per item
        Form->>DB: formation_status = complete, store context_summary
        Form->>Cons: touched decision node ids
        Cons->>Cons: embed signatures - label + summary
        Cons->>Cons: cosine similarity above MERGE_SIM_THRESHOLD, about 0.86?
        Cons->>Graph: merge_nodes keep_id, drop_id
    end
    end
    Note over Queue,DB: on failure - exponential backoff, 2 to the n times FORMATION_BACKOFF_S,<br/>max FORMATION_MAX_ATTEMPTS then formation_status=failed.<br/>Stuck processing rows reclaimed on worker restart.
```

### Retrieval

1. Short follow-ups are rewritten into standalone questions.
2. Vector and PostgreSQL full-text results are fused with reciprocal-rank
   fusion.
3. Relevant memory nodes expand through the graph for up to two hops.
4. Evidence is ranked by relevance and memory confidence.
5. The LLM reasons over the evidence, graph context, and conversation history.
6. The API streams answer tokens and structured metadata over SSE.

```mermaid
sequenceDiagram
    participant UI as React SPA
    participant API as POST /api/query
    participant RW as rewrite_followup
    participant Ret as retrieval.retrieve
    participant DB as Postgres + pgvector
    participant Graph as graph.expand - BFS, hops=2
    participant LLM as LLM provider
    participant SSE as SSE stream

    UI->>API: question + chat history
    rect rgba(217,119,6,0.08)
    opt short follow-up under 100 chars, history exists
        API->>RW: resolve pronouns / ellipsis
        RW-->>API: standalone search question
    end
    end
    API->>Ret: retrieve - question, workspace_id
    par vector + full text
        Ret->>DB: pgvector cosine top-K
        Ret->>DB: websearch_to_tsquery top-K
    end
    Ret->>Ret: reciprocal rank fusion - RRF, k=60
    Ret->>Graph: expand from seed nodes
    Graph-->>Ret: related nodes + edges - relation-priority order, topic fanout capped
    Ret->>Ret: rank evidence = node confidence x similarity, floor 0.5
    Ret-->>API: chunks + nodes + edges + retrieval trace
    API->>API: build_context - graph summary + chronological chunks + history
    API->>LLM: stream_text - QUERY_SYSTEM, context
    LLM-->>SSE: streamed markdown, holdback buffer strips partial delimiters
    SSE-->>UI: event: delta - answer tokens
    API->>API: parse trailing JSON metadata block
    API->>API: locate_quote - exact citation spans in source chunks
    API-->>SSE: event: metadata - citations, confidence, timeline, counter-evidence
    SSE-->>UI: event: done
```

## Configuration

YBase reads `backend/.env` at startup. Real environment variables take
precedence, and secrets should never be committed.

### LLM providers

Set `LLM_PROVIDER` to `auto`, `anthropic`, `nvidia`, or `ollama`.

| Provider | Activates when `auto` | Default model | Notes |
|---|---|---|---|
| Anthropic | Anthropic credentials are available | `claude-fable-5` | Streaming, adaptive thinking, structured extraction |
| NVIDIA NIM | `NVIDIA_API_KEY` is set | `openai/gpt-oss-120b` | OpenAI-compatible chat completions |
| Ollama | Fallback | `qwen3.5` | Fully local inference |

### Embedding providers

Set `EMBED_PROVIDER` to `auto`, `voyage`, `ollama`, or `local`.

| Provider | Activates when `auto` | Model | Notes |
|---|---|---|---|
| Voyage AI | `VOYAGE_API_KEY` is set | `voyage-3-lite` | Hosted 512-dimensional embeddings |
| Ollama | Ollama is reachable | `nomic-embed-text` | Local Matryoshka embeddings, normalized to 512 dimensions |
| Local hash | Fallback | `local-hash` | Deterministic, lexical, demo-grade fallback |

Embedding spaces must not be mixed. After changing providers or embedding
models, rebuild stored vectors:

```bash
backend/.venv/bin/python scripts/reembed.py
```

### Provider routing

Both `auto` chains prefer a hosted provider when credentials are present and
degrade gracefully to fully local inference otherwise:

```mermaid
flowchart TD
  LP{"LLM_PROVIDER"}:::gray
  LP -->|explicit: anthropic / nvidia / ollama| LPuse["use configured provider"]:::blue
  LP -->|auto| LP1{"Anthropic credentials present?"}:::gray
  LP1 -->|yes| LPa["Anthropic — claude-fable-5<br/>adaptive thinking, structured output"]:::green
  LP1 -->|no| LP2{"NVIDIA_API_KEY set?"}:::gray
  LP2 -->|yes| LPn["NVIDIA NIM — openai/gpt-oss-120b<br/>OpenAI-compatible completions"]:::blue
  LP2 -->|no| LPo["Ollama — qwen3.5<br/>fully local inference"]:::amber

  EP{"EMBED_PROVIDER"}:::gray
  EP -->|explicit| EPuse["use configured provider"]:::blue
  EP -->|auto| EP1{"VOYAGE_API_KEY set?"}:::gray
  EP1 -->|yes| EPv["Voyage — voyage-3-lite, 512d<br/>hosted embeddings"]:::green
  EP1 -->|no| EP2{"Ollama reachable?"}:::gray
  EP2 -->|yes| EPo["Ollama — nomic-embed-text<br/>Matryoshka, normalized to 512d"]:::blue
  EP2 -->|no| EPl["Local hash embedder<br/>deterministic, demo-grade fallback"]:::amber

  classDef blue   fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:1.4px;
  classDef green  fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:1.4px;
  classDef amber  fill:#fef3c7,stroke:#d97706,color:#78350f,stroke-width:1.4px;
  classDef gray   fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.4px;
```

### Important environment variables

| Variable | Purpose | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL connection string | Local Compose database |
| `LLM_PROVIDER` | `auto`, `anthropic`, `nvidia`, or `ollama` | `auto` |
| `EMBED_PROVIDER` | `auto`, `voyage`, `ollama`, or `local` | `auto` |
| `CONNECTOR_SECRET_KEY` | Encrypts OAuth tokens at rest | Required for connectors |
| `ALLOW_PUBLIC_SIGNUP` | Enables public workspace registration | `true` |
| `SEED_DEMO_ON_SIGNUP` | Seeds new workspaces with demo memory | `true` |
| `SESSION_COOKIE_SECURE` | Sends session cookies over HTTPS only | `false` |
| `CORS_ORIGINS` | Allowed browser origins | Local Vite origins |
| `DB_POOL_MAX_SIZE` | Per-process database connection limit | `20` |
| `FORMATION_CONCURRENCY` | Parallel workspace formation workers | Auto |
| `SENTRY_DSN` | Enables Sentry error reporting | Disabled |

See [.env.example](.env.example) and
[backend/.env.example](backend/.env.example) for the full configuration surface.

## Integrations

YBase currently supports:

- **Slack** — OAuth, public-channel selection, Events API ingestion, periodic
  reconciliation, and historical backfill.
- **GitHub** — OAuth, repository selection, issue ingestion, and periodic
  resynchronization.
- **Jira** — Atlassian OAuth 3LO, project selection, issue ingestion, and
  periodic resynchronization.
- **Direct documents** — paste or upload content through the UI or API.
- **Slack exports** — import an unzipped workspace export without OAuth.

Connector credentials are encrypted using `CONNECTOR_SECRET_KEY`. Source-level
ACLs, private Slack channels, DMs, files, and attachments are intentionally
outside the current product boundary.

Slack has a live path on top of periodic reconciliation; Jira and GitHub have
no realtime API, so they rely purely on scheduled polling. All three converge
on the same dedup-and-queue ingestion path:

```mermaid
flowchart TB
  subgraph SlackFlow["Slack"]
    S1["OAuth install<br/>oauth.v2.access"]:::blue
    S2["Events API webhook<br/>live messages, HMAC verified"]:::green
    S3["Quiet-thread rollup<br/>SLACK_THREAD_QUIET_S"]:::green
    S4["Periodic reconcile<br/>SLACK_RECONCILE_INTERVAL_S<br/>(covers missed webhook deliveries)"]:::amber
    S1 --> S2 --> S3
    S1 --> S4
  end
  subgraph JiraFlow["Jira"]
    J1["OAuth 3LO<br/>access + refresh token"]:::blue
    J2["Initial backfill<br/>CONNECTOR_BACKFILL_DAYS<br/>(fast slice first, then full)"]:::amber
    J3["Periodic resync<br/>CONNECTOR_RESYNC_INTERVAL_S"]:::amber
    J1 --> J2 --> J3
  end
  subgraph GitHubFlow["GitHub"]
    G1["OAuth App<br/>non-expiring token"]:::blue
    G2["Initial backfill<br/>fast slice then full"]:::amber
    G3["Periodic resync"]:::amber
    G1 --> G2 --> G3
  end
  Ingest["ingest_document()<br/>dedup by content_hash / external_ref"]:::purple
  Queue["Formation queue"]:::purple

  S3 --> Ingest
  S4 --> Ingest
  J3 --> Ingest
  G3 --> Ingest
  Ingest --> Queue

  classDef blue   fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:1.4px;
  classDef green  fill:#dcfce7,stroke:#16a34a,color:#14532d,stroke-width:1.4px;
  classDef amber  fill:#fef3c7,stroke:#d97706,color:#78350f,stroke-width:1.4px;
  classDef purple fill:#f3e8ff,stroke:#9333ea,color:#581c87,stroke-width:1.4px;
```

## Product surfaces

| Surface | Purpose |
|---|---|
| **Home** | Activity, recent decisions, open questions, digests, and relitigation alerts |
| **Ask memory** | Cited streaming chat with follow-ups, confidence, timelines, and retrieval traces |
| **Timeline** | Chronological documents, decisions, questions, and revisits |
| **Decision log** | Rationale, alternatives, participants, status, evidence, and lineage |
| **People** | Decisions, positions, questions, and documents connected to each person |
| **Graph** | Interactive exploration of the typed memory graph |
| **Sources** | OAuth connections, stream selection, backfills, and sync status |
| **Review** | Admin curation, editing, validation, archive, and restore |
| **Feedback** | Trust and answer-quality triage |
| **Ops** | Readiness checks, queue health, retries, recovery, and demo seeding |
| **Settings** | Workspace members, roles, account, and billing |

## API

FastAPI publishes the complete interactive contract at `/docs`. The main route
groups are:

| Route group | Responsibility |
|---|---|
| `/api/auth/*` | Bootstrap, registration, login, OAuth, sessions, and password reset |
| `/api/workspace/*` | Workspace setup, members, invites, roles, and ownership |
| `/api/sources/*` | Connector OAuth, streams, sync jobs, retries, and removal |
| `/api/ingest`, `/api/documents/*` | Ingestion, document status, reform, and relinking |
| `/api/query` | SSE answer stream with status, deltas, metadata, and completion |
| `/api/decisions`, `/api/timeline`, `/api/graph` | Read models over organizational memory |
| `/api/memory-review/*` | Admin review and curation |
| `/api/answer-feedback/*` | Member feedback and admin resolution |
| `/api/analytics/*` | Activation, engagement, and memory-quality metrics |
| `/api/ops/*` | Readiness, recovery, queue inspection, and demo operations |
| `/api/health*` | Database, provider, embedding, and queue health |

## Data model

`workspace_id` scopes nearly every table for multi-tenancy. The typed memory
graph (`memory_nodes`, `memory_edges`, `chunk_links`) sits alongside
document/chunk storage, connector sync state, and chat/feedback/digest
tables:

```mermaid
erDiagram
  workspaces ||--o{ workspace_memberships : has
  users ||--o{ workspace_memberships : has
  workspaces ||--o{ auth_sessions : scopes
  users ||--o{ auth_sessions : has
  workspaces ||--o{ workspace_invites : issues
  workspaces ||--o{ documents : owns
  documents ||--o{ chunks : "splits into"
  chunks ||--o{ chunk_links : evidences
  memory_nodes ||--o{ chunk_links : "evidenced by"
  workspaces ||--o{ memory_nodes : owns
  memory_nodes ||--o{ memory_edges : src
  memory_nodes ||--o{ memory_edges : dst
  memory_nodes ||--o{ decision_shares : "shared as"
  workspaces ||--o{ source_connections : has
  source_connections ||--o{ source_streams : has
  source_connections ||--o{ sync_jobs : has
  source_connections ||--o{ slack_events : buffers
  workspaces ||--o{ chat_sessions : has
  users ||--o{ chat_sessions : owns
  chat_sessions ||--o{ chat_messages : has
  chat_messages ||--o{ answer_feedback : receives
  workspaces ||--o{ digests : generates
  workspaces ||--o{ audit_events : logs

  workspaces {
    int id PK
    text slug UK
    text plan
    text plan_status
    timestamptz trial_ends_at
  }
  users {
    int id PK
    text email UK
    text password_hash
    text auth_provider
    text google_sub UK
  }
  workspace_memberships {
    int workspace_id FK
    int user_id FK
    text role
  }
  auth_sessions {
    int id PK
    int user_id FK
    text token_hash UK
    timestamptz expires_at
    timestamptz revoked_at
  }
  documents {
    int id PK
    int workspace_id FK
    text source
    text content_hash
    text formation_status
    int formation_attempts
    text external_ref
  }
  chunks {
    int id PK
    int document_id FK
    int chunk_index
    vector embedding
    text embed_model
    tsvector text_tsv
  }
  memory_nodes {
    int id PK
    int workspace_id FK
    text kind
    text label
    text status
    jsonb data
    vector embedding
    timestamptz archived_at
  }
  memory_edges {
    int id PK
    int workspace_id FK
    int src FK
    int dst FK
    text relation
  }
  chunk_links {
    int chunk_id FK
    int node_id FK
    text relation
  }
  source_connections {
    int id PK
    text provider
    text access_token_enc
    text refresh_token_enc
    timestamptz last_sync_at
  }
  source_streams {
    int id PK
    int connection_id FK
    text external_id
    boolean selected
    jsonb sync_cursor
  }
  sync_jobs {
    int id PK
    int connection_id FK
    text status
    text kind
    jsonb stats
  }
  slack_events {
    int id PK
    text channel
    text thread_key
    boolean consumed
  }
  chat_sessions {
    int id PK
    int user_id FK
    text title
  }
  chat_messages {
    int id PK
    int session_id FK
    text role
    jsonb meta
  }
  answer_feedback {
    int id PK
    int chat_message_id FK
    text issue_type
    text status
  }
  decision_shares {
    int id PK
    int node_id FK
    text token UK
    int view_count
  }
  digests {
    int id PK
    timestamptz period_start
    jsonb payload
  }
  workspace_invites {
    int id PK
    text token_hash UK
    text role
    timestamptz expires_at
  }
  audit_events {
    int id PK
    text action
    text target_type
    jsonb data
  }
```

Full schema in [schema.sql](backend/app/core/schema.sql) plus numbered
migrations in [backend/app/core/migrations/](backend/app/core/migrations/).

## Project structure

```text
YBase/
├── backend/
│   ├── app/
│   │   ├── api/                 # Router assembly and route compatibility aliases
│   │   ├── core/                # Config, DB, migrations, crypto, mail, observability
│   │   ├── domains/             # Product capabilities grouped by domain
│   │   │   ├── auth/
│   │   │   ├── connectors/
│   │   │   ├── documents/
│   │   │   ├── memory/
│   │   │   ├── query/
│   │   │   └── ...
│   │   ├── providers/           # LLM and embedding adapters
│   │   └── main.py              # FastAPI lifecycle and application assembly
│   ├── tests/                   # Pure, API, connector, graph, and worker tests
│   └── requirements*.txt
├── frontend/
│   ├── public/                  # Static brand assets
│   └── src/                     # React application, views, components, design system
├── scripts/                     # Demo, imports, evaluations, and re-embedding
├── docker-compose.yml           # Local pgvector and optional full-stack profile
├── Dockerfile                   # Multi-stage UI + API production image
└── fly.toml                     # Fly.io deployment configuration
```

## Development

### Backend tests

The test suite expects a PostgreSQL server with pgvector on port `5433`.

```bash
docker compose up -d db
cd backend
.venv/bin/pip install -r requirements-dev.txt
PYTHONPATH=. .venv/bin/python -m pytest
```

### Frontend build

```bash
cd frontend
npm ci
npm run build
```

### Memory-quality evaluation

Run the evaluator after changing formation prompts, models, retrieval, scoring,
or consolidation:

```bash
backend/.venv/bin/python scripts/eval.py
```

The GitHub Actions workflow runs the backend suite against pgvector/PostgreSQL
and builds the frontend on every push and pull request.

## Demo and data import

Authenticate as an owner or admin, then:

```bash
export YBASE_EMAIL=owner@example.com
export YBASE_PASSWORD='your-password'

# Seed documents and run representative questions
backend/.venv/bin/python scripts/demo.py

# Preview and import an offline Slack export
backend/.venv/bin/python scripts/import_slack.py /path/to/export \
  --channel engineering --since 2025-01-01 --limit 20 --dry-run
```

The in-product **Ops** surface can also seed a compact demo corpus and expose
formation or sync failures without using the CLI.

## Deployment

| Target | Path |
|---|---|
| Docker | `docker compose --profile app up -d --build` |
| Fly.io | [fly.toml](fly.toml) |
| Neon Postgres | [DEPLOY-neon.md](DEPLOY-neon.md) |

Production deployments should use HTTPS, set `SESSION_COOKIE_SECURE=true`,
restrict `CORS_ORIGINS`, configure a stable `CONNECTOR_SECRET_KEY`, and use a
pooled PostgreSQL connection string.

```mermaid
flowchart TB
  subgraph FlyIO["Fly.io — primary_region iad"]
    VM["Machine: 1 shared CPU, 2GB RAM<br/>always-on, min_machines_running=1"]:::blue
    subgraph Container["Docker image"]
      UIbuild["Vite build<br/>(node:20-alpine stage)"]:::gray
      API2["FastAPI — python:3.12-slim<br/>serves static/ + /api/*"]:::blue
    end
    VM --> Container
  end
  Neon[(Neon Postgres<br/>pgvector, pooled DATABASE_URL)]:::purple
  Anthropic{{"Anthropic API"}}:::purple
  Voyage{{"Voyage AI"}}:::purple
  Resend{{"Resend (email)"}}:::amber
  GH["GitHub Actions CI<br/>pytest against pgvector + npm build"]:::gray

  UIbuild -->|dist/ copied into image as static| API2
  API2 <-->|DATABASE_URL secret| Neon
  API2 --> Anthropic
  API2 --> Voyage
  API2 --> Resend
  GH -.->|on push / PR| Container

  classDef blue   fill:#dbeafe,stroke:#2563eb,color:#1e3a8a,stroke-width:1.4px;
  classDef purple fill:#f3e8ff,stroke:#9333ea,color:#581c87,stroke-width:1.4px;
  classDef amber  fill:#fef3c7,stroke:#d97706,color:#78350f,stroke-width:1.4px;
  classDef gray   fill:#f1f5f9,stroke:#64748b,color:#1e293b,stroke-width:1.4px;
```

## Full architecture documentation

This README covers the highlights. [docs/architecture.html](docs/architecture.html)
is a self-contained documentation site — no server or internet connection
required, just clone the repo and open the file in a browser — with all of
the diagrams above plus the ones that don't fit here: the domain module map,
the formation job-queue state machine, the auth/RBAC/billing request
lifecycle, and the system-context view. Light/dark themes, search, and
one-click SVG/PNG export for every diagram.

## Current boundaries

- The graph uses PostgreSQL adjacency tables; no separate graph database is
  required at the current scale.
- Access control is workspace-scoped, not per document or per source.
- Slack v1 focuses on selected public channels; private channels and DMs are not
  ingested.
- Local hash embeddings are for evaluation and demos, not production semantic
  retrieval.
- Formation quality and latency depend on the selected model and available
  hardware.

## License

Copyright © 2026 YBase. All rights reserved.

This repository is proprietary software. No license is granted for use,
copying, modification, or distribution without prior written permission.
