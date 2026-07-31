.DEFAULT_GOAL := help

PYTHON ?= backend/.venv/bin/python

.PHONY: help install up down logs dev-backend dev-frontend dev test build check ci clean

## Show available commands.
help:
	@awk 'BEGIN {printf "Usage: make <target>\\n\\nTargets:\\n"} /^##/ {sub(/^## /, ""); description = $$0; next} /^[a-zA-Z0-9_-]+:/ && description {split($$0, target, ":"); printf "  %-16s %s\\n", target[1], description; description = ""}' $(MAKEFILE_LIST)

## Install backend and frontend development dependencies.
install:
	$(PYTHON) -m pip install -r backend/requirements-dev.txt
	cd frontend && npm ci

## Start the local database and Redis services.
up:
	docker compose up -d db redis

## Stop and remove local Docker Compose services.
down:
	docker compose down

## Follow local Docker Compose service logs.
logs:
	docker compose logs -f

## Start the FastAPI development server on port 8100.
dev-backend:
	cd backend && PYTHONPATH=. .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8100 --reload

## Start the Vite development server.
dev-frontend:
	cd frontend && npm run dev -- --host 127.0.0.1

## Show how to run the backend and frontend development servers.
dev:
	@printf "Run these commands in separate terminals:\\n  make dev-backend\\n  make dev-frontend\\n"

## Run the backend test suite using the CI command.
test:
	PYTHONPATH=backend $(PYTHON) -m pytest backend/tests

## Build the frontend for production.
build:
	cd frontend && npm run build

## Validate the Docker Compose configuration.
check:
	docker compose --profile app config --quiet

## Run the local equivalent of CI validation.
ci: test build

## Remove regenerated Python and frontend build caches.
clean:
	find backend -type d -name __pycache__ -prune -exec rm -rf {} +
	find backend -type d -name .pytest_cache -prune -exec rm -rf {} +
	rm -rf frontend/dist frontend/node_modules/.vite
