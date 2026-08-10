# YBase Memory System Hardening Design

**Date:** 2026-08-10
**Status:** Approved direction; written specification awaiting final review
**Scope:** Document ingestion, memory formation, semantic retrieval, answer grounding,
concurrency, and Neon-backed scaling

## 1. Purpose

YBase must support both of these operating profiles without weakening tenant
isolation or answer quality:

1. Many concurrently active workspaces with modest corpora.
2. A smaller number of workspaces with 100,000 or more documents and sustained
   connector backfills.

The system already has valuable foundations: durable documents and chunks,
pgvector and PostgreSQL full-text retrieval, evidence links, a typed memory
graph, formation retries, workspace serialization, citations, curation, and
quality telemetry. This design preserves those foundations while correcting
the lifecycle and scaling weaknesses identified in the architecture review.

The governing principle is:

> Evidence is immutable and versioned; interpretations are reproducible;
> canonical memory is derived; retrieval quality is measured; mutations are
> reversible.

## 2. Goals

- Keep PostgreSQL/Neon as the transactional source of truth.
- Make vector retrieval tenant-safe and measurable before increasing traffic.
- Preserve changes to connected source objects instead of treating them as
  permanent duplicates.
- Make ingestion idempotent under concurrency.
- Make formation re-runnable without accumulating stale nodes or edges.
- Separate source observations, dated memory events, and current memory state.
- Increase formation throughput by parallelizing immutable work while keeping
  canonical graph commits ordered within a workspace.
- Improve retrieval recall with wider candidates, reranking, query-aware graph
  traversal, and answer verification.
- Support zero-downtime embedding-model migrations.
- Enforce quality, latency, cost, and fairness budgets in CI and production.

## 3. Non-goals

- Replacing Neon, pgvector, or PostgreSQL in the first implementation stages.
- Introducing a graph database solely because YBase has graph-shaped data.
- Allowing multiple workers to mutate one workspace's canonical memory
  concurrently.
- Automatically merging ambiguous memory nodes based only on one similarity
  threshold.
- Rebuilding every connector in the first subproject.
- Sending more retrieved content to the answer model without reranking it.

## 4. Current risks to correct

### 4.1 Tenant-filtered approximate retrieval

Chunk vectors are indexed globally while workspace ownership is reached through
the `documents` join. Approximate nearest-neighbor filtering can under-return
workspace results. The risk grows as the global corpus grows and any one
workspace represents a smaller share of it.

### 4.2 Source freshness

Stable connector `external_ref` values currently cause an early duplicate
return even when source content has changed. Edited issues, pages, comments,
and documents can remain stale in YBase.

### 4.3 Concurrent duplicate ingestion

Deduplication is checked in application code before embedding and insertion.
The relevant database indexes are not uniqueness constraints, so concurrent
requests can insert duplicate documents.

### 4.4 Non-replacing reformation

Re-forming a document adds or updates memory but does not transactionally diff
and retire all earlier contributions from that document. Old interpretations
can remain active.

### 4.5 Direct probabilistic mutation

LLM extraction output is validated and then persisted directly into canonical
memory. This makes it difficult to compare extractor versions, roll back a bad
run, or explain which extraction established a field.

### 4.6 Current state and history are conflated

Revisiting a decision can update the existing canonical node rather than create
a dated event. Late ingestion of old documents can also overwrite newer state.

### 4.7 Unverified evidence fallback

When extraction produces no valid evidence index, persistence links the first
document chunk. This guarantees a foreign key, not actual evidentiary support.

### 4.8 Narrow candidate selection

Retrieval fuses small vector and lexical lists directly into a small seed set.
There is no final relevance reranker over the union of semantic, lexical, and
graph-derived candidates.

### 4.9 Coupled runtime roles

API traffic, connector scheduling, formation, consolidation, and periodic work
share one application runtime and connection pool. A continuously busy
formation queue can delay idle-path integration ticks.

### 4.10 Non-atomic embedding migration

The re-embedding script updates the active corpus in batches. Queries can see a
partial corpus during migration, and rollback requires another full migration.

## 5. Target architecture

```text
Source adapters
    -> source objects
    -> immutable document revisions
    -> source-aware parsing and chunks
    -> versioned embeddings
    -> directly searchable evidence
    -> parallel extraction observations
    -> validation / quarantine
    -> ordered workspace resolver
    -> canonical identities + dated memory events
    -> current-state and graph projections
    -> tenant-aware candidate retrieval
    -> exact reranking + relevance reranking
    -> evidence context
    -> cited answer
    -> claim/citation verification + deterministic confidence
```

Each arrow is a durable stage boundary. A downstream failure does not require
the upstream source fetch to be repeated unless its own output is absent or
invalid.

## 6. Core data model

### 6.1 Source objects and revisions

`source_objects` identifies the stable remote object:

- `workspace_id`
- `source_connection_id`
- `source_stream_id`
- `external_ref`
- `external_updated_at`
- `current_revision_id`
- `deleted_at`

`document_revisions` stores immutable normalized versions:

- `source_object_id`
- `revision_number`
- `content_hash`
- `raw_text`
- `normalizer_version`
- `source_created_at`
- `source_updated_at`
- processing status fields

Direct uploads without an external object still receive an immutable revision
and a caller-provided or server-generated idempotency key.

### 6.2 Chunks and embeddings

Chunks carry `workspace_id` directly and retain document/revision provenance.
They additionally store structural metadata such as section path, author,
source offsets, content type, and token count.

Embeddings move to a versioned relation keyed by `(chunk_id,
embedding_model_id)`. A workspace selects one active embedding model version.
New model vectors are built alongside old vectors and activated only after
coverage and retrieval-quality validation.

### 6.3 Formation runs and observations

Every extraction attempt produces a versioned `formation_run`. Its immutable
`memory_observations` contain proposed decisions, questions, entities, topics,
relations, evidence spans, effective dates, confidence, model identifier, and
prompt version.

Observations with missing or invalid evidence are quarantined. They do not
receive a fabricated first-chunk link and do not enter canonical memory until
repaired or curated.

Reformation creates a candidate run, validates it, then atomically makes the
new run active for that document revision and retires the prior run's
observations. Canonical projections are recomputed only for affected objects.

### 6.4 Canonical memory and events

Canonical nodes represent durable identities: a decision, question, person,
project, system, or topic. Dated events represent what happened to an identity
at a point in time.

For decisions, events include proposed, decided, revisited, reversed, and
reaffirmed. Current status is derived by deterministic chronological rules,
with curated overrides recorded separately and audited.

Every projected field and relationship must be traceable to active
observations or a curator action.

### 6.5 Reversible identity resolution

Embedding similarity produces merge candidates, not automatic authority.
Automatic merging is limited to high-confidence duplicates that also satisfy
scope and evidence checks. All merges record a reversible ledger containing
survivor, retired identity, evidence, resolver version, and curator state.

## 7. Processing and concurrency model

### 7.1 Durable ingestion acceptance

The ingest request validates and persists the source object/revision before
performing provider calls. It returns an accepted identifier. Background stages
advance the revision through normalization, chunking, embedding, searchability,
extraction, resolution, and completion.

### 7.2 Parallel and serialized formation stages

The expensive immutable stages may run concurrently within a workspace:

- parsing
- chunking
- embeddings
- section extraction
- candidate observation generation

Only the resolver and canonical projection commit are serialized per workspace.
They operate on versioned observations and hold the workspace lock for the
shortest practical time.

### 7.3 Workload isolation

API/query, connector sync, embedding, extraction, resolution, consolidation,
and maintenance jobs have separate concurrency budgets. Deployment processes
may initially share one codebase while selecting explicit runtime roles.

Scheduling is workspace-fair. Backfills cannot consume all query or formation
capacity, and large workspaces receive bounded parallel preprocessing without
blocking smaller workspaces indefinitely.

## 8. Retrieval design

### 8.1 Tenant-safe vector search

- Store `workspace_id` on chunks.
- Apply the workspace and active embedding-version filters directly in vector
  queries.
- Enable pgvector iterative HNSW scans when the deployed extension supports
  them.
- Over-fetch approximate candidates and exact-rerank within the workspace.
- Compare approximate results against exact-search ground truth in evaluation.
- Partition by stable workspace buckets when measured corpus size requires it;
  isolate exceptionally large tenants only when justified by measurements.

### 8.2 Candidate generation and selection

Candidate generation remains broader than the final context:

- dense/vector candidates
- PostgreSQL lexical candidates
- exact identifier and fuzzy-name candidates
- graph-derived candidates

The union is exact-reranked, then relevance-reranked. Final selection applies
source diversity, chronology, relationship priority, and a strict character or
token budget. The answer model receives the selected evidence, not the whole
candidate set.

### 8.3 Query-aware graph traversal

A lightweight intent classifier selects traversal priorities. Reversal queries
favor decision events and revisit relationships; people queries favor
involvement; unresolved-question queries favor questions and resolution
relationships. All traversal remains bounded.

### 8.4 Search product semantics

The UI and agent APIs expose two explicit modes:

- Locate: lexical/fuzzy known-item lookup.
- Explore/Ask: hybrid semantic evidence retrieval and reasoning.

## 9. Answer grounding

Answers continue to cite retrieved chunks. A post-generation verifier parses
factual claims, checks citation coverage and entailment, and removes or retries
unsupported claims according to a bounded policy.

Confidence is computed from retrieval coverage, score separation, independent
supporting sources, evidence freshness, contradiction state, curation state,
and verification results. The LLM may explain confidence but does not set the
authoritative value by itself.

Negative user feedback is classified into source freshness, parsing, chunking,
retrieval, graph, ranking, canonical-memory, and answer-generation failures.
Confirmed cases become regression examples.

## 10. Failure handling

- Every stage is idempotent and writes a durable status transition.
- Provider timeouts retry with bounded exponential backoff.
- Poisoned jobs enter a reviewable failed state rather than retry forever.
- Workspace locks guard only ordered projection commits.
- A partially built embedding version never becomes active.
- A candidate formation run never replaces the active run until validation and
  projection succeed transactionally.
- Connector deletions and permission loss are explicit states, not silent
  absence.
- Curated state is never overwritten silently by automated formation.

## 11. Security and tenancy

- Database constraints prevent cross-workspace chunk, observation, node, edge,
  and embedding references.
- Retrieval applies workspace scope before approximate candidate acceptance.
- Topic-scoped agent results are filtered before answer generation, as they are
  today, and receive regression tests for unlinked seed chunks.
- Source deletions, membership changes, and connector revocations propagate to
  searchable and projected data according to documented retention rules.

## 12. Quality and performance enforcement

### 12.1 Required metrics

- source update to accepted revision
- accepted revision to searchable chunks
- searchable chunks to active formed memory
- queue age and fairness by workspace
- embedding and extraction throughput
- formation retry, timeout, and quarantine rates
- retrieval p50/p95 latency
- query time to first token and completion
- connection-pool wait, database CPU, cache-hit rate, and I/O
- ANN recall against exact search
- retrieval recall@5/10 and mean reciprocal rank
- citation coverage and entailment precision
- answer correctness and helpful-feedback rate
- cost per document, formation, and answered query

### 12.2 Release gates

Changes to chunking, embeddings, prompts, extraction models, resolution,
indexes, graph traversal, ranking, or answer models must pass a fixed evaluation
suite. Approximate tenant retrieval must achieve at least 95% recall@10 against
exact tenant search on the fixed corpus. A release is blocked when it loses more
than two percentage points of retrieval recall or citation-entailment precision,
or increases retrieval p95 or per-query provider cost by more than 20%, unless
the change is explicitly accepted with a recorded trade-off. Model and prompt
changes use canaries and retain rollback metadata.

### 12.3 Load profiles

Every major stage is tested against both:

1. Many-workspace profile: concurrent small and medium corpora, interactive
   queries, connector sync, and fairness assertions.
2. Large-workspace profile: 100,000+ documents, sustained backfill, re-embed,
   formation backlog, and query recall under active writes.

## 13. Neon scaling policy

Neon compute is scaled from measured CPU, cache-hit, I/O, connection, and query
plan data. Increased compute is expected to improve database concurrency,
working-set cache, vector/full-text queries, and index builds. It is not treated
as a remedy for source freshness, LLM latency, serial graph mutation, or weak
retrieval design.

Read replicas may serve evaluation, analytics, and latency-tolerant retrieval
after freshness behavior is explicitly defined. The primary remains the source
for ordered commits and read-after-write-sensitive flows.

## 14. Delivery decomposition

This program is intentionally divided into independently testable subprojects.
Each receives its own implementation plan and review gate.

### Subproject 1: Retrieval correctness and measurement

- Add a migration-safe `workspace_id` path on chunks.
- Add tenant-aware vector-query helpers.
- Add iterative-scan capability detection and query-scoped configuration.
- Over-fetch and exact-rerank candidates.
- Add exact-versus-ANN recall evaluation and query-plan tests.
- Preserve the public retrieval response contract.

This is the first implementation plan because it reduces an existing
multi-tenant correctness risk without requiring the later memory-model schema.
It is complete when a multi-workspace integration corpus reliably returns ten
tenant-owned vector candidates when at least ten exist, reaches at least 95%
recall@10 against exact search, records the approximate-versus-exact evaluation,
and leaves existing query, agent, and topic-scope tests passing.

### Subproject 2: Versioned ingestion and idempotency

- Introduce source objects and immutable revisions.
- Enforce uniqueness in the database.
- Support updated and deleted connector objects.
- Make ingestion acceptance durable before embedding.
- Migrate connectors incrementally behind compatibility adapters.

### Subproject 3: Reproducible formation

- Introduce immutable formation runs and observations.
- Quarantine invalid evidence.
- Make reform activate a replacement run.
- Preserve existing memory views through projection adapters.

### Subproject 4: Memory events and reversible resolution

- Add dated decision/question events.
- Derive current state chronologically.
- Embed all linkable memory kinds.
- Replace destructive similarity-only consolidation with a reversible resolver.

### Subproject 5: Worker isolation and concurrency

- Add explicit runtime roles and queue budgets.
- Separate connector scheduling from worker idle time.
- Parallelize preprocessing/extraction.
- Serialize only workspace resolution commits.
- Add workspace-fair scheduling and load tests.

### Subproject 6: Retrieval quality and answer verification

- Broaden candidate generation.
- Add relevance reranking and query-aware traversal.
- Add claim/citation verification and deterministic confidence.
- Convert feedback into regression cases.

### Subproject 7: Versioned embeddings and infrastructure scaling

- Add side-by-side embedding versions and atomic activation.
- Support rollback and online index builds.
- Add partitioning/read-replica policies based on measured thresholds.
- Document Neon sizing and operational playbooks.

## 15. Compatibility and rollout

- Existing `/api/query`, agent, document, decision, graph, and review contracts
  remain stable unless a subproject explicitly versions an endpoint.
- New schemas are introduced with dual-read or dual-write compatibility where
  needed; destructive cutovers require backfill verification.
- Existing documents, chunks, and memory remain readable throughout migrations.
- Every subproject includes a rollback path and migration coverage.
- User-visible behavior changes receive focused UI work only after backend
  contracts are stable.

## 16. Acceptance criteria for the program

- Concurrent identical ingests cannot create duplicate active revisions.
- Updated connector objects become searchable and formed without losing prior
  history.
- Re-forming a document cannot leave retired interpretations active.
- Every active automated memory field is traceable to active evidence-backed
  observations.
- Decision status is derived from dated events, not ingestion order.
- Approximate tenant retrieval meets the chosen recall budget against exact
  search in both load profiles.
- No production request silently uses the demo-grade hash embedder.
- Embedding migrations do not expose partial active corpora.
- Large-workspace preprocessing scales horizontally while canonical commits
  remain ordered.
- Model, prompt, retrieval, and indexing changes are blocked when evaluation
  budgets fail.
