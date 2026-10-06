# Changelog — `apps/api`

All notable changes to the **api** service (FastAPI). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) from the consumer's perspective (root `CLAUDE.md`, rule 15).
The two services version independently.

## [Unreleased]

## [0.4.3] — 2026-10-06

### Security

- **SQL Server's own error text no longer reaches database spans.** The OTel SQLAlchemy instrumentor exports
  the driver's raw message on two paths — a failed statement (the span's status description) and a failed
  connection (the `connect` span records the exception event and sets the status) — and the message can quote
  user content (a truncation error carries `Truncated value: '…'`) or the login name. `hide_parameters` (0.4.1)
  cannot reach either. `app.observability.hide_driver_error_text` now swaps both hooks for versions that report
  only the exception kind (`IntegrityError`, `DBAPIError`, …), so failures stay diagnosable and content-free.
  It runs before `instrument()` and **fails closed**: if the swap is impossible, no DB spans are started.
  `tests/test_db_span_hygiene.py` sends failures through the real engine paths and reads the exported spans,
  with a control for each path proving the leak was real, and reads the upstream source so an instrumentor bump
  fails loudly. `tests/conftest.py` restores the hooks after every test. Closes the gap recorded in 0.4.1.
  **Still open:** a DB error that escapes after a streamed response has started is recorded on the FastAPI
  server span (roadmap api 4.3).

## [0.4.2] — 2026-10-06

### Fixed

- **Alembic now reads `apps/api/.env` like the service.** `alembic/env.py` calls `load_local_env()` before
  resolving `DATABASE_URL`. Before, only `app.main` did, so `alembic check` / `upgrade` /
  `revision --autogenerate` from a plain terminal silently used the template's placeholder URL
  (`your-server.database.windows.net`) even when `.env` held a real one. A variable already exported
  in the shell still wins, a missing file is a no-op, and the URL is never printed.
  `tests/test_alembic_env_loading.py` covers all four, offline, and checks the URL that reaches Alembic.

### Added

- **`Alembic target: <host>:<port>/<database> (DATABASE_URL from shell|.env|default)`**, logged once before
  Alembic connects, so a stale shell export cannot silently pick the database. It never shows the user or
  password and flags the template placeholder host (`app.config.describe_database_target`, tested).
- **`tests/conftest.py`**: reading the real `apps/api/.env` is a no-op in every test, so a developer's own
  `EXTERNAL_DATABASE_URL` / `AZURE_*` / telemetry values can no longer leak into the pytest process.

## [0.4.1] — 2026-10-06

### Security

- **Database errors no longer carry bound parameters.** The project and the external read-only engine
  are created with `hide_parameters=True`: `str(exc)`, `repr(exc)` and the OTel `exception.message` /
  `exception.stacktrace` for a `DBAPIError` show `[SQL parameters hidden …]` instead of the values
  (message text, titles, citations). `tests/test_db_error_hygiene.py` covers both engines, the span
  event (with a control case proving the leak path is real) and that the response and logs of a failing
  request hold only the exception kind. **Not covered:** SQL Server's own error text (e.g. a truncation
  error quoting the value) still reached the DB span's status description — closed in 0.4.3.
  Found by the api 1.1 security review.

## [0.4.0] — 2026-10-06

### Added

- **Conversation and message models** (`app/models/conversation.py`): `conversations` and `messages`
  per `docs/design/db-design.md` — UUID keys, `DATETIME2(3)` UTC timestamps, a `role` check, a JSON
  check on `citations`, and `messages.seq` (`BIGINT IDENTITY`, unique) as the thread order. Long text
  uses `Unicode()` so offline DDL says `NVARCHAR(max)`, never the deprecated `NTEXT`.
- **Conversation repository** (`app/repositories/conversations.py`): create, list newest-first (limit
  1–100), open with the thread ordered by `seq`, the last N messages for model history, and
  `add_message` — bumps `updated_at`, turns the first user message into the title of a conversation
  still called "New conversation" (whitespace collapsed, 200 characters). Flushes, never commits,
  never logs content.
- **First migration** `5228ef6b6604` (`alembic/versions/…_create_conversations_and_messages.py`): creates
  both tables with every constraint and index. Hand-written; `tests/test_migration_conversations.py`
  proves offline that it and the models render identical DDL (the stand-in for `alembic check`) and that
  the downgrade drops children first. **Not applied to any database** — applying it to dev is a human step.
- No endpoint yet: nothing changes at runtime until roadmap api 2.1.

## [0.3.0] — 2026-10-04

### Added

- **Error contract** `app/errors.py`: every non-2xx response body is `{"error": <snake_case code>,
  "trace_id": <id>}` with the trace id also echoed in `x-trace-id`. `ApiError` (+ `NotFound`,
  `Conflict`, `AIUnavailable`) for routes and repositories; handlers for `HTTPException`
  (a snake_case `detail` becomes the code, prose maps to a default per status; `Allow` and other
  protocol headers kept), `RequestValidationError` (`422 validation_error` — field locations are
  logged, never returned) and `AINotConfigured` (`503 ai_unavailable`); `UnhandledErrorMiddleware`
  turns anything else into `500 internal_error` logging only the exception kind and location.
  `v1_router` documents 422/500 as `ErrorBody` in OpenAPI (`docs/reference/openapi.json`
  regenerated). Promoted from the first project dry run (`docs/development/walkthrough-team-assistant.md`).

### Changed

- Comments and docstrings that described the Terraform landing zone now describe the use-case
  Bicep deployment (ADR-0012). No behaviour change.

## [0.2.0] — 2026-09-28

### Added

- **AI runtime** `app/ai/` (ADR-0009): LangChain + LangGraph on Azure AI Foundry — client
  factory (`get_chat_model` / `get_embeddings`, managed identity when deployed), a compiled
  `retrieve → answer` graph with an optional tool loop (`build_graph`, `ask`), SSE streaming
  (`stream_answer`, `sse_response`), a tool registry with the allow-listed read-only
  `query_external_db` tool, prompt files (`load_prompt`), content-free `gen_ai` telemetry
  (`model_call_span`), Azure AI Search retrieval (`AzureSearchRetriever`) and the ingestion job
  (`python -m app.ai.ingest`). No route or use case is shipped.
- New optional env vars: `AZURE_AI_ENDPOINT`, `AZURE_AI_DEPLOYMENT`,
  `AZURE_AI_EMBEDDING_DEPLOYMENT`, `AZURE_AI_EMBEDDING_DIMENSIONS`, `AZURE_AI_API_VERSION`,
  `AZURE_AI_AUTH_MODE`, `AZURE_AI_API_KEY`, `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_INDEX`,
  `AZURE_SEARCH_API_KEY`, `AI_REQUEST_TIMEOUT_SECONDS`, `AI_MAX_RETRIES`, `AI_ALLOW_TEXT_TO_SQL`.
- Dependencies: `langchain-core`, `langgraph`, `langchain-openai`, `azure-search-documents`
  (OTel pins unchanged). Tests: `tests/ai/` + the `tests/evals/` prompt regression harness.
- `python -m app.openapi_export` writes the API contract to `docs/reference/openapi.json`
  (`make openapi` also regenerates the web's TS types); `tests/test_openapi_export.py` fails
  when the committed contract is stale.

## [0.1.0] — 2026-09-28

### Changed — **BREAKING** (consumer-facing configuration)

- **Database engine is Azure SQL Database**, not Postgres (ADR-0008, supersedes ADR-0004):
  `DATABASE_URL` must now be an `mssql+aioodbc://…?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no`
  URL. `postgresql+asyncpg://…?ssl=require` URLs are no longer accepted.
- Dependencies: `asyncpg` and `opentelemetry-instrumentation-asyncpg` removed; `aioodbc`,
  `pyodbc` and `opentelemetry-instrumentation-sqlalchemy` (0.61b0) added. The runtime image now
  installs Microsoft ODBC Driver 18 for SQL Server.
- DB dependency spans come from the SQLAlchemy instrumentation (registered without an engine, so
  every lazily created engine is covered) instead of the asyncpg driver instrumentation.

### Added

- `EXTERNAL_DATABASE_URL` (optional) + `app/db/external.py`: a second, lazy, **read-only**
  engine for an external Azure SQL database (`get_external_session()` dependency); non-`SELECT`
  statements are refused before they reach the driver.

### Internal

- Engine factories resolve `create_async_engine` at call time (`sa_asyncio.create_async_engine`)
  so the OTel wrapper applies regardless of import order.
- Offline tests now guard on `pyodbc.connect`; Alembic offline mode renders T-SQL.

## [0.0.0] — 2026-09-20

- Template baseline. No features shipped.
