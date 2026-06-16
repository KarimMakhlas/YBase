import os

# Postgres (pgvector). Default matches docker-compose.yml (host port 5433).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://whybase:whybase@localhost:5433/whybase",
)

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

# Per-user rate limits on the expensive endpoints (events per minute,
# 0 disables). Login has its own throttle (auth_login_attempts).
QUERY_RATE_PER_MINUTE = int(os.getenv("QUERY_RATE_PER_MINUTE", "20"))
INGEST_RATE_PER_MINUTE = int(os.getenv("INGEST_RATE_PER_MINUTE", "120"))

# Claude — memory formation, query reasoning, answer synthesis.
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-fable-5")

# LLM provider: "anthropic" | "ollama" | "auto" (auto = Anthropic when
# credentials are present, otherwise a local Ollama server).
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
GRAPH_HOPS = int(os.getenv("GRAPH_HOPS", "2"))             # graph expansion depth
GRAPH_MAX_NODES = int(os.getenv("GRAPH_MAX_NODES", "40"))

# Formation job queue: one document at a time per workspace (links need
# order), with this many workspaces forming in parallel. 0 = auto: 1 on local
# Ollama (concurrent formations jam its GPU queue), 3 on Anthropic.
FORMATION_CONCURRENCY = int(os.getenv("FORMATION_CONCURRENCY", "0"))
# Bounded attempts with exponential backoff instead of blind in-call retries.
FORMATION_MAX_ATTEMPTS = int(os.getenv("FORMATION_MAX_ATTEMPTS", "3"))
FORMATION_BACKOFF_S = int(os.getenv("FORMATION_BACKOFF_S", "60"))
FORMATION_READ_TIMEOUT_S = float(os.getenv("FORMATION_READ_TIMEOUT_S", "900"))

# Post-formation consolidation: decisions whose label+summary embed this close
# are treated as the same decision and merged.
MERGE_SIM_THRESHOLD = float(os.getenv("MERGE_SIM_THRESHOLD", "0.86"))

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

# Live Slack ingestion (Events API). Empty secret disables the endpoint.
SLACK_CLIENT_ID = os.getenv("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.getenv("SLACK_CLIENT_SECRET", "")
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
SLACK_REDIRECT_BASE_URL = os.getenv("SLACK_REDIRECT_BASE_URL", "http://localhost:8100")
SLACK_THREAD_QUIET_S = int(os.getenv("SLACK_THREAD_QUIET_S", "600"))
SLACK_MIN_THREAD_CHARS = int(os.getenv("SLACK_MIN_THREAD_CHARS", "200"))

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

# When set (docker image), the backend serves the built frontend from here.
STATIC_DIR = os.getenv("STATIC_DIR", "")
