"""Ten sample documents simulating Slack, Notion, GitHub, Jira and meeting notes.

The corpus is built so the database story spans sources and time:
Slack debate (Sep 2025) -> Notion architecture doc (Sep) -> pgvector RFC (Nov)
-> Jira near-reversal (Jan 2026) -> GitHub PR landing the compromise (Jan)
-> quarterly review reaffirming + opening a scaling question (Feb)
-> incident postmortem (Mar) -> sharding proposal (Apr, still open),
plus caching/rate-limiting docs as related-but-distinct memory.
"""

SAMPLE_DOCS = [
    {
        "source": "slack",
        "title": "#engineering — database choice for v1",
        "author": "Maya Chen",
        "created_at": "2025-09-12T15:42:00Z",
        "tags": ["database", "architecture"],
        "text": """Maya Chen [09:42]
Before we write any more persistence code we need to settle the database question. I want to propose PostgreSQL as our primary datastore.

Dev Patel [09:47]
I'd push back — MongoDB gets us moving faster. Our event payloads are heterogeneous and a flexible schema means we don't fight migrations every sprint. Mongo's horizontal scaling story is also better out of the box.

Maya Chen [09:53]
The billing and subscription model is inherently relational — accounts, plans, invoices, usage records. We'll need real transactions and joins there, and I don't want to hand-roll referential integrity in application code. Also: four of the six of us have run Postgres in production before; nobody here has operated a Mongo cluster.

Priya Raghavan [10:01]
+1 to Maya. Also worth noting Postgres gives us JSONB if we genuinely need schemaless fields, so the flexibility argument is partly covered. And pgvector exists if we ever do semantic search — keeps the stack to one database.

Dev Patel [10:08]
Fair points on billing and team experience. I still think we'll regret it for the activity feed, but I won't block. Can we agree to revisit if the feed becomes a problem?

Maya Chen [10:12]
Deal. Decision: PostgreSQL as the primary database for v1. Reasons for the record: (1) transactional integrity for billing, (2) joins for relational product data, (3) team operational experience, (4) JSONB + pgvector cover the flexible/vector cases. Dev's concern about the activity feed is noted — we revisit if it bites us. I'll write this up in Notion.""",
    },
    {
        "source": "notion",
        "title": "Data Layer Architecture",
        "author": "Maya Chen",
        "created_at": "2025-09-20T11:00:00Z",
        "tags": ["database", "architecture"],
        "text": """# Data Layer Architecture

Status: Accepted (2025-09-20). Owner: Maya Chen. Follows the #engineering discussion of 2025-09-12.

## Decision

PostgreSQL 16 is our primary and only datastore for v1. All services read and write through the platform schema.

## Rationale

- Billing and subscriptions are relational: accounts, plans, invoices, usage. We need ACID transactions and foreign keys, not application-level integrity checks.
- Team experience: most of engineering has operated Postgres in production; nobody has run MongoDB at scale. Operational familiarity beats theoretical scaling benefits at our size.
- Escape hatches exist inside Postgres: JSONB for heterogeneous payloads, pgvector for embeddings if search needs it. One database to operate, back up, and reason about.

## Alternatives considered

- MongoDB — championed by Dev Patel for schema flexibility and the activity feed. Rejected for v1 (no transactions story we trust for billing, zero operational experience), with an explicit agreement to revisit if the activity feed becomes painful.
- DynamoDB — briefly discussed, rejected: vendor lock-in and poor local dev story.

## Conventions

- Connection pooling through PgBouncer in transaction mode.
- Migrations via sqitch; every schema change is a reviewed PR.
- No service may open more than 10 direct connections.""",
    },
    {
        "source": "slack",
        "title": "#platform — caching layer",
        "author": "Tom Okafor",
        "created_at": "2025-10-03T09:15:00Z",
        "tags": ["caching", "performance"],
        "text": """Tom Okafor [09:15]
DB load from the dashboard endpoints is getting silly — 70% of queries are the same five aggregates. Proposal: put Redis in front as a read-through cache, 60s TTL on aggregates.

Maya Chen [09:22]
Fine with Redis, but let's be disciplined: cache only derived/aggregate data, never source-of-truth rows. Postgres stays canonical.

Tom Okafor [09:26]
Agreed. Also flagging an open question while we're here: what's our rate limiting story for the public API? We have nothing today and the scraper traffic is growing. Not urgent but someone should own it.

Dev Patel [09:31]
Decision then: Redis as a read-through cache for aggregates with short TTLs, Postgres remains source of truth. I'll take the rate limiting question to a ticket — needs a proper design.""",
    },
    {
        "source": "notion",
        "title": "RFC: Search infrastructure",
        "author": "Priya Raghavan",
        "created_at": "2025-11-08T14:30:00Z",
        "tags": ["search", "database", "embeddings"],
        "text": """# RFC: Search infrastructure

Author: Priya Raghavan. Status: Accepted 2025-11-08.

## Problem

Product wants semantic search over workspace content. We need a vector store for embeddings.

## Decision

Use pgvector inside our existing PostgreSQL instance rather than adopting a dedicated vector database.

## Reasoning

- We already chose Postgres as the single datastore (see "Data Layer Architecture", Sept 2025) precisely so we could lean on extensions like this. Adding Pinecone or Weaviate would mean a second stateful system to operate, monitor, back up, and pay for.
- Our scale is modest: low millions of vectors. pgvector with HNSW indexes is comfortably within range; benchmarks showed p95 < 40ms at 2M vectors.
- Joins matter: search results need to be filtered by workspace ACLs, which live in Postgres. Doing ACL filtering next to the vectors avoids a two-phase fetch.

## Alternatives considered

- Pinecone: best-in-class recall/latency, but a new vendor, network hop for every query, and ACL filtering becomes a join-across-systems problem.
- Weaviate self-hosted: another stateful cluster to run; the team is already stretched on ops.
- Elasticsearch kNN: we don't otherwise need ES; heavyweight for this.

## Risks

If vector volume grows past ~20M or we need sub-10ms recall at high QPS, revisit a dedicated store. Flagged as a known limitation, not a blocker.""",
    },
    {
        "source": "jira",
        "title": "ENG-77: Public API rate limiting design",
        "author": "Dev Patel",
        "created_at": "2025-12-01T10:00:00Z",
        "tags": ["api", "rate-limiting"],
        "text": """ENG-77 — Public API rate limiting design
Reporter: Dev Patel. Assignee: Tom Okafor. Status: Done (2025-12-01).

Context: Raised in #platform on Oct 3 — we had no rate limiting on the public API and scraper traffic was growing.

Investigation (Tom):
- Evaluated fixed-window counters (simple, bursty edges), sliding-window log (accurate, memory-heavy), and token bucket.
- Token bucket in Redis using Lua scripts: atomic, ~0.2ms per check against the cache cluster we already run, allows configurable burst.

Decision: token bucket in Redis, per API key, 100 req/min default with burst of 20. Implemented behind the gateway middleware. 429s include Retry-After.

Resolution note: this closes the open rate-limiting question from the October #platform thread. Redis was chosen partly because the caching decision (Oct 2025) already put it in our stack — no new infrastructure.""",
    },
    {
        "source": "jira",
        "title": "PLAT-214: Evaluate moving the activity feed to MongoDB",
        "author": "Dev Patel",
        "created_at": "2026-01-15T09:30:00Z",
        "tags": ["database", "activity-feed"],
        "text": """PLAT-214 — Evaluate moving the activity feed to MongoDB
Reporter: Dev Patel. Status: Closed — Won't Do (2026-01-15).

Context: When we picked Postgres (Sept 2025) I flagged the activity feed as the case where we might regret it, and we agreed to revisit if it hurt. It now hurts: feed writes are 40% of row volume, the events table has 11 nullable columns for different event shapes, and every new event type is a migration.

Proposal: move the activity feed (only the feed) to MongoDB. Keep billing and core product data in Postgres.

Evaluation (Dev + Maya, week of Jan 12):
- Benchmarked JSONB events table with a GIN index on payload: 9.8k writes/sec sustained on current hardware — 6x our peak. Read p95 12ms for feed pagination.
- Mongo prototype was ~15% faster on writes but adds: a second datastore to operate/back up, dual-write or CDC complexity for feed entries referencing relational rows, and we lose transactional insert of event + counters.
- Maya's position: the pain is schema-per-event-type, not Postgres itself. JSONB removes the migrations. Splitting the datastore for one workload reintroduces everything we avoided in September.
- Dev's position after benchmarks: agreed — numbers don't justify the operational cost. Withdrawing the proposal.

Outcome: KEEP PostgreSQL. Migrate the activity feed to a single JSONB `activity_events` table (PR to follow). The original decision stands, now reaffirmed with benchmark data. Revisit only if sustained writes exceed ~30k/sec.""",
    },
    {
        "source": "github",
        "title": "PR #482: activity feed on JSONB activity_events",
        "author": "Dev Patel",
        "created_at": "2026-01-22T16:20:00Z",
        "tags": ["database", "activity-feed"],
        "text": """PR #482 — feat(feed): migrate activity feed to JSONB activity_events table
Author: dev-patel. Merged 2026-01-22. Reviewers: maya-chen (approved), priya-r (approved).

Implements the outcome of PLAT-214: replaces the 11-nullable-column events table with a single `activity_events` table using a JSONB payload column and a GIN index.

Discussion highlights:

maya-chen: Nice — event type registry validating payload shape at the application layer means we keep schema discipline without migrations. This is the compromise we hoped for in the original database decision.

priya-r: Confirmed the GIN index covers feed filter queries; p95 under load test 12ms. Also added partial index per the hot event types.

dev-patel: For the record, closing the loop on my September objection — with JSONB landed I'm fully on the Postgres train. Benchmarks in PLAT-214 were convincing.

CI: green. Migration applied via sqitch change 2026-01-22-activity-events.""",
    },
    {
        "source": "meeting",
        "title": "Q1 architecture review — meeting notes",
        "author": "Priya Raghavan",
        "created_at": "2026-02-10T17:00:00Z",
        "tags": ["architecture", "database", "scaling"],
        "text": """Q1 Architecture Review — 2026-02-10
Present: Maya Chen, Dev Patel, Priya Raghavan, Tom Okafor, Sam Liu (new).

1. Datastore health check
Postgres holding up well post-JSONB migration. Feed write load fine (peak 1.8k/sec vs 9.8k benchmarked). Decision recorded: Postgres as primary database is REAFFIRMED for 2026; no datastore changes planned this year.

2. Scaling question (new, unresolved)
Sam raised: growth projections put us at ~10M users by Q4. Single-writer Postgres may become the bottleneck — connection counts and write throughput on the events table are the concerns. Options floated but not evaluated: read replicas first, Citus-style sharding, or partitioning activity_events by workspace.
ACTION: Priya to investigate sharding/partitioning options and report back by end of Q2. Explicitly an OPEN QUESTION — no decision yet.

3. Caching
Redis read-through cache working; hit rate 84%. Tom to add cache stampede protection (probabilistic early expiry) — small ticket, not a decision.

4. Misc
PgBouncer config drift between staging and prod flagged by Tom — see incident channel if it recurs.""",
    },
    {
        "source": "slack",
        "title": "#incidents — postmortem: connection pool exhaustion",
        "author": "Tom Okafor",
        "created_at": "2026-03-04T22:10:00Z",
        "tags": ["incident", "database", "postmortem"],
        "text": """Tom Okafor [22:10]
Postmortem for today's 23-minute partial outage (INC-31), writing it up here before the doc.

Timeline: 14:02 deploy of the export service shipped WITHOUT PgBouncer in front — it opened direct connections per worker. 14:19 Postgres hit max_connections (200), new sessions refused, dashboard + API 5xx. 14:25 rolled back, recovered 14:42.

Root cause: the "all services connect through PgBouncer" rule from the Data Layer Architecture doc was convention, not enforcement. New service templates don't include it by default.

Maya Chen [22:24]
Decision for the record: PgBouncer is now mandatory for every service — it goes into the service template, and CI fails any service whose DATABASE_URL doesn't point at the pooler. Also lowering per-service direct connection cap to zero except for migrations.

Sam Liu [22:31]
I'll do the template + CI check this week. Note this also feeds the scaling investigation — connection pressure is exactly what Priya's looking at for the 10M-user question.

Tom Okafor [22:35]
Action items: (1) Sam — template + CI enforcement, (2) Tom — alert at 60% of max_connections, (3) link this postmortem from the Data Layer doc.""",
    },
    {
        "source": "github",
        "title": "Issue #517: Proposal — partition activity_events and add read replicas",
        "author": "Priya Raghavan",
        "created_at": "2026-04-18T13:45:00Z",
        "tags": ["database", "scaling"],
        "text": """Issue #517 — Proposal: partition activity_events by workspace_id and introduce read replicas
Author: priya-r. Status: OPEN — RFC scheduled for Q2 review. Labels: architecture, needs-decision.

This is the follow-up assigned to me at the Q1 architecture review (10M-user scaling question). Sharing interim findings; NOT a decision yet.

Findings so far:
- Write throughput: current peak 1.8k/sec, projected 12k/sec at 10M users. Single primary handles ~25k/sec in synthetic tests after tuning — headroom exists, but margins shrink.
- Connection pressure is the nearer-term risk (see INC-31 postmortem). PgBouncer enforcement bought us room; replicas would buy more by moving read traffic off the primary.
- Recommendation forming: (1) add two read replicas for dashboard/feed reads in Q3, (2) declaratively partition activity_events by workspace_id hash now while the table is still manageable, (3) defer Citus/sharding — full sharding is a last resort, not a Q3 need.

Open questions for the RFC:
- Replication lag tolerance for the feed (product call needed — is 2s staleness acceptable?)
- Whether billing reads must stay on the primary (I believe yes — correctness over freshness).

maya-chen (comment, 2026-04-19): Direction looks right. Please make sure the RFC explicitly states this does NOT reopen the Postgres-vs-other-datastore question — we partition within Postgres. That decision is settled.""",
    },
    {
        "source": "notion",
        "title": "Onboarding: engineering team & ownership map",
        "author": "Sam Liu",
        "created_at": "2026-02-20T10:00:00Z",
        "tags": ["team", "onboarding"],
        "text": """# Engineering team & ownership map (Feb 2026)

A who-owns-what for new joiners. Update when ownership changes.

- Maya Chen — Staff engineer. Data layer, billing, schema/migration review. Wrote the Data Layer Architecture doc; final reviewer on anything touching Postgres.
- Dev Patel — Senior engineer. Activity feed, public API gateway. Authored the JSONB activity_events migration (PR #482) and the rate-limiting design (ENG-77).
- Priya Raghavan — Senior engineer. Search (pgvector), embeddings pipeline. Currently owns the database scaling investigation (issue #517), due Q2.
- Tom Okafor — Platform/SRE. Redis caching, PgBouncer, observability, incident process. Ran the INC-31 postmortem.
- Sam Liu — Engineer (joined Jan 2026). Service templates, CI enforcement of pooling rules. Raised the 10M-user scaling question at the Q1 review.

Standing conventions: all schema changes via sqitch PRs reviewed by Maya; all new services use the template (PgBouncer mandatory since INC-31); architecture decisions get a Notion doc and land in the #engineering channel first.""",
    },
]
