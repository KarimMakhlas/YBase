# Deploying Whybase

The `Dockerfile` builds the React UI and serves it from the FastAPI backend, so
the whole app is one container on port `8100`. You bring a Postgres database and
(for production-quality answers) an LLM key.

## Prerequisites

1. **Postgres with pgvector.** On startup the app runs `CREATE EXTENSION IF NOT
   EXISTS vector`, so the database must have pgvector available. These all work:
   Supabase, Neon, Railway Postgres, and recent Fly Postgres. A bare managed
   Postgres without pgvector will fail to start.
2. **An LLM.** Set `ANTHROPIC_API_KEY` for Claude or `NVIDIA_API_KEY` for
   NVIDIA's hosted OpenAI-compatible models. Without a hosted key the app
   expects a reachable Ollama server, which isn't practical in most cloud hosts.
3. **`CONNECTOR_SECRET_KEY`** — a long random string, required if you use any
   connector (it encrypts OAuth tokens). Generate one:
   `python -c "import secrets;print(secrets.token_urlsafe(48))"`.

See `.env.example` for the full list of variables.

## Option A — Fly.io

```bash
fly launch --no-deploy                 # edit the app name in fly.toml first
fly secrets set \
  DATABASE_URL="postgres://…/db?sslmode=require" \
  ANTHROPIC_API_KEY="sk-ant-…" \
  CONNECTOR_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
# Once you know the URL, point callbacks/links at it:
fly secrets set APP_BASE_URL="https://your-app.fly.dev" \
  CORS_ORIGINS="https://your-app.fly.dev"
fly deploy
```

`fly.toml` already sets `SESSION_COOKIE_SECURE=true`, `LLM_PROVIDER=anthropic`,
and `force_https`. To use NVIDIA on Fly instead, set `LLM_PROVIDER=nvidia` and
`NVIDIA_API_KEY`.

## Option B — Railway

1. New project → Deploy from repo (uses `railway.toml` / the Dockerfile).
2. Add a **Postgres** service (it includes pgvector); Railway sets `DATABASE_URL`.
3. In the app service **Variables**, set `ANTHROPIC_API_KEY`,
   `CONNECTOR_SECRET_KEY`, `SESSION_COOKIE_SECURE=true`, and `APP_BASE_URL` /
   `CORS_ORIGINS` to your Railway domain. Railway injects `$PORT`.

## Option C — Docker Compose (self-host)

`docker-compose.yml` runs everything including a pgvector Postgres:

```bash
ANTHROPIC_API_KEY=sk-ant-… CONNECTOR_SECRET_KEY=… \
  docker compose --profile app up -d --build      # → http://localhost:8100
```

## After deploy

- **Connectors:** point each provider's redirect base at your domain
  (`SLACK_REDIRECT_BASE_URL`, `JIRA_REDIRECT_BASE_URL`, `GITHUB_REDIRECT_BASE_URL`)
  and register that exact callback URL in the provider's OAuth app:
  `…/api/integrations/<provider>/oauth/callback`.
- **Auto-sync:** once a user selects projects/repos, the worker backfills them
  automatically (no manual "Backfill" click needed) and re-pulls recent changes
  on a schedule. Tune with `CONNECTOR_RESYNC_INTERVAL_S` (default `21600` = 6h;
  `0` disables), `CONNECTOR_RESYNC_WINDOW_DAYS` (default `2`), and
  `CONNECTOR_BACKFILL_DAYS` (default `90`, the first-pull depth). Slack keeps its
  own `SLACK_RECONCILE_INTERVAL_S` / `SLACK_RECONCILE_WINDOW_DAYS` safety-net knobs.
- **Verify:** open the URL, create a workspace (demo memory auto-seeds), ask a
  question, and check `GET /api/health/details` shows the expected LLM provider.
- **Cost control:** `QUERY_RATE_PER_MINUTE` / `INGEST_RATE_PER_MINUTE` cap
  per-user load on the paid LLM. Watch the Analytics dashboard for usage.
