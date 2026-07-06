import os
import uuid
from pathlib import Path


def _load_dotenv() -> None:
    """Populate os.environ from backend/.env if present, without overriding
    variables already set in the real environment. Keeps secrets (connector
    OAuth keys, API keys) in one gitignored file instead of a long inline
    launch command. Intentionally dependency-free."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

# Postgres (pgvector). Default matches docker-compose.yml (host port 5433).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://ybase:ybase@localhost:5433/ybase",
)
# Connection pool sizing. The formation worker holds one connection per
# concurrent slot (FORMATION_CONCURRENCY, up to 3) and every in-flight HTTP
# request (including long-lived SSE query streams) holds one for its duration,
# so the old max_size=8 exhausted under a handful of concurrent users. max
# must stay comfortably below Postgres' own max_connections (managed Postgres
# tiers often allow only ~100-200, shared across all app instances).
DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", "2"))
DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", "20"))
# Per-statement timeout (seconds). Caps any single query so one slow/stuck
# statement can't hold a pooled connection indefinitely and starve the pool.
# 0 disables. Applies to individual statements, not whole requests, so it is
# safe for streaming endpoints (each fetch is short) and for formation (the
# long wait is the LLM call, not the surrounding DB statements).
DB_COMMAND_TIMEOUT_S = float(os.getenv("DB_COMMAND_TIMEOUT_S", "30"))

# Browser/API security. In production, set CORS_ORIGINS to the exact frontend
# origins and SESSION_COOKIE_SECURE=true behind HTTPS.
CORS_ORIGINS = [
    o.strip() for o in os.getenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",") if o.strip()
]
SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in (
    "1", "true", "yes", "on",
)
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "14"))
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "12"))
# Trusted real-client-IP header set by the platform's proxy (NOT forwardable by
# the client), used for rate-limit keying and session records. Set this in
# production to the header your host injects: "fly-client-ip" on Fly,
# "cf-connecting-ip" behind Cloudflare, "true-client-ip" on some CDNs. When
# empty, the rightmost X-Forwarded-For entry is used (correct for a single
# trusted proxy), falling back to the direct socket peer.
REAL_IP_HEADER = os.getenv("REAL_IP_HEADER", "")

# Public self-serve signup. When true (cloud/public beta) anyone can create a
# new workspace at /api/auth/register. Set false for single-tenant self-hosted
# deployments where only the bootstrap owner and invited teammates should exist.
ALLOW_PUBLIC_SIGNUP = os.getenv("ALLOW_PUBLIC_SIGNUP", "true").lower() in (
    "1", "true", "yes", "on",
)
# Pre-load a fresh workspace with the demo corpus on signup so the first visit
# has something to query (cold-start "wow moment"). Seeded in the background.
SEED_DEMO_ON_SIGNUP = os.getenv("SEED_DEMO_ON_SIGNUP", "true").lower() in (
    "1", "true", "yes", "on",
)
# How long a workspace invite link stays valid.
INVITE_TTL_DAYS = int(os.getenv("INVITE_TTL_DAYS", "14"))
LOGIN_MAX_FAILURES = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
LOGIN_WINDOW_MINUTES = int(os.getenv("LOGIN_WINDOW_MINUTES", "15"))
# How long a password-reset link stays valid.
PASSWORD_RESET_TTL_MINUTES = int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "60"))

# Per-user rate limits on the expensive endpoints (events per minute,
# 0 disables). Login has its own throttle (auth_login_attempts).
QUERY_RATE_PER_MINUTE = int(os.getenv("QUERY_RATE_PER_MINUTE", "20"))
INGEST_RATE_PER_MINUTE = int(os.getenv("INGEST_RATE_PER_MINUTE", "120"))
# Per-IP limit on the unauthenticated auth endpoints (register / login /
# forgot / reset), events per minute. 0 disables.
AUTH_RATE_PER_MINUTE = int(os.getenv("AUTH_RATE_PER_MINUTE", "10"))
# Per-API-key limit on the machine-facing agent endpoints (/api/agent/*).
# Higher than the human query limit — an agent may fan out several context
# lookups per task — but still bounds a runaway loop's LLM spend.
AGENT_RATE_PER_MINUTE = int(os.getenv("AGENT_RATE_PER_MINUTE", "60"))

# ── Observability & ops ─────────────────────────────────────────────────────
# Error tracking (Sentry). Empty DSN disables it entirely (local / dev /
# self-host installs are unaffected).
SENTRY_DSN = os.getenv("SENTRY_DSN", "")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "production")
# Performance-trace sample rate. 0.0 = capture errors only (no tracing
# overhead or extra event volume); raise toward 1.0 to sample request traces.
SENTRY_TRACES_SAMPLE_RATE = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0"))
# Log output: "text" (human-readable, default) or "json" (one JSON object per
# line, for log aggregators / queryable production logs).
LOG_FORMAT = os.getenv("LOG_FORMAT", "text")
# Optional shared secret guarding /api/health/formation for external uptime
# monitors. Empty = open, like /api/health.
HEALTH_TOKEN = os.getenv("HEALTH_TOKEN", "")

# Claude — memory formation, query reasoning, answer synthesis.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-fable-5")

# NVIDIA NIM/OpenAI-compatible chat completions.
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY") or os.getenv("NVIDIA_NIM_API_KEY", "")
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1")
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "openai/gpt-oss-120b")
NVIDIA_MAX_TOKENS = int(os.getenv("NVIDIA_MAX_TOKENS", "4096"))
NVIDIA_TEMPERATURE = float(os.getenv("NVIDIA_TEMPERATURE", "1"))
NVIDIA_TOP_P = float(os.getenv("NVIDIA_TOP_P", "1"))

# LLM provider: "anthropic" | "nvidia" | "ollama" | "auto" (auto = Anthropic
# when credentials are present, then NVIDIA when configured, otherwise Ollama).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "auto")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5")
OLLAMA_NUM_CTX = int(os.getenv("OLLAMA_NUM_CTX", "16384"))

# Embeddings provider: "auto" | "voyage" | "ollama" | "local" (hash fallback).
# auto = Voyage if VOYAGE_API_KEY, else Ollama if reachable, else local hash.
EMBED_PROVIDER = os.getenv("EMBED_PROVIDER", "auto")
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

# Optional: real embeddings via Voyage AI (voyage-3-lite, 512-dim).
# Without a key, Ollama nomic-embed-text is used when reachable; the
# deterministic local hashing embedder is the demo-grade last resort.
VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY", "")

EMBED_DIM = 512

# Retrieval knobs
TOP_K = int(os.getenv("TOP_K", "8"))                       # vector-search seeds
CONTEXT_CHUNK_CAP = int(os.getenv("CONTEXT_CHUNK_CAP", "22"))  # max chunks sent to Claude
# Total character budget across all context chunks. CONTEXT_CHUNK_CAP bounds
# the count; this bounds the size so a worst case (22 chunks × 1500 chars +
# graph nodes + history) still fits small local context windows
# (OLLAMA_NUM_CTX=16384 tokens ≈ 50k chars for system+context+answer, which
# Ollama otherwise truncates silently). Seed chunks are always kept; graph
# evidence stops being appended once the budget is hit.
CONTEXT_CHAR_BUDGET = int(os.getenv("CONTEXT_CHAR_BUDGET", "45000"))
GRAPH_HOPS = int(os.getenv("GRAPH_HOPS", "2"))             # graph expansion depth
GRAPH_MAX_NODES = int(os.getenv("GRAPH_MAX_NODES", "40"))

# Formation job queue: one document at a time per workspace (links need
# order), with this many workspaces forming in parallel. 0 = auto: 1 on local
# Ollama (concurrent formations jam its GPU queue), 3 on Anthropic.
FORMATION_CONCURRENCY = int(os.getenv("FORMATION_CONCURRENCY", "0"))
# Bounded attempts with exponential backoff instead of blind in-call retries.
FORMATION_MAX_ATTEMPTS = int(os.getenv("FORMATION_MAX_ATTEMPTS", "3"))
FORMATION_BACKOFF_S = int(os.getenv("FORMATION_BACKOFF_S", "60"))
# Read timeout on a single LLM call during formation. 5 min is generous for a
# hosted model; a call slower than this is almost always a hang, not real work.
# Self-hosters on slow local Ollama hardware may need to raise this (and the
# task timeout below).
FORMATION_READ_TIMEOUT_S = float(os.getenv("FORMATION_READ_TIMEOUT_S", "300"))
# Hard ceiling on the whole per-document formation job (extraction +
# consolidation), enforced by the worker. Without it a hung call pins a worker
# slot indefinitely and, since all formation for the instance shares a small
# pool of slots, a few hangs freeze the queue. Must exceed FORMATION_READ_TIMEOUT_S
# so a legitimately slow LLM read finishes before the job is killed.
FORMATION_TASK_TIMEOUT_S = float(os.getenv("FORMATION_TASK_TIMEOUT_S", "420"))
# /api/health/formation reports the queue "stalled" when documents are pending
# and workers are alive but nothing has completed in this many seconds. Set
# above FORMATION_TASK_TIMEOUT_S so a healthy-but-busy queue still completes a
# job within the window and never trips the alarm.
FORMATION_STALL_S = int(os.getenv("FORMATION_STALL_S", "600"))
# How long per-run SLO rows (formation_runs) are kept before the janitor
# prunes them. Percentile reporting only ever looks weeks back.
FORMATION_RUNS_RETENTION_DAYS = int(os.getenv("FORMATION_RUNS_RETENTION_DAYS", "30"))

# Daily formation quotas by billing plan (successful formations per workspace
# per UTC day; 0 = unlimited). Enforced at claim time — over-quota documents
# are parked as 'rate_limited' and requeued after midnight UTC, never lost.
# Bounds worst-case LLM spend per tenant per day.
FORMATION_DAILY_QUOTA_TRIAL = int(os.getenv("FORMATION_DAILY_QUOTA_TRIAL", "100"))
FORMATION_DAILY_QUOTA_TEAM = int(os.getenv("FORMATION_DAILY_QUOTA_TEAM", "5000"))


# Extraction validation: reasoning shorter than this (or merely repeating the
# "what") counts as trivial in the per-run validation report.
VALIDATION_MIN_REASONING_CHARS = int(os.getenv("VALIDATION_MIN_REASONING_CHARS", "40"))

# Usage accounting (usage_events): retention for per-call token rows, and an
# optional JSON map of per-model prices for dollar annotation in /api/ops/usage:
#   {"claude-fable-5": {"input_per_mtok": 3.0, "output_per_mtok": 15.0}}
# Empty = report tokens only.
USAGE_RETENTION_DAYS = int(os.getenv("USAGE_RETENTION_DAYS", "90"))
COST_RATES_JSON = os.getenv("COST_RATES_JSON", "")


def formation_quota_for(plan: "str | None") -> int:
    """Daily formation quota for a billing plan; 0 = unlimited. Unknown plans
    (including self-hosted custom values) are unlimited by design."""
    return {
        "trial": FORMATION_DAILY_QUOTA_TRIAL,
        "team": FORMATION_DAILY_QUOTA_TEAM,
    }.get((plan or "").lower(), 0)

# Post-formation consolidation: decisions whose label+summary embed this close
# are treated as the same decision and merged.
MERGE_SIM_THRESHOLD = float(os.getenv("MERGE_SIM_THRESHOLD", "0.86"))
# Batch consolidation debounce: a workspace's touched decisions consolidate
# once no formation has landed for DEBOUNCE seconds — or MAX_DELAY after the
# first touch, so continuous ingest can't postpone consolidation forever.
# TASK_TIMEOUT bounds one batch run the way FORMATION_TASK_TIMEOUT_S bounds a
# formation.
CONSOLIDATION_DEBOUNCE_S = int(os.getenv("CONSOLIDATION_DEBOUNCE_S", "120"))
CONSOLIDATION_MAX_DELAY_S = int(os.getenv("CONSOLIDATION_MAX_DELAY_S", "900"))
CONSOLIDATION_TASK_TIMEOUT_S = float(os.getenv("CONSOLIDATION_TASK_TIMEOUT_S", "300"))

# ── Cross-instance coordination (Redis) ─────────────────────────────────────
# Optional Redis for multi-instance deployments: per-workspace formation
# locks, cross-instance wake signals, leader election for the periodic
# tickers, and shared rate-limit counters. Empty = disabled (single-instance
# mode, no Redis required). Postgres stays the source of truth for job state
# either way; Redis only coordinates. Production Redis must run with
# maxmemory-policy=noeviction — an evicted workspace lock would let two
# instances form the same workspace concurrently.
REDIS_URL = os.getenv("REDIS_URL", "")
REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "ybase")
# Per-workspace formation lock TTL (seconds). 0 = auto-derive as
# FORMATION_TASK_TIMEOUT_S + 60, so the lock can only expire after the job it
# guards is dead — raising the task timeout can't silently break the invariant.
FORMATION_LOCK_TTL_S = float(os.getenv("FORMATION_LOCK_TTL_S", "0"))
# Stable id for this instance in leader election and heartbeats. Fly injects
# FLY_ALLOC_ID; elsewhere a random per-process id is fine.
WORKER_INSTANCE_ID = (
    os.getenv("WORKER_INSTANCE_ID") or os.getenv("FLY_ALLOC_ID") or uuid.uuid4().hex[:12]
)
# Ticker-leader lease (seconds); refreshed on every is_leader() check.
LEADER_TTL_S = int(os.getenv("LEADER_TTL_S", "60"))

# Open questions older than this are surfaced as "still unanswered" on Home.
STALE_QUESTION_DAYS = int(os.getenv("STALE_QUESTION_DAYS", "21"))

# Periodic per-workspace digest (new decisions, resolved/open questions). Stored
# in-app always; emailed too when an email provider is configured. 0 disables.
DIGEST_ENABLED = os.getenv("DIGEST_ENABLED", "true").lower() in ("1", "true", "yes", "on")
DIGEST_INTERVAL_S = int(os.getenv("DIGEST_INTERVAL_S", "604800"))  # weekly
# Optional email channel (Resend). Without a key, digests are in-app only.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
DIGEST_FROM_EMAIL = os.getenv("DIGEST_FROM_EMAIL", "")
# Base URL used for links inside digests/emails (the frontend origin).
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5173")

# Periodic Slack reconciliation: re-fetch recent history for selected channels
# so missed Events API deliveries (downtime, drops) converge — content-hash and
# external-ref dedup absorb the overlap. 0 disables.
SLACK_RECONCILE_INTERVAL_S = int(os.getenv("SLACK_RECONCILE_INTERVAL_S", "3600"))
SLACK_RECONCILE_WINDOW_DAYS = int(os.getenv("SLACK_RECONCILE_WINDOW_DAYS", "1"))

# Periodic Jira/GitHub re-sync. Unlike Slack, these have no realtime path, so the
# worker re-pulls recent changes every CONNECTOR_RESYNC_INTERVAL_S. A connection
# that has never synced (or a newly selected project/repo) gets a full
# CONNECTOR_BACKFILL_DAYS pull; subsequent re-syncs use the shorter
# CONNECTOR_RESYNC_WINDOW_DAYS (dedup absorbs the overlap). 0 disables re-sync.
CONNECTOR_RESYNC_INTERVAL_S = int(os.getenv("CONNECTOR_RESYNC_INTERVAL_S", "21600"))  # 6h
CONNECTOR_RESYNC_WINDOW_DAYS = int(os.getenv("CONNECTOR_RESYNC_WINDOW_DAYS", "2"))
CONNECTOR_BACKFILL_DAYS = int(os.getenv("CONNECTOR_BACKFILL_DAYS", "90"))
# Onboarding fast slice: the first backfill pulls only this many recent days so
# memory forms in minutes (connectors ingest oldest-first, so a full 90-day
# backfill would surface recent decisions last). Once the slice completes, the
# full CONNECTOR_BACKFILL_DAYS backfill is chained in the background; dedup
# absorbs the overlap.
FAST_SLICE_DAYS = int(os.getenv("FAST_SLICE_DAYS", "7"))

# Live Slack ingestion (Events API). Empty secret disables the endpoint.
SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_REDIRECT_BASE_URL = os.getenv("SLACK_REDIRECT_BASE_URL", "http://localhost:8100")
SLACK_THREAD_QUIET_S = int(os.getenv("SLACK_THREAD_QUIET_S", "600"))
SLACK_MIN_THREAD_CHARS = int(os.getenv("SLACK_MIN_THREAD_CHARS", "200"))
# Per-team budget on the inbound Events webhook so one busy or abusive workspace
# can't flood the endpoint and exhaust the DB pool for every tenant. Over budget
# the event is dropped (HTTP 200) — returning 429 would only trigger Slack's
# aggressive retries. 0 disables.
SLACK_EVENTS_RATE_PER_MINUTE = int(os.getenv("SLACK_EVENTS_RATE_PER_MINUTE", "600"))

# Jira (Atlassian) OAuth 3LO. Register an app at developer.atlassian.com with
# the read:jira-work, read:jira-user, and offline_access scopes. Empty client
# id/secret disables the Jira connector.
JIRA_CLIENT_ID = os.getenv("JIRA_CLIENT_ID", "")
JIRA_CLIENT_SECRET = os.getenv("JIRA_CLIENT_SECRET", "")
JIRA_REDIRECT_BASE_URL = os.getenv("JIRA_REDIRECT_BASE_URL", "http://localhost:8100")
# Backfill ceiling per project per sync, so one run can't fetch unbounded issues.
JIRA_MAX_ISSUES_PER_PROJECT = int(os.getenv("JIRA_MAX_ISSUES_PER_PROJECT", "500"))

# GitHub OAuth App (issues + pull requests). Register at
# github.com/settings/developers with the callback
# <base>/api/integrations/github/oauth/callback. OAuth-App tokens don't expire.
GITHUB_CLIENT_ID = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_REDIRECT_BASE_URL = os.getenv("GITHUB_REDIRECT_BASE_URL", "http://localhost:8100")
# Backfill ceiling per repo per sync.
GITHUB_MAX_ITEMS_PER_REPO = int(os.getenv("GITHUB_MAX_ITEMS_PER_REPO", "300"))

# Secret used to encrypt connector tokens at rest. Set to a long random value
# in production. If empty, Slack/Jira OAuth install is disabled.
CONNECTOR_SECRET_KEY = os.getenv("CONNECTOR_SECRET_KEY", "")
# Previous CONNECTOR_SECRET_KEYs, comma-separated, kept temporarily during a key
# rotation so tokens encrypted with them still decrypt. Set the new key in
# CONNECTOR_SECRET_KEY, move the old one here, run scripts/rotate_connector_key.py
# to re-encrypt everything with the new key, then clear this.
CONNECTOR_SECRET_KEYS_OLD = [
    k.strip() for k in os.getenv("CONNECTOR_SECRET_KEYS_OLD", "").split(",") if k.strip()
]

# Billing: length of the no-credit-card free trial for new workspaces (days).
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "7"))

# Google sign-in (OAuth 2.0 / OpenID Connect). Register an app at
# console.cloud.google.com with the authorized redirect URI
# <GOOGLE_REDIRECT_BASE_URL>/api/auth/google/callback. Empty client id/secret
# hides the "Continue with Google" button and disables the routes.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_BASE_URL = os.getenv("GOOGLE_REDIRECT_BASE_URL", "http://localhost:8100")

# When set (docker image), the backend serves the built frontend from here.
STATIC_DIR = os.getenv("STATIC_DIR", "")
