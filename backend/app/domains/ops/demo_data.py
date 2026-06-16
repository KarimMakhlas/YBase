"""Demo seed corpus and seeding helper.

Kept dependency-light (ingest + db only, no auth) so it can be reused by both
the admin ``/api/ops/demo-seed`` route and the public signup flow without a
circular import. Callers that want an audit row write it themselves.
"""

from typing import Any, Dict, List

from app.domains.documents.ingestion import IngestRequest, ingest_document

DEMO_QUESTIONS = [
    "Why did we choose Postgres over MongoDB?",
    "Was the Postgres decision ever revisited?",
    "What open questions do we have about database scaling?",
    "Who advocated for which database choices?",
]

DEMO_DOCS: List[Dict[str, Any]] = [
    {
        "source": "slack",
        "title": "#engineering - database choice for v1",
        "author": "Maya Chen",
        "created_at": "2025-09-12T15:42:00Z",
        "tags": ["database", "architecture", "demo"],
        "text": """Maya Chen [09:42]
We need to settle the database question before we write more persistence code. I propose PostgreSQL as the primary datastore for v1.

Dev Patel [09:47]
I would push back. MongoDB would move faster for our activity feed because event payloads vary a lot.

Maya Chen [09:53]
Billing and subscriptions are relational: accounts, plans, invoices, usage records. We need real transactions and joins. The team also has production Postgres experience and no one has run MongoDB at scale here.

Priya Raghavan [10:01]
Postgres also gives us JSONB for flexible payloads and pgvector later if product wants semantic search. One datastore means one backup, one permissions model, and one operational path.

Dev Patel [10:08]
Fair. I still want us to revisit if the activity feed becomes painful.

Maya Chen [10:12]
Decision: PostgreSQL as the primary database for v1. Reasons: transactional integrity for billing, joins for relational product data, team operational experience, and JSONB plus pgvector as escape hatches. We will revisit only if the activity feed becomes a real problem.""",
    },
    {
        "source": "notion",
        "title": "Data Layer Architecture",
        "author": "Maya Chen",
        "created_at": "2025-09-20T11:00:00Z",
        "tags": ["database", "architecture", "demo"],
        "text": """# Data Layer Architecture

Status: Accepted 2025-09-20. Owner: Maya Chen.

## Decision
PostgreSQL 16 is our primary datastore for v1. All services read and write through the platform schema.

## Rationale
- Billing, subscriptions, accounts, plans, invoices, and usage records need ACID transactions and foreign keys.
- Most of engineering has operated Postgres in production; nobody has operated MongoDB in production.
- JSONB covers heterogeneous product payloads where needed.
- pgvector lets us keep semantic search in the same database if the scale remains modest.

## Alternatives considered
MongoDB was championed by Dev Patel for schema flexibility and activity-feed writes. We rejected it for v1 because the operational cost and transactional risk outweighed the flexibility benefit.""",
    },
    {
        "source": "jira",
        "title": "PLAT-214: Evaluate moving the activity feed to MongoDB",
        "author": "Dev Patel",
        "created_at": "2026-01-15T09:30:00Z",
        "tags": ["database", "activity-feed", "demo"],
        "text": """PLAT-214 - Evaluate moving the activity feed to MongoDB
Reporter: Dev Patel. Status: Closed - Won't Do.

Context: When we chose Postgres in September 2025, I flagged the activity feed as the workload that might make us regret it. We agreed to revisit if it hurt.

Evaluation:
- The current events table has too many nullable columns for different event shapes.
- A MongoDB prototype was 15 percent faster for writes but introduced a second datastore, dual-write complexity, and a separate backup/restore path.
- A JSONB events table in Postgres sustained 9.8k writes/sec in benchmarks, about six times current peak.

Outcome: Keep PostgreSQL. Migrate the activity feed to a single JSONB activity_events table with a GIN index. The original Postgres decision is reaffirmed; revisit only if sustained writes exceed roughly 30k/sec.""",
    },
    {
        "source": "meeting",
        "title": "Q1 architecture review - meeting notes",
        "author": "Priya Raghavan",
        "created_at": "2026-02-10T17:00:00Z",
        "tags": ["architecture", "database", "scaling", "demo"],
        "text": """Q1 Architecture Review - 2026-02-10
Present: Maya Chen, Dev Patel, Priya Raghavan, Tom Okafor, Sam Liu.

1. Datastore health check
Postgres is holding up well after the JSONB activity feed migration. Feed write load is fine: peak 1.8k/sec versus 9.8k/sec benchmarked. Decision recorded: Postgres as primary database is reaffirmed for 2026; no datastore changes planned this year.

2. Scaling question
Sam raised a new unresolved question: growth projections put us near 10M users by Q4. Single-writer Postgres may become a bottleneck, especially connection counts and write throughput on activity_events. Options to investigate: read replicas, partitioning activity_events by workspace, or Citus-style sharding.

Action: Priya owns the scaling investigation and will report back by end of Q2. This is explicitly an open question, not a decision.""",
    },
]


async def seed_demo_data(workspace_id: int) -> Dict[str, Any]:
    """Ingest the demo corpus into a workspace. Idempotent: re-ingesting the
    same docs dedups by content hash. Returns a summary; the caller is
    responsible for writing any audit row."""
    created = 0
    duplicates = 0
    document_ids: List[int] = []
    for doc in DEMO_DOCS:
        doc_id, duplicate = await ingest_document(
            IngestRequest(**doc),
            workspace_id=workspace_id,
        )
        document_ids.append(doc_id)
        if duplicate:
            duplicates += 1
        else:
            created += 1
    return {
        "created": created,
        "duplicates": duplicates,
        "document_ids": document_ids,
        "questions": DEMO_QUESTIONS,
    }
