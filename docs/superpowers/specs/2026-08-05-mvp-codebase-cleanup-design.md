# YBase MVP Codebase Cleanup Design

## Goal

Reduce YBase to a maintainable minimum working product while preserving its core memory workflow, billing, and agent access. Remove unused features, connectors, data, documentation, assets, configuration, and explanatory noise without rewriting the application or weakening its security and operational guarantees.

## Product Boundary

The MVP retains:

- Authentication, password reset, email verification, workspace ownership, and membership.
- Billing, plans, usage enforcement, and checkout flows.
- API-key management and MCP agent access.
- Manual document import.
- Slack, GitHub, and Notion OAuth, source selection, synchronization, and retry handling.
- Memory formation, consolidation, decisions, cited chat/search, and source evidence.
- A minimal MCP proposal approval flow.
- Essential application and formation health endpoints.

The MVP removes:

- Jira, Linear, Confluence, Discord, Google Docs, and Figma connectors.
- Analytics, digests, answer feedback, the general memory review queue, the operations dashboard, decision sharing, workspace invitations, and demo/evaluation tooling.
- Dedicated routes, UI, configuration, tests, styles, documentation, and database objects used only by removed features.

Google authentication is not a source connector and remains supported. Transactional email remains for authentication; digest generation and digest-specific UI are removed.

## Architecture

The application keeps its current React/Vite frontend, FastAPI backend, PostgreSQL database, and in-process memory worker. This cleanup is a reduction of the existing architecture, not a rewrite.

The backend exposes six functional areas:

1. Authentication and workspaces.
2. Billing.
3. API keys and MCP agent access.
4. Documents and source connectors.
5. Memory and decisions.
6. Query/chat and health.

Slack, GitHub, and Notion continue to use the connector orchestration layer. Provider-specific OAuth, normalization, and sync behavior stays inside each provider package. Shared orchestration dispatches only those three providers and rejects unsupported provider values.

The authenticated frontend has four primary surfaces:

1. Ask.
2. Decisions.
3. Sources.
4. Settings.

Account, workspace membership, billing, API keys, and MCP proposal approval live under Settings. Sources supports Slack, GitHub, Notion, and manual document import. Authentication, reset, and verification screens remain public.

Large retained modules are split only where responsibilities are independently understandable and testable. Likely boundaries are route/state helpers from `frontend/src/App.jsx`, provider metadata from `frontend/src/components/Sources.jsx`, and identical OAuth or sync mechanics shared by retained connectors. Line-count reduction alone does not justify a new abstraction.

## Data Cleanup

A new forward-only migration performs the destructive cleanup requested for removed features and connectors:

1. Delete source connections, streams, and sync jobs for Jira, Linear, Confluence, Discord, Google Docs, and Figma.
2. Delete documents whose source belongs to those removed providers.
3. Rely on foreign-key cascades to remove their chunks and evidence links.
4. Delete memory nodes that have no remaining evidence after the purge.
5. Preserve merged memory nodes that retain evidence from Slack, GitHub, Notion, or manual documents.
6. Remove obsolete tables for digests, answer feedback, decision shares, and workspace invitations after all application dependencies are removed.

Billing, usage, authentication, workspace, API-key, chat, document, connector, memory, and proposal data remain. Schema removal must account for foreign keys and indexes explicitly, and the migration must be safe to run once on databases containing or lacking removed-feature rows.

## MCP Proposal Flow

The MCP `propose_decision` tool remains useful only if a human can approve or reject proposals. The broad review and curation feature is removed, but a minimal proposal list with approve/reject actions remains under Settings. General node editing, archiving, and the standalone Review navigation item are removed.

Existing MCP tools for asking questions, retrieving task/file context, searching memory, and proposing decisions retain their current external contracts unless a test exposes a dependency on a removed feature.

## Error Handling and Security

Removed endpoints and OAuth callbacks disappear rather than returning compatibility placeholders. Requests naming unsupported connectors fail validation without starting jobs or writing data.

The cleanup preserves:

- Authentication and role checks.
- Workspace isolation.
- Billing write gates and usage accounting.
- API-key topic scoping and permissions.
- Request-size and rate limits.
- Connector token encryption.
- Connector retry, stale-job recovery, and per-workspace synchronization rules.
- Memory-worker concurrency, timeouts, retry, and recovery behavior.
- Transactional auth email behavior.

Comments that explain these constraints remain when the code cannot express the reason directly.

## Code Reduction Rules

- Delete modules, exports, imports, styles, assets, dependencies, configuration, and tests used only by removed features.
- Consolidate repeated retained-connector code only when inputs, outputs, error semantics, and retry behavior are equivalent.
- Prefer named helpers, early returns, and explicit data structures over compressed expressions.
- Remove comments and docstrings that restate names, narrate straightforward control flow, describe obsolete history, or duplicate nearby documentation.
- Retain concise comments for security rationale, external-provider quirks, concurrency, locking, retries, database invariants, and compatibility constraints.
- Remove dead compatibility routing for deleted UI surfaces unless it protects an authentication or billing URL.
- Do not introduce dependencies solely to shorten code.

## Documentation and Repository Hygiene

Retain only:

- A concise root `README.md` covering purpose, supported features, local setup, configuration, tests, and deployment basics.
- `backend/.env.example` as the application configuration reference.
- `mcp-server/README.md` for MCP installation and use.

Remove:

- `DEPLOY-neon.md`.
- `docs/architecture.html`.
- Ignored internal planning/specification files after this cleanup is implemented.
- Demo and evaluation scripts.
- Generated screenshots and caches.
- Unused or duplicate logo assets.

Generated dependencies and caches such as virtual environments, `node_modules`, build output, bytecode, and tool caches remain ignored and are removable through the repository clean command rather than version control.

## Quality Gates

The Ruff configuration moves to a repository location or invocation that CI and local commands consistently discover. Existing high-signal findings in retained code are resolved or narrowly suppressed with an explanation when a rule is a false positive. Python and frontend lint steps become blocking.

Verification includes:

- Migration tests covering connector data deletion, evidence preservation, and obsolete table removal.
- Retained auth, billing, workspace, API-key, MCP, document, memory, query, health, and connector tests.
- Slack, GitHub, and Notion normalization and synchronization tests.
- Tests proving removed routes and connector types are unavailable.
- Full backend tests against PostgreSQL and Redis.
- Frontend lint and production build.
- Docker Compose configuration validation.
- Searches for removed provider names, feature endpoints, stale configuration, unused imports, and obsolete documentation references.

## Success Criteria

- The remaining product supports the complete retained workflow from sign-in through source ingestion and cited answers.
- Billing and API-key/MCP access remain functional, including minimal proposal approval.
- Removed connectors, features, data, code, tests, configuration, and documentation are absent.
- The production frontend builds and all retained automated checks pass.
- CI lint checks are blocking and use the intended configuration.
- The resulting diff has a clear net reduction in tracked source and asset size.
- Retained code follows the comment and readability rules above without sacrificing non-obvious operational rationale.
