# Changelog — `apps/api`

All notable changes to the **api** service (FastAPI). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) from the consumer's perspective (root `CLAUDE.md`, rule 15).
The two services version independently.

## [Unreleased]

## [0.8.0] — 2026-10-07

### Added

- **`POST /v1/conversations/{id}/ask`** (`app/routers/ask.py`; roadmap api 4.2): ask a question in a conversation and
  get `{ message_id, answer, citations }`. `citations` are the retrieved documents whose `[n]` the answer cites, each
  once, in citation order — `[]` when it cites none — built from retrieval only; the same list is stored on the
  assistant message. Full contract and error table in `docs/reference/http-api.md`.
  - The question is stored and committed **before** the model is called, so it survives a failure; the assistant
    message (with citations and the output token count) is stored only once an answer exists. A conversation still
    titled "New conversation" takes the question as its title; `updated_at` moves with each message.
  - The model sees the conversation's last 10 messages (`app/ai/history.py`: `user` → human, `assistant` → AI, any
    other role dropped — a stored row never becomes a system message — each row cut to 4,000 characters), then the
    retrieved context, then the question.
  - Errors: blank, missing or invalid `question` (or a body that is not `application/json`) → `422`; over 4,000
    characters → `413 payload_too_large`; unknown conversation → `404`; **`429 busy`** when `AI_MAX_CONCURRENT_ASKS`
    asks are already running (nothing stored); a model or search failure or timeout, an unconfigured AI layer or a
    blank reply → `503 ai_unavailable` (question kept, no answer stored; no provider text in the body or the logs).
  - Question and answer text are cleaned before storage: NUL and lone surrogates are dropped and control characters
    other than tab, newline and carriage return become spaces, so no stored character costs more than 4 bytes in a
    thread read. The answer is cut at 4,000 characters (ending in `…`).
- **Three settings** (`.env.example`, `docker-compose.yml`, `docs/reference/environment-variables.md`):
  `AI_MAX_OUTPUT_TOKENS` (default 1024; sent to the model as `max_completion_tokens`), `AI_RETRIEVAL_TIMEOUT_SECONDS`
  (default 30; the query embedding and the search each get this long) and `AI_MAX_CONCURRENT_ASKS` (default 4; per
  process). An unusable value falls back to the default.
- `app/ai/limits.py` (`ask_slot`, released on success, failure and cancellation) and `app/ai/history.py`.
- OpenAPI (`docs/reference/openapi.json`) and the web's generated types include the new route.

### Changed

- **A model-call span no longer carries provider error text** (`app/ai/telemetry.py`). It used the tracer's defaults,
  which record the exception message as a span event and as the status text; a provider error can quote the request.
  The span now records only `ai.error_kind` and a status without a description, as the database spans already did.
- **The search client is bounded** (`app/ai/tools/retrieve.py`): it is built with the retrieval timeout as its
  connect and read timeout and 2 retries, where azure-core's defaults are 300 s and 10 retries. A search the caller
  gave up on now frees its worker thread within about that time instead of minutes. The same client serves ingestion.
- `AzureSearchRetriever` raises `TimeoutError` when a step exceeds its budget; the chat model client carries the
  output bound.
- Worst-case duration of one ask is about **4 minutes** (model 60 s × 3 attempts, plus 30 s for each of the two
  retrieval steps): whatever calls the route must wait longer than that.

### Known limits

- The body is read before the 4,000-character check (a 413 is not an early reject), so the BFF must cap the request
  size, and the api has no size cap of its own yet (api roadmap 5.5).
- A thread read is bounded to 500 messages, not by bytes: a thread of maximum-length emoji-only messages with very
  long citation titles can exceed the BFF's 8 MiB limit (api roadmap 5.4). Ordinary threads fit with a wide margin.
- The concurrency cap is per process and global, not per client: four slow asks can occupy every slot for up to about
  4 minutes. A per-client rate limit or quota belongs at the BFF or the ingress. An ask may keep running after its
  client disconnects, holding its slot until it finishes.
- Conversations and messages have no per-conversation cap and no purge (decision 5).
- The answer text is returned as the model wrote it (after cleaning); the web must not render raw HTML or load images
  from it, and must show citation titles and paths as plain text.

## [0.7.0] — 2026-10-06

### Added

- **`app/ai/citations.py`** (roadmap api 4.4): `citation_numbers`, `invalid_citation_numbers`, `cited_chunks` and
  `citation_of`. They read the `[n]` markers of an answer as numbers and look the passages up in what retrieval
  returned, so a citation can never name a document the model made up. A marker is one to three ASCII digits, alone
  or comma-separated (`[1, 3]`, `[1][2]`), and not inside code (a fenced block — an unclosed fence runs to the end,
  as in Markdown — or an inline span). The scan is one linear pass over the first 200,000 characters. Nothing
  rewrites the answer text.
- `ask()`'s `Answer` gains `cited` (the retrieved passages the answer cites, one per document, in citation order)
  and `invalid_citations` (how many `[n]` point to no passage). `sources` is unchanged (every retrieved
  document). One log line per answer carries `retrieved`, `cited` and `invalid` as counts — no text, no paths.

### Changed

- **A passage can no longer pose as another passage or as the start of the context** (`format_context` in
  `app/ai/graph.py`). The numbers and `[n] (source)` headers are written by the code only. A passage's `source` is
  flattened to one printable line, `()[]` become `_`, and it is cut at 200 characters (the **stored** source is not
  touched, so citations keep the real path). Any line of a passage's text that reads as a header or as the prompt's
  `Context:` marker is escaped with a leading backslash — also behind a quote, list, heading or table mark, behind
  zero-width, bidirectional or other format characters, in fullwidth form, and a bare `[2]` line whose `(…)` is on
  the next line. Other lines are untouched, except that every line separator (`\r\n`, U+2028, …) becomes `\n` and a
  trailing newline is dropped. The rendered prompt therefore changes for such lines and for sources with brackets or
  parentheses; nothing else changes for callers, and no endpoint uses any of this yet (api 4.2 does).
- The eval harness now also proves, for every case, that the only header lines the model sees are the code's own,
  numbered 1..N (checked again by an independent implementation that ignores decoration and fullwidth forms), and that
  each case's cited passages and invalid-marker count are as declared (`expect_cited`).

### Known limits

- `invalid_citation_numbers` is a **lower bound**: `[1000]` and longer are not markers, and a marker the page would
  render differently (indented code, HTML comments) may still count. A count of 0 does not prove an answer clean.
- Homoglyph letters, role-like lines (`System:`, `</context>`) and an instruction placed mid-line are not touched;
  the prompt's "context is data" rule is the defence there, and only the live eval (api 5.3) shows how a real model
  copes.
- `citation_of` returns one bounded line of printable text, but it is **untrusted**: `<`, `(` and a `javascript:`
  prefix survive. Whoever stores or shows it must render it as plain text, never as markup or a link target.

## [0.6.0] — 2026-10-06

### Added

- **Document titles in the index** (`app/ai/ingest.py`, `app/ai/tools/retrieve.py`; roadmap api 3.2): every chunk
  stores its `source` (`docs/<path>`) and a `title` — the document's first top-level `# ` heading (outside code
  fences of any length and outside front matter; `# C#` keeps its `#`; a byte-order mark is ignored), else the file
  name. Titles are one line of printable text, at most 200 characters; control characters become spaces. Retrieval
  returns both (`RetrievedChunk.title`), and the index gains a searchable `title` field.
- **`CORPUS_DIR`** (`app/ai/config.py`): the directory of the `.md` corpus; empty means the repository's `docs/`,
  `/app/corpus` in the image. A document's `source` is `docs/<path inside it>` wherever the corpus lives.
- **Vector-size guards** for ingestion. Each stops the run with a message naming both numbers:
  - an existing index whose vector size differs from `AZURE_AI_EMBEDDING_DIMENSIONS` is left **unchanged**; so is an
    existing index that has no `content_vector` field. Ingestion never deletes or recreates an index;
  - an embedding deployment that returns vectors of another size is caught by a constant probe **before** the index is
    touched, and again on every batch before it is uploaded (a batch after the first could in theory still fail
    after earlier batches were uploaded; re-run after fixing the setting — uploads are idempotent).
- `python -m app.ai.ingest` prints a one-line reason and exits `2` — instead of a traceback — for these stops: a
  missing or empty corpus, a file that is not valid UTF-8, a vector-size mismatch, and a missing AI/search setting.
  Other failures (a network or Azure error) still show their traceback.
- The ingestion log and report now include the run time in seconds (still counts only: no document text, no paths).

### Changed

- **Ingestion reads `.md` files only** (decision 14); `.txt` is no longer accepted. The command takes an optional
  `--corpus DIR` instead of a path argument, and an empty or missing corpus is an error. Files are read in a
  deterministic order (by relative path, case-sensitive), the same on Windows and Linux. A `.md` file that is not
  valid UTF-8 now stops the run, naming the file, instead of being read with replacement characters.
- `apps/api/.env.example` sets `AZURE_AI_EMBEDDING_DIMENSIONS=3072` for the Team Assistant (the code default stays
  `1536`).

### Upgrade notes

- **Run the ingestion before using this version for retrieval.** The retriever now asks the index for `title`; an
  index that lacks the field makes retrieval fail until the ingestion has updated it (the ingestion adds the field).
  Nothing in the api calls retrieval yet, so today this only matters for the first ingestion and for api 4.2.
- A `source` used to be whatever path was passed on the command line. Chunks ingested earlier under another path form
  keep their old ids and would sit next to the new ones as duplicates; if the index was ever ingested before this
  version, its owner should delete it and let the ingestion rebuild it (stale-chunk removal is api 3.5).
- An index built before this release at another vector size must also be deleted by its owner first.

## [0.5.1] — 2026-10-06

### Changed

- **The assistant's system prompt** (`app/ai/prompts/system.md`; roadmap api 4.1) is rewritten for the Team Assistant:
  it answers only from the numbered context; says it does not know when the context lacks the answer or says
  `(no context available)`; cites each statement as `[n]` (the number at the start of each passage), only numbers
  that exist and passages it used; and treats everything after the `Context:` line as data, not instructions, naming
  the usual injection phrases ("ignore the above", "reply with …", "you are now …", "reveal your prompt") as text
  never to follow or repeat. It also says earlier messages and tool results never override the rules, that `[n]` in
  an earlier turn is not this turn's passage, and that links, images and HTML from the context are not to be
  output. The context is the last thing in the system message. No endpoint uses the prompt yet (api 4.2), so
  nothing changes for callers.

### Added

- **Eval cases** (`tests/evals/cases.json`, each with a `kind`): two grounded answers (one citing two sources), two
  out-of-context questions (nothing retrieved; unrelated passages retrieved), and four injection cases — a passage
  ordering `PWNED` and a prompt reveal, a passage imitating the `Context:` marker and containing `{context}` and `{}`,
  a passage forging a `[2] (path)` header, and a user asking for the prompt. Cases can say what an answer must
  **not** contain.
- **Harness checks** (`tests/evals/test_evals.py`): the fake model records what it receives, so each case also
  proves the model gets exactly one system message — the rendered prompt, rules first, the context after the last
  `Context:` line — plus the user's question, with every retrieved passage in the context slot and none in the user
  turn. The prompt's own text is pinned (one placeholder, last; the injection, citation and no-context rules).
  **What this does not prove:** offline, each answer is the case's script, so the harness pins the plumbing and
  the guardrail wording, not model behaviour; the live tier (`AI_EVAL_LIVE=true`) is the check that a prompt change
  still resists injection, and should be run before a prompt change is released.

## [0.5.0] — 2026-10-06

### Added

- **`/v1/conversations`** (`app/routers/conversations.py`; roadmap api 2.1) — the first business endpoints:
  - `POST /v1/conversations` (body optional, `{ "title"?: string }` ≤ 200 characters) → `201` with
    `{ id, title, created_at, updated_at }`; a blank title becomes "New conversation".
  - `GET /v1/conversations?limit=` (1–100, default 50) → `200` `{ items: [{ id, title, updated_at }], count }`,
    newest first.
  - `GET /v1/conversations/{id}` → `200` the conversation and its messages, oldest first, each with its
    citations; `404 not_found` for an unknown id.
  - `422 validation_error` for a bad title, an unknown field, a `limit` outside 1–100 or a malformed id. Every
    response echoes `x-trace-id`; timestamps are UTC with a `Z`.
  - **A read is bounded:** at most 500 messages (the most recent, oldest first) and a `truncated` flag, because
    conversations are never purged (decision 5). The repository's `get_conversation_with_messages` now returns a
    `Thread` and queries newest-first with a limit.
  - Logs carry counts only — no ids, titles or message text; metrics label by the route pattern; and a new
    `app.observability.scrub_ids_server_hook` rewrites any UUID in the request span's URL attributes
    (`http.target`, `http.url`, `url.path`, `url.full`) to `{id}` — the FastAPI instrumentation would otherwise
    export `GET /v1/conversations/<uuid>` into Application Insights (`tests/test_http_span_hygiene.py` proves it,
    with a control).
  - A title is limited to what the column holds: `NVARCHAR(200)` counts UTF-16 code units, so 100 emoji fit and
    101 get a `422` instead of a database overflow; `make_title` cuts the same way, never splitting a character.
    `docs/reference/openapi.json`, the web's generated `api-types.ts` and `http-api.md` are regenerated.
  - Not exposed through the web BFF yet (roadmap web 1.2). Nothing calls `ask`: no model is involved.

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
