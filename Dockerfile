# Full-stack image: built frontend served by the FastAPI backend.
# Build + run via: docker compose --profile app up -d --build

FROM node:20-alpine AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Keep this minor in step with .github/workflows/ci.yml — the tested Python and
# the shipped Python should be the same one.
FROM python:3.12-slim
WORKDIR /srv
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=ui /ui/dist ./static
ENV STATIC_DIR=/srv/static

# Drop root. The app only ever reads its own code and talks to Postgres, so it
# has no reason to run privileged.
RUN useradd --system --create-home --uid 10001 ybase && chown -R ybase:ybase /srv
USER ybase

EXPOSE 8100
# Lets Docker/Compose see an unhealthy container instead of a running-but-broken
# one. /api/health checks the DB round-trip, not just the process.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/api/health', timeout=4).status == 200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
