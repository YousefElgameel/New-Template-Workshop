# Roadmap — api (Team Assistant)

_Backend: models, migrations, `/v1` endpoints, AI graphs/tools, ingestion. Schema:
[`roadmaps/README.md`](../README.md). Planned 2026-10-06 from `docs/architecture/ARCHITECTURE.md`
Part B (B2–B8), `docs/design/db-design.md` and `docs/design/wireframes/chat.md`. Decisions
referenced as "decision N" are in B8; ADR-0013/0014/0015 are Proposed._

_There is no infra roadmap: every Azure resource already exists in `rg-ai-dev` (B6). Authoring
never waits for Azure — only **applying** a migration, **running** ingestion and the dev smoke
need a human with access._

## Phase 1 — Foundation (data)

### 1.1 — Conversation and message models

- **status:** done
- **depends_on:** []
- **layers:** [model]
- **acceptance:**
  - tables `conversations` and `messages` are modelled exactly as in `docs/design/db-design.md`: UUID primary keys, a length on every string, `role` check (`user`/`assistant`), `citations` JSON check, `messages.seq` as a unique `bigint IDENTITY`, FK `messages.conversation_id → conversations.id` with `NO ACTION`, the two indexes
  - a data-access layer can: create a conversation (title optional, default "New conversation"), list newest-first by `updated_at` with a limit, load one conversation with its messages ordered by `seq`, append a message
  - appending any message bumps the conversation's `updated_at` (decision 11); appending the first user message to a conversation still titled "New conversation" replaces the title with the question cut to 200 characters
  - the models are imported so Alembic autogenerate sees them
  - exercised with fake sessions only — no database is touched
- **how_to_test:**
  - `just test` (or `uv run --directory apps/api pytest`) — expect **251 passed** (203 before + 48 new in `tests/test_models_conversation.py` and `tests/test_repositories_conversations.py`)
  - `uv run --directory apps/api ruff check app tests alembic` — expect `All checks passed!`
  - see the SQL Server DDL the models produce, offline: `uv run --directory apps/api python -c "import app.models; from sqlalchemy.dialects import mssql; from sqlalchemy.schema import CreateTable; from app.db import Base; [print(CreateTable(t).compile(dialect=mssql.dialect())) for t in Base.metadata.sorted_tables]"` — expect `seq BIGINT NOT NULL IDENTITY(1,1)`, `content NVARCHAR(max) NOT NULL` (never `NTEXT`), the two named CHECKs, the unique `seq` and the FK `ON DELETE NO ACTION`
  - no endpoint exists yet, so there is nothing to call with `curl`; nothing connects to a database
- **needs_human:**
  - none for this item. Reply **done** to close it. (A live check of the `seq` refresh against SQL Server happens in 1.2/2.1 once the migration is applied.)
- **notes:**
  - 2026-10-06 — planned.
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — implemented; awaiting test by Yousef Wahid. Files: `app/models/conversation.py` (+ `__init__.py`), `app/repositories/conversations.py` (+ `__init__.py`), the two test files, `apps/api/CLAUDE.md`, `CHANGELOG.md` (0.4.0), `pyproject.toml`/`uv.lock` (0.4.0), `docs/architecture/data.md`, root `CLAUDE.md`. Reviews run: `code-reviewer` and `security-reviewer`, no blockers; fixed their points (history `limit` validated, timestamps truncated to ms, role CHECK built from the constant, tests added). Caught during the work: `UnicodeText` renders the deprecated `NTEXT` offline, so `content` uses `Unicode()`.
  - 2026-10-06 — design choices: the repository flushes and never commits; no ORM relationships (the async session cannot lazy-load); `add_message` refreshes `seq` after the flush because it is database-generated; the title rule is "title is still the default", so a blank first question leaves it for the next one.
  - 2026-10-06 — follow-ups raised by review, not done here (outside this item): engine `hide_parameters=True` → new item 1.3; size caps on message content and citations and a bounded thread read → notes on 2.1 and 4.2; an opt-in real-database test of `seq` → with 1.2.
  - 2026-10-06 — confirmed by Yousef Wahid; closed.

### 1.2 — First migration (create both tables)

- **status:** done
- **depends_on:** [1.1]
- **layers:** [migration]
- **acceptance:**
  - one Alembic revision creates `conversations` and `messages` with every constraint and index from 1.1, and a working downgrade that drops them in dependency order
  - the offline SQL (`alembic upgrade --sql`) is generated, reviewed and SQL-Server-safe (no features Azure SQL lacks)
  - `alembic check` reports no drift against the models
  - nothing is applied to any database by the agent
- **how_to_test:**
  - `just test` (or `uv run --directory apps/api pytest`) — expect **257 passed**: 251 before, plus 6 in `tests/test_migration_conversations.py`; `tests/test_alembic.py` was updated
  - `uv run --directory apps/api ruff check app tests alembic` — expect `All checks passed!`
  - `uv run --directory apps/api alembic heads` — expect `5228ef6b6604 (head)`
  - read the SQL a database would receive (offline, no connection; set a dummy URL first, e.g. `$env:DATABASE_URL='mssql+aioodbc://u:p@db.invalid:1433/app?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no'`): `uv run --directory apps/api alembic upgrade head --sql` — expect `CREATE TABLE conversations` then `CREATE TABLE messages` with `seq BIGINT NOT NULL IDENTITY(1,1)`, `content NVARCHAR(max)`, the two named CHECKs, `ON DELETE NO ACTION`, `ix_conversations_updated_at … (updated_at DESC)` and `ix_messages_conversation_id_seq`
  - and the reverse: `alembic downgrade 5228ef6b6604:base --sql` — expect `DROP INDEX`/`DROP TABLE` for `messages` first, then `conversations`, then the `alembic_version` delete
  - nothing connects to a database; the tests fail if `pyodbc.connect` is called
- **needs_human:**
  - ~~install ODBC Driver 18~~ — already installed on this machine (`pyodbc.drivers()` lists it)
  - review the offline SQL above, then **apply it to dev yourself after explicit confirmation** (`.github/workflows/migrate.yml` for `dev`, or a named human) — the agent never runs `alembic upgrade`; the applying identity needs DDL rights (`db_ddladmin`), the runtime identity only `db_datareader`/`db_datawriter`
  - **after applying:** run `uv run --directory apps/api alembic check` against dev. This is the one acceptance point that cannot be proven offline ("no drift"); the offline parity test is its stand-in. Known risk: `check` may report a false drift on `ix_conversations_updated_at` (the DESC expression index) — if it does, tell me and I will adjust
  - reply **done** to close the item, or tell me what to change
- **notes:**
  - 2026-10-06 — planned. Delegate to `migration-author`.
  - 2026-10-06 — started by Claude. **Path taken: hand-written**, not autogenerated — autogenerate needs the dev database and its credentials, and the dev database's state is unknown.
  - 2026-10-06 — from the 1.1 review: confirm `IDENTITY(1,1)`, `ix_conversations_updated_at … DESC`, `NVARCHAR(max)` (not `NTEXT`) and the CHECK names in the offline SQL, and add an opt-in real-database test (skipped unless `DATABASE_URL` is reachable) that inserts two messages and asserts `seq` is filled and increasing.
  - 2026-10-06 — implemented; awaiting test by Yousef Wahid. Files: `alembic/versions/20261006_1321-5228ef6b6604_create_conversations_and_messages.py`, `tests/test_migration_conversations.py` (new), `tests/test_alembic.py` (two "no revisions" assertions replaced), `alembic.ini` comment, docs (`apps/api/CLAUDE.md`, `README.md`, `CHANGELOG.md`, root `README.md`/`CLAUDE.md`, `docs/architecture/data.md`, `docs/design/db-design.md`, `docs/development/testing.md`, `docs/reference/commands.md`, `docs/getting-started.md`). Reviews: `code-reviewer` and `security-reviewer`, no blockers; fixed their points (tests no longer pinned to the head, parity checks every table and compares sorted lists, the batch split is on a `GO` line, stale docs corrected). Version stays 0.4.0 (unreleased, nothing tagged): the changelog entry went into it.
  - 2026-10-06 — not done, deliberately: an opt-in real-database test of `seq` — it would write rows to the dev database, which must not happen before the migration is applied and you have chosen to; needs an explicit `RUN_DB_TESTS=1`-style opt-in, so it is proposed for after the apply.
  - 2026-10-06 — proposals from the reviews, for you to decide (not applied): (1) make `downgrade()` refuse to run unless an explicit opt-in env var is set, since it drops both tables and all data and the only guard today is the GitHub Environment reviewers on `migrate.yml`; (2) name the three `DEFAULT` constraints (SQL Server auto-names them, which makes a later `ALTER` of a default harder); (3) confirm the migrate service principal and the runtime identity stay separate accounts. Template-owned text that still says "no migrations" (ARCHITECTURE.md Part A, `.claude/rules/35-fastapi.md`, `.claude/agents/migration-author.md`, `.claude/skills/write-migration/SKILL.md`, `.claude/commands/write-migration.md`) was left alone.
  - 2026-10-06 — applied to the dev database by Yousef Wahid (the target shown by `Alembic target:`, URL from `apps/api/.env`). `alembic check` then printed `No new upgrade operations detected.` — no drift between the models and the database, which was the one acceptance point that could not be proven offline; the feared false drift on `ix_conversations_updated_at` did not occur. Confirmed by Yousef Wahid; closed.

### 1.3 — Keep SQL parameters out of database errors

- **status:** done
- **depends_on:** []
- **layers:** [model]
- **acceptance:**
  - the project engine is created with `hide_parameters=True`, so an `IntegrityError`/`DataError`/`DBAPIError` never carries message text, titles or citations in `str(exc)` or `repr(exc)`
  - a test asserts the setting on the engine; the external read-only engine gets the same
  - no log line, response body or span includes exception text from the database layer (checked by a test that raises a failing statement and inspects the error handler's output)
- **how_to_test:**
  - `just test` (or `uv run --directory apps/api pytest`) — expect **267 passed** in api (257 before, plus 10 new in `tests/test_db_error_hygiene.py`; `test_db_engine.py` gained one assertion)
  - `uv run --directory apps/api ruff check app tests alembic` — expect `All checks passed!`
  - `uv run --directory apps/api pytest tests/test_db_error_hygiene.py -v` — expect 10 passed, including **a failing INSERT sent through the real project engine** (no connection) whose `str(exc)` shows `[SQL parameters hidden due to hide_parameters=True]` and not the message text, the same for the read-only external engine, and a control test that flips the setting off and sees the text leak
  - prove the tests bite: in `app/db/engine.py` change `hide_parameters=True` to `False`, run `tests/test_db_error_hygiene.py` and `tests/test_db_engine.py` (5 fail), then change it back — I did exactly this and restored it
  - nothing connects to a database; `pyodbc.connect` is patched to fail in these tests
- **needs_human:**
  - none for this item. Reply **done** to close it. (The residual risk — SQL Server's own error text on DB spans — is tracked as 1.4; your call when.)
- **notes:**
  - 2026-10-06 — added after the 1.1 security review (MEDIUM): user content could otherwise reach logs through SQLAlchemy exception text. Touches `app/db/engine.py` and `external.py`, which is why it is its own item. Do it before 2.1 ships.
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — implemented; awaiting test by Yousef Wahid. Files: `app/db/engine.py`, `app/db/external.py` (one kwarg each), `tests/test_db_error_hygiene.py` (new), `tests/test_db_engine.py`, `apps/api/CLAUDE.md`, `CHANGELOG.md` (0.4.1), `pyproject.toml`/`uv.lock` (0.4.1), `docs/architecture/data.md`, `.claude/rules/25-sqlalchemy.md` (the engine line now shows the kwarg). Reviews: `code-reviewer` and `security-reviewer`, no blockers; fixed their points (a failing statement now goes through the real engine path instead of a hand-built exception; the instrumentor test pins the kwarg; docs state what is NOT covered).
  - 2026-10-06 — acceptance bullet 1 is true for the **bound parameters** SQLAlchemy appends. It cannot cover the **driver's own message** (`exc.orig`), which SQL Server writes; see 1.4. Bullet 3 holds for logs and responses (handlers log only exception kind and place); span events now carry the hidden placeholder, but the DB span's status description still carries the driver text.
  - 2026-10-06 — left alone on purpose: `alembic/env.py` creates its own engine without the flag (migrations run under the CLI or `migrate.yml`, DDL carries no user content); a one-line change if you want consistency.
  - 2026-10-06 — version bumped to 0.4.1 (PATCH, security hardening, no contract change).
  - 2026-10-06 — confirmed by Yousef Wahid; closed.

### 1.4 — Keep driver error text off database spans

- **status:** done
- **depends_on:** [1.3]
- **layers:** [model]
- **acceptance:**
  - the exception text SQL Server returns (`exc.orig`) never reaches a span's status description or events: an OTel span processor (or exporter filter) replaces the description on database client spans with the exception kind
  - a test puts a secret in a fake driver error, sends it through the real engine path like the 1.3 tests, and shows the exported span carries only the kind
  - a failing statement still shows its exception _kind_ on the span, so errors stay diagnosable
- **how_to_test:**
  - `just test` (or `uv run --directory apps/api pytest`) — expect **288 passed** in api (279 before, plus 9 in `tests/test_db_span_hygiene.py`)
  - `uv run --directory apps/api ruff check app tests alembic` — expect `All checks passed!`
  - `uv run --directory apps/api pytest tests/test_db_span_hygiene.py -v` — expect 9 passed: for **a failing statement** and for **a failing connection** there is a control (the driver message IS on the span without the swap) and a test (only the exception kind is, and no exception event on the connect span); plus idempotence, "swap is in place before `instrument()` runs", "no DB spans if the swap is impossible", and a test that reads the upstream package source
  - order independence: `pytest tests/test_observability_config.py tests/test_db_span_hygiene.py` and the reverse order — both 44 passed
  - prove the tests bite: in `app/observability.py` make `_safe_handle_error` use `str(context.original_exception)`, or change `record_exception=False` to `True` in `_safe_wrap_connect`; the matching test fails each time (I did both and restored them)
  - nothing connects to a database; `pyodbc.connect` is patched to fail
- **needs_human:**
  - none for this item. Reply **done** to close it. (An optional live check needs Application Insights, which is not set up: with a connection string and a failing statement, the dependency row's result description should be an exception name, not SQL Server's message.)
- **notes:**
  - 2026-10-06 — added from the 1.3 security review (MEDIUM). `opentelemetry-instrumentation-sqlalchemy` 0.61b0 sets `span.set_status(ERROR, str(original_exception))`, and the original exception is the raw pyodbc error, so `hide_parameters` cannot reach it. Real example: SQL Server error 2628 ("String or binary data would be truncated … Truncated value: '…'") quotes the user's text. Not reachable today (titles are clamped to the column width, message content is `NVARCHAR(max)`), but it becomes reachable if a bounded text column is added or the clamp is removed. Do it before any endpoint writes content you cannot clamp.
  - 2026-10-06 — started by Claude. Picked by the skill's rule (first `todo` in phase order with its dependencies done), ahead of 2.1.
  - 2026-10-06 — implemented; awaiting test by Yousef Wahid. Files: `app/observability.py` (`_safe_handle_error`, `_safe_wrap_connect`, `hide_driver_error_text`, `_instrument_sqlalchemy` now fails closed), `tests/test_db_span_hygiene.py` (new), `tests/conftest.py` (restores the instrumentor hooks after each test), `apps/api/CLAUDE.md`, `CHANGELOG.md` (0.4.3), `pyproject.toml`/`uv.lock` (0.4.3), `.claude/rules/60-observability.md`. Reviews: `code-reviewer` and `security-reviewer`.
  - 2026-10-06 — the security review found a second leak path I had missed: a failed **connection** (login rejected, firewall, unreachable host) is recorded by the instrumentor's `connect` span as an exception event + status, and `handle_error` never sees it (no execution context). Verified in the installed source (`_wrap_connect`, wired in the package's `instrument()`), and a control test shows the message on the span. Both paths are now swapped. The code reviewer called that wrapper "dead code" — that was wrong; I checked the source rather than trust either report.
  - 2026-10-06 — also from the reviews: the swap runs BEFORE `instrument()` (the package binds `_wrap_connect` by name), and fails closed (no DB spans) instead of warning and carrying on; tests no longer depend on run order (conftest restores the hooks; the pin test reads module source instead of the live attribute).
  - 2026-10-06 — NOT covered, recorded on 4.3: a DB error that escapes after a **streamed** response has started is re-raised by `UnhandledErrorMiddleware` and recorded by the FastAPI instrumentation on the server span (`exception.message` carries the driver text). No route writes to the database while streaming yet. Also: under the `opentelemetry-instrument` launcher the instrumentor could start before the swap — the Dockerfile uses plain `uvicorn`; documented in `apps/api/CLAUDE.md`. `db.user` (the login name under SQL auth) and `db.statement` (placeholders only) are exported as before.
  - 2026-10-06 — version 0.4.3 (PATCH, security hardening, no contract change).
  - 2026-10-06 — confirmed by Yousef Wahid; closed.

### 1.5 — Alembic reads `apps/api/.env` like the service does

- **status:** done
- **depends_on:** []
- **layers:** [migration]
- **acceptance:**
  - `alembic check`, `alembic upgrade` and `alembic revision --autogenerate` run from a plain terminal use the `DATABASE_URL` in `apps/api/.env`, exactly as `just dev` does — no manual export needed
  - a `DATABASE_URL` already set in the shell still wins (the file never overrides it), and a missing `.env` is a no-op
  - nothing is printed or logged about the URL, and offline `--sql` still never connects
- **how_to_test:**
  - `just test` (or `uv run --directory apps/api pytest`) — expect **279 passed** in api (267 before; 12 new across `tests/test_alembic_env_loading.py`, `tests/test_config_database_target.py`; `tests/conftest.py` is new and changes no count)
  - `uv run --directory apps/api ruff check app tests alembic` — expect `All checks passed!`
  - the real check, from a plain terminal with **no** `DATABASE_URL` exported: `uv run --directory apps/api alembic check` — expect `INFO [alembic.env] Alembic target: <your-server>:1433/<your-database> (DATABASE_URL from .env)` and then `Target database is not up to date.` until the migration is applied (I ran this: it reached the database named in your `.env`, not the template placeholder)
  - export a different URL first (`$env:DATABASE_URL = '…'`) and run it again — the line must now say `from shell`; unset it and it says `from .env`
  - prove the tests bite: in `alembic/env.py` replace the `load_local_env()` line with `pass`, run `tests/test_alembic_env_loading.py` (2 fail), and put it back — I did this and restored it
- **needs_human:**
  - **look at the `Alembic target:` line above and confirm it is the database you intend to migrate.** Its server and database names look like a shared sandbox, not a Team Assistant database. I cannot tell whether that is intended; applying the migration creates two tables there
  - reply **done** to close the item, or tell me what to change
- **notes:**
  - 2026-10-06 — added after `alembic check` fell back to the template placeholder host (`your-server.database.windows.net`) although `apps/api/.env` held a real URL: only `app/main.py` called `load_local_env()`, so every Alembic command ignored the file unless `DATABASE_URL` was exported. The docs (`write-migration` skill, `apps/api/CLAUDE.md`) already said the URL comes from `apps/api/.env`; this makes that true.
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — implemented; awaiting test by Yousef Wahid. Files: `alembic/env.py` (loads `.env`, logs the target), `app/config.py` (`describe_database_target`, `PLACEHOLDER_DATABASE_HOST`), `tests/conftest.py` (new), `tests/test_alembic_env_loading.py` (new), `tests/test_config_database_target.py` (new), `apps/api/CLAUDE.md`, `CHANGELOG.md` (0.4.2), `pyproject.toml`/`uv.lock` (0.4.2), `docs/development/testing.md`. Reviews: `code-reviewer` and `security-reviewer`, no blockers.
  - 2026-10-06 — changes made because of the reviews: the one-line `Alembic target:` log (security MEDIUM: a stale shell export would silently pick the database); `tests/conftest.py` (reading the real `apps/api/.env` is a no-op in tests, so a developer's own `AZURE_*`/telemetry values cannot leak into the run); the tests now check the URL that reaches Alembic, not only `os.environ`. `LOCAL_ENV_FILE` is left untouched because an existing test pins its value, so the guard wraps `load_dotenv` instead.
  - 2026-10-06 — known and accepted: the whole `.env` is loaded into the Alembic process, not only `DATABASE_URL` (same as the service; `env.py` imports no telemetry or AI code and reads nothing else). A `DATABASE_URL` exported in the shell wins over the file, even an empty one — documented in `apps/api/CLAUDE.md`. Driver tracebacks from a bad URL can still echo it: do not paste Alembic failures unredacted.
  - 2026-10-06 — version 0.4.2 (PATCH).
  - 2026-10-06 — confirmed by Yousef Wahid; closed. He also confirmed that the database named by the `Alembic target:` line (from `apps/api/.env`) is the intended dev database for the api 1.2 migration.

## Phase 2 — Conversation history (F2)

### 2.1 — Create, list and open conversations

- **status:** done
- **depends_on:** [1.1, 1.3]
- **layers:** [endpoint]
- **acceptance:**
  - `POST /v1/conversations` with `{ "title"?: string }` → `201` `{ id, title, created_at, updated_at }`; a title over 200 characters or a wrong type → `422 validation_error`
  - `GET /v1/conversations?limit=50` → `200` `{ items: [{ id, title, updated_at }], count }`, newest first by `updated_at`; `limit` is 1–100 (default 50), outside that → `422`
  - `GET /v1/conversations/{id}` → `200` the conversation plus `messages: [{ id, role, content, citations, created_at }]` ordered by `seq`; unknown id → `404 not_found`
  - every response echoes `x-trace-id`; failures use the standard `{ "error", "trace_id" }` shape; routes are `/v1`-prefixed
  - no conversation id or message content appears in logs, span attributes or metric attributes (route patterns only)
  - `docs/reference/openapi.json` and `http-api.md` regenerated
  - works against fake sessions; against the dev database once 1.2 is applied
- **how_to_test:**
  - `just test` (or `uv run --directory apps/api pytest`) — expect **346 passed** in api (288 before; 58 new across `tests/test_conversations_api.py`, `tests/test_http_span_hygiene.py` and `tests/test_repositories_conversations.py`) and web unchanged at 136
  - `uv run --directory apps/api ruff check app tests alembic` — `All checks passed!`; `pnpm -C apps/web typecheck` — clean (the web's generated `api-types.ts` changed)
  - the contract: `just openapi` — expect no further diff; `docs/reference/openapi.json` lists the three routes
  - **live check against the dev database** (optional, yours — it writes rows to the database `Alembic target:` showed). Terminal 1: `uv run --directory apps/api uvicorn app.main:app --port 8000` (loads `apps/api/.env`). Terminal 2 (PowerShell):
    ```
    $c = Invoke-RestMethod -Method Post -Uri http://localhost:8000/v1/conversations -ContentType application/json -Body '{"title":"smoke test"}' -ResponseHeadersVariable h
    $c; $h["x-trace-id"]                                   # 201 body {id,title,created_at,updated_at}; a 32-hex trace id
    Invoke-RestMethod http://localhost:8000/v1/conversations                 # items newest first, count
    Invoke-RestMethod "http://localhost:8000/v1/conversations/$($c.id)"      # messages = [], truncated = False
    Invoke-RestMethod "http://localhost:8000/v1/conversations/$([guid]::NewGuid())"   # 404 {"error":"not_found","trace_id":...}
    Invoke-RestMethod "http://localhost:8000/v1/conversations?limit=0"       # 422 {"error":"validation_error",...}
    ```
    There is no delete endpoint (out of scope, decision 6), so the smoke-test row stays; remove it in the database if you want it gone.
  - prove the tests bite: in `app/observability.py` make the scrub in `scrub_ids_server_hook` a no-op (2 span tests fail), or in `app/routers/conversations.py` make `_fits_the_title_column` never raise (the emoji-title test fails) — I did both and restored them
- **needs_human:**
  - optional: the live check above (the migration is applied, so it works now)
  - cap request bodies at the ingress or in the BFF — the api reads a whole body before validating it (the web roadmap's BFF items are the place; I do not edit another layer's roadmap)
  - reply **done** to close the item, or tell me what to change
- **notes:**
  - 2026-10-06 — planned. The web roadmap should `depends_on: [api:2.1]` and use `MOCK_UPSTREAM` fixtures until then.
  - 2026-10-06 — `depends_on` gained 1.3 after the 1.1 security review. Also bound the thread read: `GET /v1/conversations/{id}` must not return an unbounded number of messages (cap or paginate it), and conversation ids and titles must stay out of logs, spans and metric attributes.
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — implemented; awaiting test by Yousef Wahid. Files: `app/routers/conversations.py` (new) and `v1.py`, `app/repositories/conversations.py` (bounded read, `Thread`, UTF-16 titles), `app/observability.py` (`scrub_ids_server_hook`), the three test files, `docs/reference/openapi.json` + the web's generated `api-types.ts` (`just openapi`), `docs/reference/http-api.md`, `apps/api/CLAUDE.md`, `CHANGELOG.md` + `pyproject.toml` + `uv.lock` (0.5.0), `.claude/rules/60-observability.md`, ADR-0014 (consequences). Reviews: `code-reviewer` and `security-reviewer`, no CRITICAL/HIGH; every finding below was verified or fixed.
  - 2026-10-06 — decisions made here, for the record: (1) `GET /{id}` returns at most **500** messages — the most recent, oldest first — plus `truncated: bool`; (2) timestamps are returned as UTC with a `Z`; (3) a title is limited to the column's **UTF-16 units** (100 emoji fit, 101 → `422`), because a code-point limit let 150 emoji overflow `NVARCHAR(200)` into a 500; (4) the POST body is optional and `extra="forbid"`.
  - 2026-10-06 — PROPOSED edit to `ARCHITECTURE.md` B4a (the team applies it): `GET /v1/conversations/{id}` → `200` conversation + `messages` (oldest first, at most 500) + `truncated`.
  - 2026-10-06 — found by the reviews and fixed: the **conversation id was on the request span** — the FastAPI instrumentation exports the concrete target (`http.target`, `http.url`), and my first docstring claimed otherwise. A control test shows the id on the span without the hook and gone with `scrub_ids_server_hook`. Also fixed: the title overflow, a monkeypatch in my test that did nothing (the router now passes the cap at call time), a logging test that could pass vacuously (it now requires the endpoints' own log events), and a stale sentence in `http-api.md`.
  - 2026-10-06 — NOT done here, carried forward: validate `citations` on insert in 4.2/4.3 (valid JSON of the wrong shape makes that conversation's `GET` a permanent clean 500 — a test pins the clean 500); cap message `content` size on write (4.2 limits the question to 4,000 characters; an answer is bounded by the model) because a read is bounded by count, not bytes; a rate limit on `POST` once auth lands (ADR-0014 now says so); body-size cap at the ingress/BFF. The list endpoint exposes all ids and titles — recorded in ADR-0014.
  - 2026-10-06 — version 0.5.0 (MINOR: new endpoints, backward compatible).
  - 2026-10-06 — confirmed by Yousef Wahid; closed.

## Phase 3 — Knowledge base (F3)

### 3.1 — Threat model for chat, history and ingest

- **status:** done
- **depends_on:** []
- **layers:** [docs]
- **acceptance:**
  - `docs/security/threat-models/team-assistant.md` (STRIDE) covers: shared conversations holding personal data (ADR-0014), the ingest endpoint and its shared secret (ADR-0015), indirect prompt injection through the corpus, quota abuse, and the all-IP dev firewall (decision 22)
  - each threat has a mitigation that maps to a roadmap item or an accepted risk
  - ADR-0014 and ADR-0015 link to it
- **how_to_test:**
  - open `docs/security/threat-models/team-assistant.md`: the status line says "Draft — awaiting review"; section 6 is the threat register (each row names its mitigation item, or ACCEPTED with a proposed owner), section 7 the AI threats, section 8 lists 15 gaps with no roadmap item (G1–G15)
  - check the five topics are covered: shared conversations (ADR-0014: I1, S2, R1), the ingest key (ADR-0015: S5, E1), corpus injection (section 7, AI-1 to AI-5), quota abuse (section 6.5), the all-IP dev firewall (section 6.7)
  - `docs/adr/0014-…md` and `docs/adr/0015-…md` each got one Consequences bullet and one References line linking to it (`git diff docs/adr` shows 6 added lines, nothing else)
  - `docs/security/threat-models/README.md` lists it with status "Draft — awaiting review"
- **needs_human:**
  - review the threat model and accept it (or say what to change) before 3.3 ships; the "ACCEPTED" entries are proposals until you do
  - decide what to do with gaps G1–G15 (section 8): none is a roadmap item yet. Highest priority: G1/G2 rate limit and `max_tokens` (cost); G9 restrict the dev SQL firewall (an admin action, decision 22; ARCHITECTURE B6 still says "no change needed", a Part B edit for the team to make); G6 ingest hygiene (before 3.2/3.3 ship); G8 admin-key hardening (3.3). `/plan-roadmap api` can turn the ones you choose into items
  - two open decisions: store `trace_id` on message rows (G14), and whether decision 14's corpus scope should be narrowed (G7)
- **notes:**
  - 2026-10-06 — planned. Delegate to `threat-modeler` (inputs listed in the 2026-10-06 architecture review).
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — drafted by the `threat-modeler` agent; verified by Claude against the repo: all 19 roadmap ids cited exist; spot-checked claims hold (`.txt` accepted and `rglob` in `app/ai/ingest.py`; upsert only; the default log redaction list has no admin-key entry; the web renders plain text and sets no CSP; ARCHITECTURE B6 contradicts decision 22). Files: `docs/security/threat-models/team-assistant.md` (new), its README row, one pointer each in ADR-0014 and ADR-0015 (decisions and status unchanged). Docs only: no code, no tests, no version bump. Not verified (flagged in the model): the BFF's `x-trace-id` handling, whether `pii-purge.md` covers the SQL tables, Foundry data-retention settings.
  - 2026-10-06 — confirmed by Yousef Wahid (the "done" closes the item; the threat model file itself stays "Draft — awaiting review" until its acceptance is recorded — gaps G1–G15 are still unplanned).

### 3.2 — Ingestion with titles at 3072 dimensions

- **status:** done
- **depends_on:** []
- **layers:** [ai]
- **acceptance:**
  - ingestion stores, for every chunk, its source path and a `title` (the file's first `# ` heading, else the file name) in the index, and retrieval returns both
  - the corpus root is configurable: `./docs` locally, `/app/corpus` when deployed (decision 1, ADR-0013); only `.md` files under `docs/` are read (decision 14)
  - the index vector dimension comes from `AZURE_AI_EMBEDDING_DIMENSIONS` (3072 for `text-embedding-3-large`, decision 20); if the existing index has a different dimension, ingestion stops with a clear error naming both values — it never silently reuses or recreates it
  - ingestion stays idempotent and logs counts and durations only, never document text
  - covered with fake search and embedding clients — no live service
- **how_to_test:**
  - `cd apps/api && uv run pytest tests/ai` → all green; the whole suite `uv run pytest` → 415 passed; `uv run ruff check app tests alembic` clean (on Windows, `uv` may need to be on `PATH`)
  - dry look at the corpus without Azure: `cd apps/api && uv run python -c "from app.ai import ingest; d = ingest.load_corpus(ingest.default_corpus_dir()); print(len(d), d[0].source, '|', d[0].title)"` → a count above 0 and a `docs/…` source with a real heading as the title
  - with `AZURE_AI_EMBEDDING_DIMENSIONS` set to a value that does not match your deployment, `uv run python -m app.ai.ingest` prints `ingest stopped: the embedding deployment returns vectors of N dimensions, but AZURE_AI_EMBEDDING_DIMENSIONS is M …`, exits `2`, and the index is not touched (needs the Azure settings, so it is part of the human step below)
- **needs_human:**
  - if `team-assistant-docs` already exists at another dimension, its owner deletes it (the agent never deletes the index)
  - confirm the project `.env` has `AZURE_AI_EMBEDDING_DIMENSIONS=3072`
  - before the first real ingestion: if `team-assistant-docs` was ever ingested with an earlier version of this job, its owner deletes it so it is rebuilt cleanly (old chunks keep their old ids and would duplicate)
  - run the first ingestion yourself once the Azure settings are in `apps/api/.env` — `uv run python -m app.ai.ingest` — and check the index has documents with a `title` (the endpoint that does this over HTTP is item 3.3)
- **notes:**
  - 2026-10-06 — planned. Uses `feature-scaffold` target `rag`.
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — implemented by Claude. Files: `app/ai/ingest.py` (corpus loader, titles, dimension guards, exit codes), `app/ai/tools/retrieve.py` (`title`), `app/ai/config.py` (`CORPUS_DIR`), tests `tests/ai/test_ai_ingest_corpus.py` + updated ones + `FakeIndexClient`, `.env.example`, `environment-variables.md`, `docs/architecture/ai.md`, api `CLAUDE.md`/`README.md`, `.claude/rules/70-ai.md`, the `feature-scaffold` skill (CLI text only), CHANGELOG, api 0.6.0. Reviews: code-reviewer (4 should-fix: all applied — rollout/upgrade notes, honest exit-code and dimension wording, `Nothing was uploaded` reworded, BOM/fence-length/tab handling) and security-reviewer (no critical or high; applied: file-name fallback sanitised, index without a vector field refused, invalid UTF-8 fails the run instead of being replaced). Mutation-checked: 17 mutations, all caught. Not done here by design (item 3.5): symlink and outside-root checks, stale-chunk removal, ingest-call timeouts, hidden-character stripping. Not changed: embedding calls are not wrapped in `model_call_span()` — retrieval already behaves the same; say if you want rule 70 applied to the job.
  - 2026-10-06 — confirmed by Yousef Wahid. The first real ingestion against Azure has not been run yet (needs `AZURE_*` in `apps/api/.env`).

### 3.3 — Ingest endpoint

- **status:** done
- **depends_on:** [3.1, 3.2, 3.5]
- **layers:** [endpoint, ai]
- **acceptance:**
  - `POST /v1/admin/ingest` with the right `x-admin-key` → `202 { "status": "started" }` and ingestion runs in the background (the corpus from 3.2)
  - a second call while one runs → `409 ingest_running` (in-process, single replica — decision 19)
  - a missing or wrong key → `401`; if `INGEST_ADMIN_KEY` is not set the endpoint also returns `401` (fails closed); the key is compared in constant time and never logged
  - the outcome (counts, duration, success or error kind) is logged and emitted as a bounded event; no document text
  - the route is `/v1`-prefixed, echoes `x-trace-id`, and is **not** exposed through the web BFF
  - `INGEST_ADMIN_KEY` has a placeholder entry in `apps/api/.env.example` and `docs/reference/environment-variables.md`
  - an `INGEST_ADMIN_KEY` shorter than 32 characters is treated as not set (`401`, fails closed) and logs one warning naming only the reason kind, never the key or its length
  - `x-admin-key`, `admin_key` and `ingest_admin_key` are on the log redaction list, with a test that a log line carrying each shows `[redacted]`
  - `docs/operations/runbooks/` has a short procedure to rotate the key (set the new value, restart, check the old key now gets `401`)
  - an unexpected failure of the background run is reported by a bounded error kind in the log and the outcome event, never by the exception text (which can carry service URLs or response bodies)
  - the corpus has a size cap: a run over 1,000 files or 50 MB in total refuses before it embeds anything (a clear one-line reason, nothing uploaded or removed); the numbers are configurable and documented (threat model decision G7)
- **how_to_test:**
  - `uv run --directory apps/api pytest` — expect **927 passed, 4 skipped** (new: `tests/test_admin_ingest.py`, 67 tests); `ruff format --check` and `ruff check` clean; `docs/reference/openapi.json` current (`tests/test_openapi_export.py`)
  - **the guard, offline** (no Azure needed): put `INGEST_ADMIN_KEY=<40 random characters>` in your own `apps/api/.env`, run the api (`uv run --directory apps/api uvicorn app.main:app`), then
    - `curl -si -X POST localhost:8000/v1/admin/ingest` → `401 {"error":"unauthorized","trace_id":"0c70…"}` and the same id in `x-trace-id`
    - the same with `-H "x-admin-key: wrong"` → `401`; with a key of 31 characters in `.env` (and that key in the header) → `401` and ONE log line `admin key is not usable…` with `reason: too_short` (no key, no length); with the key unset → `401` and `reason: not_set`
    - repeat a wrong key 20 times in a minute → one `admin call refused` log line, not twenty
    - with the right key but **no** Azure settings → `503 {"error":"ai_unavailable",…}` and nothing started
  - **a real run** (needs the dev Azure settings, see `needs_human`): `curl -si -X POST -H "x-admin-key: $INGEST_ADMIN_KEY" localhost:8000/v1/admin/ingest` → `202 {"status":"started"}` at once; a second call within the run → `409 {"error":"ingest_running",…}`; the log then shows `ingest finished` with counts and seconds (no text, no paths) and one `ingest.completed` event (`outcome: success`); check the index has the chunks
  - **the size cap:** `INGEST_MAX_FILES=1 INGEST_MAX_BYTES=1000 uv run --directory apps/api python -m app.ai.ingest` → exit 2 with one line `ingest stopped: the corpus is too large: … over the cap of 1 (INGEST_MAX_FILES) …`, nothing uploaded
  - **not behind the BFF:** `curl -si -X POST localhost:3000/api/v1/admin/ingest` (web running) → `404`
  - prove the tests bite: in `app/admin.py` drop the length check, compare with `==`, drop the throttle; in `routers/admin.py` drop the `dependencies=[…]` or the config checks; in `ingest_job.py` drop the lock check, a kind, or log `str(exc)`; in `ingest.py` make a cap `>=` or run the scan/index setup on the loop; in `logging_config.py` drop one of the three names: the matching tests fail (28 such changes tried, all caught; the one that "survived" was logging the text under the key `message`, which structlog overwrites)
- **needs_human:**
  - set a real `INGEST_ADMIN_KEY` (at least 32 random characters) in your own `.env` — I did not read or write any `.env`; for a deployed environment the key and its Container App wiring **do not exist in `infra/` yet**: the use-case Key Vault secret + the api Container App env entry (value never in a parameter file) need an infra roadmap item, and until then a deployed api answers `401` to everyone
  - run the first ingestion against dev (`curl -X POST -H "x-admin-key: …" localhost:8000/v1/admin/ingest`) and check the index is populated; needs the Azure AI and Search settings in your `.env`
  - on the first real failure, check in Application Insights that no span of the Search/embedding call carries the exception text (endpoint URLs, response bodies): I verified the log and the event only; the Azure SDK and `httpx` spans were not tested against a real failure
  - decide whether the runtime and the ingest job use separate Search identities (threat model G13: runtime = Search Index Data Reader, ingest = Contributor); if yes, an entry for the cloud team in `infra/grant-request.md`
- **notes:**
  - 2026-10-06 — planned. Delegate to `observability-instrumenter` for the event.
  - 2026-10-06 — from the api 3.1 threat model (S5, G8, G13): key hardening and the redaction entries added to acceptance; now also depends on 3.5 (ingest hygiene). The 32-character minimum is a default chosen here — change it if the team prefers another.
  - 2026-10-06 — from the api 3.2 security review: the CLI prints a traceback for unexpected errors, which is fine for a person at a terminal; the endpoint must not.
  - 2026-10-07 — proposed decisions of the threat model §8a (Claude, on Yousef Wahid's instruction to decide; ADR-0015 is still for him to accept): the corpus-size cap above was added (G7); the runtime and the ingest job use separate Search identities in the deployed environment (G13) — a request for the cloud team, not authored here.
  - 2026-10-07 — from the api 4.2 security review: the api has no request-size cap (item 5.5); the ingest route takes no body, so it is not affected, but any admin route added later should not rely on the BFF to cap sizes.
  - 2026-10-09 — started by Claude. ADR-0015 was accepted on 2026-10-07 (the first `needs_human` line below is done).
  - 2026-10-09 — implemented by Claude, awaiting test by Yousef Wahid. Files: `app/admin.py` (the key guard), `app/routers/admin.py`, `app/ai/ingest_job.py` (the background run, lock, outcome), `app/ai/ingest.py` (`run()` split into `ingest_corpus()`; corpus cap; scan and index setup in worker threads), `app/ai/config.py` (`INGEST_MAX_FILES`, `INGEST_MAX_BYTES`), `app/logging_config.py` (3 names), `app/routers/v1.py`, `tests/test_admin_ingest.py`, the regenerated `docs/reference/openapi.json` + `apps/web/src/lib/api-types.ts`, `.env.example`, `docker-compose.yml`, `environment-variables.md`, `http-api.md`, `ai.md`, `apps/api/CLAUDE.md`, rule 70, the new runbook `rotate-ingest-admin-key.md`, `CHANGELOG.md` + `pyproject.toml`/`uv.lock` (0.13.0). Reviews: `code-reviewer` and `security-reviewer`; no CRITICAL/HIGH.
  - 2026-10-09 — fixed after review: the corpus scan and the index setup ran on the event loop (now in worker threads); refused calls were logged one line per request (now once a minute per reason, with the count left out); a cancelled run reported as `unexpected` (now `cancelled`); a failing reporter could end the task with an exception (now swallowed); the cap's message named a cap that was not set (now only the one exceeded); a test-only parameter removed from `start()`; weak and order-dependent tests tightened (structlog state restored, trace id on the outcome line asserted).
  - 2026-10-09 — DECISIONS: the 50 MB cap is 50 MiB (52,428,800 bytes); the endpoint answers `503 ai_unavailable` before starting when Search or embeddings are not configured (a failure minutes later in a log is worse); a refusal is logged at most once a minute per reason; the key's minimum length is a length rule only (no entropy check).
  - 2026-10-09 — not done, recorded: (1) the key has no deployed path — an infra item (Key Vault secret, Container App env entry, no value in params) is needed, and the runbook says so; (2) the lock is per process (api 5.2); `infra/` should keep the api at one replica until then; (3) the cap pass reads sizes first and the files later, so a file that grows in between is limited only by the per-file cap (2 MB) — it needs write access to the corpus directory; (4) a web test that no `admin` route is ever added to the BFF; (5) the runbook's multi-revision caveat and the unverified span text (see `needs_human`); (6) the G13 separate Search identity.
  - 2026-10-09 — version 0.13.0 (MINOR: a new endpoint and settings; the command is unchanged).
  - 2026-10-09 — confirmed by Yousef Wahid.

### 3.4 — Corpus baked into the api image

- **status:** done
- **depends_on:** [3.3]
- **layers:** [ai]
- **acceptance:**
  - the api image contains the repo's `docs/**/*.md` at `/app/corpus` and the ingest endpoint reads it there when `APP_ENV` is not `local`
  - the build works with the image's real build context (the api image is built from `apps/api`, while `docs/` is at the repo root — adjust the context, `.dockerignore` and `docker-compose.yml` together)
  - the final image still runs as a non-root user, and `.env*` stays out of the image
  - before the corpus is copied into the image, a check fails the build when a corpus file contains a secret-looking string (`AKIA…`, `sk-…`, `ghp_…`, `eyJ…`, a private-key header); it prints the file name and the pattern kind, never the matched text
- **how_to_test:**
  - `uv run --directory apps/api pytest` — expect **1021 passed, 5 skipped** (new: `tests/test_corpus_image.py`, 88 tests with 1 skipped on Windows that runs the Dockerfile's `find` on Linux; `test_config.py` cases for a blank `APP_ENV`); `ruff format --check` and `ruff check` clean
  - **the secret check, without Docker:** `uv run --directory apps/api python scripts/check_corpus_secrets.py ../../docs` → `corpus secret check: clean`, exit 0. Then put a fake key in a scratch folder (a file `a.md` holding `AKIA` immediately followed by 16 capital letters or digits) and run it on that folder → `a.md:1: looks like aws_access_key_id`, exit 1, and **no part of the key** in the output
  - **the corpus choice:** `APP_ENV=prod` with a `/app/corpus` directory present reads it; `APP_ENV=local` (or unset or blank) reads the repository's `docs/`; `CORPUS_DIR` wins over both — pinned by `TestCorpusDirectory`
  - **the image — needs Docker or CI** (I could not build it here): from the repository root `docker build --file apps/api/Dockerfile --tag team-assistant-api .` — the build **fails** at the `corpus` stage if a document holds a secret-looking string (try one in a scratch doc and delete it again) and succeeds otherwise. Then `docker run --rm --entrypoint sh team-assistant-api -c 'id -un; find /app/corpus -name "*.md" | wc -l; ls -ld /app/corpus; ls -a /app'` → `appuser`, 55 (the number of `.md` files under `docs/` today), `root root` on `/app/corpus`, and no `.env` in `/app`. `docker compose build api` works too
  - prove the tests bite: in the scanner drop a pattern, the allow-list, or the NUL/BOM check; in `ingest.py` ignore the image corpus or use it locally; in the Dockerfile drop the check, ignore its failure, use `\;` instead of `+`, keep hidden files, copy `docs` raw, chown the corpus, drop `USER`, copy an `.env`, or bake `CORPUS_DIR`; in compose, the workflows or `.dockerignore` put the old context or drop `**/.env`: the matching tests fail (41 such changes tried; 2 survived and the tests were tightened until they were caught)
- **needs_human:**
  - build the image with Docker (not installed on this machine) or let CI build it — `ci.yml` builds it on every pull request; pushing an image is always a human step. **Nothing about the Dockerfile has been run**: I checked it by reading, by two reviews, and by running its `find` / `cp` step by hand with GNU tools
  - **`apps/api/.dockerignore` is no longer read** (the build context is the repository root, so the root `.dockerignore` governs). Deleting it is your call — reply "delete it" and I will remove it, or keep it as it is; I did not touch it
  - review the workflow edits: `ci.yml` and `infra.yml` now build with `--file apps/api/Dockerfile … .`, `infra.yml` also runs on a push that changes `docs/**`, and its image job checks out with `persist-credentials: false`
  - review whether the corpus scope is right (decision 14: `docs/**/*.md`, including the template's own docs) — the whole corpus becomes searchable by everyone who can use the chat (threat model G7)
- **notes:**
  - 2026-10-06 — planned. Not needed for local runs; only for a deployed container.
  - 2026-10-06 — from the api 3.1 threat model (G7): secret scan and corpus-scope review added.
  - 2026-10-09 — started by Claude. ADR-0013 was accepted on 2026-10-07 (the second `needs_human` line is done).
  - 2026-10-09 — implemented by Claude, awaiting test by Yousef Wahid. Files: `apps/api/Dockerfile` (new `corpus` stage, repo-root context), `apps/api/scripts/check_corpus_secrets.py` (new), `app/ai/ingest.py` (`IMAGE_CORPUS_DIR`, `default_corpus_dir`), `app/config.py` (a blank `APP_ENV` is `local`), `.dockerignore`, `docker-compose.yml`, `.github/workflows/ci.yml` + `infra.yml`, `tests/test_corpus_image.py`, `tests/test_config.py`, `.env.example`, `environment-variables.md`, `ai.md`, `overview.md`, `workflow.md`, `getting-started.md`, `security/README.md`, `template-roadmap.md`, `README.md`, `apps/api/README.md`, `apps/api/CLAUDE.md`, rule 70, `CHANGELOG.md` + `pyproject.toml`/`uv.lock` (0.14.0). Reviews: `code-reviewer` and `security-reviewer`; no CRITICAL.
  - 2026-10-09 — fixed after review: `find -exec cp \;` would not fail the build on a failed copy (now `-t /corpus {} +`); the corpus was owned by the service user (now root-owned); the root `.dockerignore` had root-anchored cache patterns and no key/certificate files; docs still said each app's `.dockerignore` governs; the scanner missed PGP blocks, Azure shared-access and SAS keys, Slack, Google and Stripe keys, bearer tokens, URL credentials, `password=` assignments and UTF-16 / NUL files (all added, with fake secrets and look-alikes), and had no way to allow the AWS docs' example (an exact-string allow-list now); a blank `APP_ENV` was an empty string, not `local` (found by a test); weak Dockerfile tests tightened.
  - 2026-10-09 — DECISIONS: the build context is the repository root (a named BuildKit context would need new flags in every caller); the secret check is a stdlib script in the Dockerfile's own stage, so no secret-looking document reaches a layer; `/app/corpus` is read only when `APP_ENV` is not `local` (as the acceptance says), so an api run by `docker compose` (default `APP_ENV=local`) reads no baked corpus unless `CORPUS_DIR=/app/corpus` is set; version 0.14.0 (MINOR: `docker build apps/api` no longer works, but only CI and compose build it).
  - 2026-10-09 — not done, recorded: the scanner is line-based, so a secret wrapped over two lines or base64-encoded inside a document is not seen (api 5.7 adds the CI scan, and a maintained tool is preferable to growing these patterns); the corpus is still all of `docs/**/*.md` including the template's own docs (decision 14, the scope review below); `python:3.14-slim` is not pinned by digest; `**/test/` and `**/generated/` in the root `.dockerignore` would silently drop a `docs/**/test/` directory from the corpus (none exists today).
  - 2026-10-09 — confirmed by Yousef Wahid.

### 3.5 — Ingest hygiene (paths, sources, stale chunks, timeouts)

- **status:** done
- **depends_on:** [3.2]
- **layers:** [ai]
- **acceptance:**
  - every candidate path is resolved first; a path that resolves outside the corpus root (a symlink, `..`) is skipped and counted, never read
  - only `.md` files are read — `.txt` is no longer accepted (decision 14)
  - each chunk's `source` is the repo-relative path with `/` separators (`docs/…`); it is never absolute and never contains a newline
  - after a run that completes, chunks whose source file is no longer in the corpus are removed from the index (only chunks of this index, selected by source) and counted; a run that fails part-way removes nothing
  - the embedding and search calls of ingestion each have an explicit timeout (default 30 s, from config), so one hung call cannot hold the run or use up the worker threads
  - logs and the result carry counts and durations only (read, skipped outside the root, uploaded, removed, seconds) — never a document's text or path list
  - covered with fake search and embedding clients and a temporary corpus (the symlink case is skipped on a machine that cannot create symlinks)
  - the text of every file is cleaned before chunking: control and format characters (zero-width, bidirectional marks, NUL) are removed and HTML comments are dropped; the number of files that changed is logged (a count, never text)
  - `source` also goes through the title cleaning rules: printable characters only, no newline, bounded length (3.2 already makes it repo-relative and cleans the title)
- **how_to_test:**
  - `cd apps/api && uv run pytest tests/ai` → all green; the whole suite `uv run pytest` → **794 passed, 4 skipped** (the 4 are the symlink tests, skipped on a machine that cannot create symlinks — the same rule is also covered by tests that make the path resolver say a file is outside, which run everywhere); `uv run ruff check app tests alembic` clean (on Windows, `uv` may need to be on `PATH`)
  - a dry look at what the load would do with the real `docs/`, no Azure needed: `uv run python -c "from app.ai import ingest; l = ingest.scan_corpus(ingest.default_corpus_dir()); print(len(l.documents), l.skipped_outside_root, l.skipped_unsafe, l.skipped_unresolved, l.cleaned)"` → `54 0 0 0 2` (2 files change by the cleaning: `adr/README.md` has a 44-character HTML comment, `reference/commands.md` one invisible character)
  - the tests read like the acceptance: `tests/ai/test_ai_ingest_hygiene.py` — paths (`TestPathsStayInsideTheRoot`, `TestWhatElseIsSkipped`), sources (`TestSources`), cleaning (`TestCleanText`, `TestHtmlComments`, `TestScanCleansAndCounts`), timeouts (`TestTimeouts`), stale chunks (`TestPruneStale`) and whole runs (`TestRun`)
  - with the Azure settings in `apps/api/.env` (the human step below): `uv run python -m app.ai.ingest --no-prune` first, then `uv run python -m app.ai.ingest` → one line `documents=… chunks=… uploaded=… failed=0 skipped_outside_root=0 skipped_unsafe=0 skipped_unresolved=0 cleaned=2 removed=0 removal_failed=0 removal_refused=0 seconds=…`; rename or delete a doc, run again → `removed` is its chunk count; point `--corpus` at a small folder → the run uploads, refuses to remove most of the index, prints why on stderr and exits 1 (`--force-prune` overrides)
  - prove the tests bite: in `app/ai/ingest.py` drop the outside-root check, the `.md`-target check, the hidden-file check, the empty-after-cleaning guard, the prune brake, the `docs/` prefix filter, the comment pass or one of the timeouts: the matching tests fail (19 such changes tried, all caught; one more — reading the unresolved instead of the resolved path — only differs for an in-root symlink, which this machine cannot create)
- **needs_human:**
  - the **first real ingestion** (as for 3.2): confirm `AZURE_AI_EMBEDDING_DIMENSIONS=3072`, have the owner of any old `team-assistant-docs` index delete it, then run it yourself — `uv run python -m app.ai.ingest` — and check that the stale-chunk pass works against the real service: the listing is one paged search and has not been run live, so look for the log line `ingest index listing incomplete` (it means the service listed fewer chunks than it holds; the pass then removes less, never more)
  - decide whether a **corpus size cap** is needed before the ingest endpoint (3.3): a file over 2 MB is skipped, but the number of files and the total size are not limited, and embedding cost grows with them
  - the threat model's gap G6 is settled here (3.5) instead of 3.2 — I updated rows T2, T3, I7, AI-11, G6's checklist line and the residuals; please read them when you review the threat model (residuals: hidden content such as reference-link comments, `<script>`, `<div hidden>` and encoded blobs is not covered; the prompt stays the real control)
  - reply **done** to close the item, or tell me what to change
- **notes:**
  - 2026-10-06 — planned from the api 3.1 threat model (T3, G3, G6). Runs after 3.2 so it changes the one ingestion code path once; 3.3 depends on it so the endpoint never exposes the unhardened job.
  - 2026-10-06 — after api 3.2: `.txt` is already dropped, `source` is already `docs/<relative path>`, and a non-UTF-8 file already fails the run — only the path-escape/symlink check, stale-chunk removal, the call timeouts, content cleaning and the `source` sanitising remain here. The threat model's pre-launch checklist says G6 is settled in 3.2; it is settled here instead (3.5) — update that line when this item closes.
  - 2026-10-07 — after api 4.2: `build_search_client` now carries a 30 s connect/read timeout (`AI_RETRIEVAL_TIMEOUT_SECONDS`) and 2 retries, and ingestion uses that client, so the search upload is already bounded. The embedding calls and the index client (`build_index_client`) still need their own timeouts here.
  - 2026-10-07 — started by Claude.
  - 2026-10-07 — implemented by Claude, awaiting test by Yousef Wahid. Files: `app/ai/ingest.py` (`scan_corpus`, `clean_text`, `strip_html_comments`, `prune_stale`, `_bounded`, `--no-prune`/`--force-prune`), `tests/ai/test_ai_ingest_hygiene.py` (new, 70-odd tests), `test_ai_ingest_corpus.py`, `fakes.py`, `apps/api/CHANGELOG.md` + `pyproject.toml` + `uv.lock` (0.10.0), api `CLAUDE.md`/`README.md`, `docs/architecture/ai.md`, `.claude/rules/70-ai.md`, the threat model. Reviews: `code-reviewer` and `security-reviewer`, both run; the findings below were verified (most reproduced) before acting.
  - 2026-10-07 — fixed after review: **HIGH** — comment stripping skipped code fences, and a comment that spans a fence line, or a line that only looks like a fence (a backtick in the info string), kept a payload that no renderer shows; comments are now dropped everywhere, fences included. **MEDIUM** — the stale-chunk pass could empty the index (a corpus that is empty after cleaning, or a wrong `CORPUS_DIR`): it now stops before any call when nothing is left, only removes chunks whose source starts with `docs/`, refuses to remove at least 20 chunks that are more than half the index's own (`--force-prune` overrides), and removes nothing when a file could not be resolved; a link to a non-`.md` target (say a `.env`) was read; hidden files, hard links and files over 2 MB are skipped; variation selectors and blank letters were not removed and `\x0b`/`\x0c`/NEL glued words; the heading pattern was quadratic on a line of spaces (an ingest could hang on one hostile file) — long lines are skipped; the timeout message promised "nothing was removed" and a timed-out call can still finish; an unreadable file raised a traceback with its path; a file name with two spaces was refused as unsafe.
  - 2026-10-07 — DECISIONS: removing a hidden payload is preferred to keeping legitimate text, so a `<!--` written in inline code takes the text up to the next `-->` with it (the `cleaned` count is the only sign); the zero-width joiner and non-joiner are removed too (joined emoji split, Persian and Indic text loses its joiners); hidden characters and comments are removed and counted rather than failing the run (the threat model said "fail the run"; this item's acceptance said clean); `skipped_unresolved` is counted apart from `skipped_outside_root` because a transient I/O error must not look like an escape and must not trigger removals; the embedding calls share the 30 s retrieval budget (the model client's own 60 s with retries is longer).
  - 2026-10-07 — not done, recorded: no lock between two ingests (api 5.2); `ensure_index` and `scan_corpus` are blocking and run on the event loop (fine for the command line; wrap them in a thread when 3.3 calls them from a route); the stale-chunk listing is unverified against a live index; a partial report is not printed when a timeout stops the run.
  - 2026-10-07 — confirmed by Yousef Wahid.

## Phase 4 — Chat with citations (F1)

### 4.1 — Assistant prompt and eval cases

- **status:** done
- **depends_on:** []
- **layers:** [ai]
- **acceptance:**
  - the system prompt lives in `app/ai/prompts/` and says retrieved text is **data, not instructions**, answers only from the provided context, says so when the context lacks the answer, and names the sources it used
  - eval cases (in `tests/evals/cases.json`) cover: a grounded answer, an out-of-context question, and a corpus passage that tries to inject an instruction (the answer must ignore it)
  - the cases run offline against the fake model
- **how_to_test:**
  - `cd apps/api && uv run pytest tests/evals` → 19 passed (8 cases × 2 checks, plus the guardrail, coverage and "obeying model fails" tests); `uv run pytest` → 362 passed; `uv run ruff check app tests alembic` clean
  - read `apps/api/app/ai/prompts/system.md`: data-not-instructions rule, "do not know" rule, `[n]` citations, context last
  - optional, needs a real deployment (see needs_human): `AI_EVAL_LIVE=true uv run pytest tests/evals/test_evals.py::test_eval_case` — the same cases graded against the real model
- **needs_human:**
  - read the prompt wording and say what to change (tone, scope, language)
  - optional: run the live eval tier once the dev Foundry deployment is reachable (`AZURE_AI_*` in `apps/api/.env`); the offline tier cannot show how the real model reacts to an injected passage
- **notes:**
  - 2026-10-06 — planned.
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — implemented by Claude. Files: `app/ai/prompts/system.md`, `tests/evals/cases.json` + `test_evals.py`, `docs/architecture/ai.md`, CHANGELOG, api 0.5.1 (`pyproject.toml`, `uv.lock`). No code outside the prompt and tests changed. Reviews: code-reviewer (5 should-fix: 2–5 applied — narrowed forbidden strings, one clear citation rule, the real `(no context available)` marker, the header format described; 1 applied as honest wording, since offline answers are scripted) and security-reviewer (no critical; prompt wording applied; code-side findings recorded on 4.2). Mutation-checked: inverted injection rule, context sent twice, marker dropped/changed, text after the slot.
  - 2026-10-06 — confirmed by Yousef Wahid (offline tier ran green on their machine: 19 passed). The live eval tier was not run.

### 4.2 — Ask in a conversation (JSON)

- **status:** done
- **depends_on:** [1.1, 2.1, 3.2, 4.1, 4.4]
- **layers:** [endpoint, ai]
- **acceptance:**
  - `POST /v1/conversations/{id}/ask` with `{ "question": string }` → `200` `{ message_id, answer, citations }`, where `citations` is `[{ title, path }]`: the retrieved documents (`top_k = 5`) whose `[n]` the answer cites, each once, in citation order — an answer that cites nothing (for example "I do not know") returns `[]`; the same list is what is stored with the assistant message
  - the user message is stored first; the assistant message (with citations JSON and token count) is stored after the answer; the conversation title and `updated_at` follow decision 11
  - the model receives the last 10 messages of the conversation as history, then the context, then the question
  - empty question → `422`; over 4,000 characters → `413 payload_too_large`; unknown conversation → `404`
  - a model or search failure → `503 ai_unavailable`, the user message is kept and no assistant message is stored
  - model calls go through the template's client and span; no question, answer or retrieved text appears in logs, spans, metrics or events
  - the call has a 60 s timeout and the standard retries; `x-trace-id` echoed; OpenAPI and `http-api.md` regenerated
  - history is rebuilt from stored rows with `user` → human message and `assistant` → AI message only; any other role is dropped and a stored row never becomes a system message (test with a fake row of another role)
  - the answer has an output bound: `max_tokens` from `AI_MAX_OUTPUT_TOKENS` (default 1024, documented in `.env.example` and `environment-variables.md`)
  - the retrieval and query-embedding calls each have an explicit timeout (default 30 s) and a failure maps to the same `503 ai_unavailable`
  - the stored assistant text is capped so that 500 messages stay under the BFF's response limit; the cut is logged by kind only (the value is chosen at implementation, with a test at the boundary)
  - at most `AI_MAX_CONCURRENT_ASKS` asks (default 4, documented in `.env.example` and `environment-variables.md`) run at once in the process; one more is answered `429 busy` before anything is stored; the slot is released on every exit path (answer, error, cancellation)
- **how_to_test:**
  - `cd apps/api && uv run pytest` → 649 passed (the new files: `tests/test_ask_api.py`, `tests/ai/test_ai_ask_support.py`); `uv run ruff check app tests alembic` clean (on Windows, `uv` may need to be on `PATH`)
  - `uv run pytest tests/test_ask_api.py -v` → the test names read as the acceptance: answer and citations, order of events (question committed before the model), 422/413/404/429/503, history, cleaning, the cap, the slot cap with a real three-at-once overlap, no content in logs
  - `docs/reference/http-api.md` → section `POST /v1/conversations/{id}/ask`; `docs/reference/openapi.json` lists the route with 200/404/413/422/429/500/503
  - live check (human, see needs_human): after 1.2 is applied and the index is ingested, `curl -X POST localhost:8000/v1/conversations/<id>/ask -H "content-type: application/json" -d '{"question":"Which port does the api listen on?"}'` → `200` with `answer` and `citations` (create the conversation first with `POST /v1/conversations`)
- **needs_human:**
  - a live answer needs 1.2 applied and an ingested index (3.2's job run by you, or 3.3 once it exists); an agent checks only with fakes. Please try one real question and tell me whether the answer and its citations look right
  - decide the Part B correction: ARCHITECTURE B4a lists `POST …/ask` errors as `404`, `413`, `503`; the route also returns `422` and `429 busy` (a proposal for the team to apply — not edited here)
- **notes:**
  - 2026-10-06 — planned. The web hop timeout must exceed 60 s plus retries (rule 70) — note for the web roadmap.
  - 2026-10-06 — from the 1.1 security review: the request schema caps `question` at 4,000 characters, `citations` come only from retrieval results (never from the request body), each citation is `{title, path}` strings of bounded length, and `token_count` is non-negative.
  - 2026-10-06 — from the 4.1 security review (code-side, so they belong here, not in the prompt): (1) `Answer.sources` and the `sources` frame are every retrieved chunk's source, and the `[n]` markers in the answer are never checked — validate `[n]` against the number of passages and build the stored/returned `citations` from retrieval only, never from model-written paths; (2) `format_context` inserts `content` and `source` raw, so a passage can forge a `[n] (path)` header — delimit passages unforgeably (collapse newlines in `source`, neutralise header-like lines in `content`); (3) history: map stored rows with role `user` → `HumanMessage` and `assistant` → `AIMessage` only, never a `SystemMessage` from stored text, with a test; note that `[n]` is renumbered every turn, so old `[n]` in history do not refer to this turn's passages; (4) check how the web renders answers (markdown images/links) before the first live answer.
  - 2026-10-06 — from the api 3.1 threat model (AI-5, G2, G3) and the 4.1 security review: history role mapping, output bound, retrieval timeouts and the stored-size cap moved from notes into acceptance; passage delimiting and citation checks are item 4.4, which this item now depends on. Open decision (not planned): a cap on concurrent asks, so one client cannot spend the whole quota — the BFF/ingress rate limit (G1) belongs in the web roadmap.
  - 2026-10-06 — decisions taken by Claude on the developer's instruction to follow its recommendations: (1) citations are the cited subset, not every retrieved passage; (2) a process-wide cap on concurrent asks is in this item (the BFF/ingress rate limit stays a web-layer need); (3) `trace_id` is **not** stored on message rows — it would need a new migration on tables already applied, and logs carry the id per request (revisit with auth; threat model G14, R3).
  - 2026-10-06 — after api 4.4: `ask()` already logs `retrieved`/`cited`/`invalid` as counts — the route must not log them again, and must build the stored/returned `citations` with `cited_chunks` + `citation_of` (never from model text). `citation_of` returns one bounded line each for `title` and `path` but both are **untrusted text** (`<`, `(`, `javascript:` survive): the web must render them as plain text only (a text node, never an `href`, never markdown). `invalid_citations` is a lower bound. The security review also noted that the answer text itself is returned unrewritten, so the web must not auto-load images or render raw HTML from it.
  - 2026-10-07 — started by Claude.
  - 2026-10-07 — implemented by Claude. Files: `app/routers/ask.py` (new), `app/ai/history.py` and `app/ai/limits.py` (new), `app/ai/config.py`, `app/ai/client.py`, `app/ai/telemetry.py`, `app/ai/tools/retrieve.py`, `app/routers/v1.py`, tests `tests/test_ask_api.py` + `tests/ai/test_ai_ask_support.py` (new) and updated ones, docs (`http-api.md`, `environment-variables.md`, `ai.md`, api `CLAUDE.md`, `.claude/rules/70-ai.md`), `.env.example` (api and root), `docker-compose.yml`, regenerated `openapi.json` and the web's `api-types.ts`, CHANGELOG, api 0.8.0. Reviews: code-reviewer and security-reviewer. Fixed from them: the search client had azure-core's defaults (300 s, 10 retries) so a timed-out search stranded its thread — now bounded and tested against the real client; the `gen_ai.chat` span exported provider error text — fixed and tested with a real tracer provider; control characters made the "always fits 8 MiB" claim false (6-byte JSON escapes) — now cleaned, the false claim and its vacuous test removed; lone surrogates in answers; replayed history rows are cut; compose and root env files and the rules text synced; hang guards and order assertions in the tests. Mutation-checked: 37 mutations, all caught (one test hung on a regression and was fixed with a timeout). Not done here, planned as 5.4 and 5.5: a byte bound on a thread read and a request-size cap in the api; per-client rate limits belong at the BFF/ingress (web roadmap).
  - 2026-10-07 — confirmed by Yousef Wahid. The live check against the real model and index has not been run yet (needs migration 1.2 applied and an ingested index).

### 4.3 — Streaming ask

- **status:** done
- **depends_on:** [4.2]
- **layers:** [endpoint, ai]
- **acceptance:**
  - `POST /v1/conversations/{id}/ask/stream` returns `text/event-stream` with frames `sources` (`[{ title, path }]`), then `token`\* (`{ text }`), then `done` (`{ message_id, input_tokens, output_tokens, citations }`, where `citations` is the cited subset exactly as in 4.2 and as stored) or `error` (`{ error_kind, error, trace_id }`) — extending the template's frames (B3)
  - the user message is stored before streaming starts; the assistant message is stored once the whole answer exists, **before** `done` is sent (`done` carries its `message_id`); an `error` frame or a client disconnect before then stores no assistant message
  - validation and not-found cases behave as in 4.2 (answered as normal JSON errors before the stream starts)
  - `x-trace-id` is on the response headers, and the `error` frame carries the same trace id; no content in telemetry
  - the frame shapes are documented in `http-api.md`
  - the `sources` frame still lists every retrieved passage, as the template's stream does; a client shows it early and replaces it with `citations` when `done` arrives
  - a stream holds one of the `AI_MAX_CONCURRENT_ASKS` slots until it ends (`done`, `error` or disconnect); an extra stream before the first byte is answered `429 busy` as normal JSON
- **how_to_test:**
  - `cd apps/api && uv run pytest` → 722 passed (the new files: `tests/test_ask_stream_api.py`, `tests/test_ask_stream_hardening.py`); `uv run ruff check app tests alembic` clean (on Windows, `uv` may need to be on `PATH`)
  - `uv run pytest tests/test_ask_stream_api.py -v` and `tests/test_ask_stream_hardening.py -v` → the names read as the acceptance: frame order and shapes, the user message committed before the model, nothing stored on error or disconnect, the 422/413/404/429 JSON errors before the first byte, error frames (model, search, mid-stream, blank, database), the cap, the slot held and released, no content in logs or spans
  - `docs/reference/http-api.md` → section `POST /v1/conversations/{id}/ask/stream` (frames, error kinds, time bounds, framing); `docs/reference/openapi.json` lists the route with a `text/event-stream` 200 and 404/413/422/429/500
  - live check (human, see needs_human): `curl -N -X POST localhost:8000/v1/conversations/<id>/ask/stream -H "content-type: application/json" -d '{"question":"Which port does the api listen on?"}'` → `sources` first, then `token` frames appearing one by one, then `done` (use `-N` so curl does not buffer)
- **needs_human:**
  - the live check needs the same prerequisites as 4.2 (migration applied, index ingested, `AZURE_*` set); please watch that tokens arrive incrementally, not all at once
  - nothing else: the BFF stream routes and the page are web 3.1–3.3
- **notes:**
  - 2026-10-06 — planned. The web roadmap's chat item depends on this one (`api:4.3`) and on the frame shapes above.
  - 2026-10-06 — from the 1.4 security review: if a database error escapes after the stream has started (for example persisting the assistant message at the end), `UnhandledErrorMiddleware` re-raises it and the FastAPI instrumentation records `str(exc)` and the traceback on the server span — including SQL Server's own message, which can quote the text. The stream must therefore catch database errors itself and end with the bounded `error` frame, never re-raise after `http.response.start`; add a test that fails a DB write mid-stream and checks the exported server span carries no driver text.
  - 2026-10-06 — decisions taken with 4.2 (see its notes): `done` carries the cited `citations`, and streams count against the concurrency cap. **Web impact (not edited here):** web 3.1/3.2/3.3 and the hand-declared `DoneFrame` type must add `citations`, show the early `sources` list and swap it for `done.citations` — to add when planning or implementing those items.
  - 2026-10-06 — after api 4.4: at `done`, accumulate the streamed text and call `cited_chunks` / `invalid_citation_numbers` once (same helpers as `ask()`), put the cited `citations` in the `done` frame, and log the three counts once. The streaming module does not do this yet.
  - 2026-10-07 — started by Claude.
  - 2026-10-07 — implemented by Claude. Files: `app/routers/ask.py` (stream route and frames), `app/ai/streaming.py` (`iter_answer`, `SseResponse` with a write timeout, frame escaping), `app/ai/limits.py` (`take_slot`), `app/observability.py` (no per-frame spans), tests `tests/test_ask_stream_api.py` + `tests/test_ask_stream_hardening.py` (new) and additions, docs (`http-api.md`, `ai.md`, `60-observability.md`, `70-ai.md`, api `CLAUDE.md`), CHANGELOG, api 0.9.0, regenerated `openapi.json` and the web's `api-types.ts`. Reviews: code-reviewer and security-reviewer, both done. Fixed from them: the slot release could be skipped if closing the generator was cancelled; the stream had no overall deadline and no write timeout (a slow provider or a non-reading client could hold a slot for ever); one span per frame (about 1,000 per answer); U+2028/2029/0085/007F left raw in frames; an authentication failure was visible to clients; token counts were not validated; failed streams were invisible (HTTP 200) so a bounded `ask.stream.failed` event was added; `sources` and `done.citations` used different document keys for source-less passages; the template `stream_answer` did not close its iterator; hang guards and a span-based test for the database failure after the first byte (the 1.4 note — now exercised with a control run that shows the instrumentation really records the text). Mutation-checked: 34 mutations, all caught.
  - 2026-10-07 — **for the web roadmap (not edited here):** `DoneFrame` needs `citations` and its token counts are `number | null`; the `error_kind` set is `timeout`, `rate_limited`, `network`, `error`, `empty_answer`, `persist_failed`; the page shows `sources` early and replaces it with `done.citations`; titles and paths are plain text; the SSE parser should split lines only on CR, LF and CRLF; the BFF forwards the stream unchanged and its hop timeout must exceed the 4-minute worst case; and it must handle `429 busy` and the JSON errors before the first byte.
  - 2026-10-07 — confirmed by Yousef Wahid. The live check against the real model and index has not been run yet (needs migration 1.2 applied and an ingested index).

### 4.4 — Unforgeable passage headers and citation checks

- **status:** done
- **depends_on:** [4.1]
- **layers:** [ai]
- **acceptance:**
  - in the context given to the model, each passage's number is assigned by the code, and its `source` and `content` cannot imitate a passage header: newlines and header-like text in `source` are removed, and lines in `content` that look like a header (`[n] (…)`) or the `Context:` marker are neutralised
  - a helper reports which `[n]` markers in an answer point to no passage; the ask routes use it to log a bounded count (no text) and never to rewrite the answer
  - the citations that are returned or stored always come from the retrieved passages, never from text the model wrote, and are only the passages whose valid `[n]` the answer cites (a helper returns them from the answer text and the passage list)
  - offline eval cases: a passage that forges a header (the harness checks the rendered prompt, not only the scripted answer) and an answer that cites a number that does not exist
- **how_to_test:**
  - `cd apps/api && uv run pytest tests/ai tests/evals` → all green; the whole suite `uv run pytest` → 529 passed; `uv run ruff check app tests alembic` clean (on Windows, `uv` may need to be on `PATH`)
  - `uv run pytest tests/ai/test_ai_citations_context.py -v` → the test names list what is covered: citation parsing, fence rules, bogus numbers, every lookalike header and `Context:` form that must be escaped, and the ordinary lines that must not be
  - `uv run pytest tests/ai/test_ai_citations_context.py -k long_runs -v` → 2 passed in well under a second (the old fence regex needed about a second for 4,000 backticks and minutes for 100,000)
  - `uv run pytest tests/evals -v` → every eval case now also checks its rendered header lines and its cited documents; read the new case `decorated-and-fullwidth-headers-are-escaped` in `tests/evals/cases.json`
  - read the "Known limits" section of the 0.7.0 entry in `apps/api/CHANGELOG.md`
- **needs_human:**
  - nothing blocks the next item. For later: the live eval (api 5.3) is what shows how a real model copes with the forged headers; and whoever builds the web display of citations (web 3.x) must render `title` and `path` as plain text only — see the notes
- **notes:**
  - 2026-10-06 — planned from the api 3.1 threat model (S6, AI-2, AI-3) and the 4.1 security review, which found that `format_context` inserts passages raw and that `[n]` markers are never checked. Changes `app/ai/graph.py`; the prompt (4.1) is unchanged. Open design point for 4.3: the stream sends `sources` (all retrieved passages) before the first token, so the web page shows every retrieved source, not only the cited ones; whether to narrow the stored citations to the cited numbers is a product decision to make then.
  - 2026-10-06 — design point settled (see 4.2 notes): stored/returned citations are the cited subset; the stream's early `sources` frame stays all retrieved.
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — implemented by Claude. Files: `app/ai/citations.py` (new), `app/ai/graph.py` (`format_context`, `_header_source`, `_neutralise`, `Answer.cited`/`invalid_citations`, one log line), tests `tests/ai/test_ai_citations_context.py` (new) + `tests/evals/test_evals.py` and `cases.json` (`expect_cited`, an independent header check, a decorated-headers case), `docs/architecture/ai.md`, `.claude/rules/70-ai.md`, api `CLAUDE.md`, CHANGELOG, api 0.7.0. Reviews: code-reviewer and security-reviewer. Both found the same real defect: the first version used a regex for code fences that was quadratic-to-cubic (1 s at 4,000 backticks) and would stall the event loop; it is now a linear line scanner with a 200,000-character cap and tests that fail fast on a regression. Also fixed: lookalike headers behind quote/list marks, zero-width or bidi characters, fullwidth forms and bare `[2]` lines; ASCII-only digits; a set for seen markers; the version (MINOR) and the over-claimed changelog. Mutation-checked: 50 mutations over three rounds, all caught (one run was interrupted because the deliberately reintroduced regex hung the suite; that test now fails in seconds). Accepted limits are listed in the CHANGELOG ("Known limits").

## Phase 5 — Hardening

- 2026-10-07 — confirmed by Yousef Wahid (ran the offline tests on their machine: 222 passed for `tests/ai` + `tests/evals`, and the long-run test passed in under a second). The live eval tier has not been run.

### 5.1 — Dev smoke runbook

- **status:** todo
- **depends_on:** [1.2, 3.3, 4.3]
- **layers:** [docs]
- **acceptance:**
  - `docs/operations/runbooks/team-assistant-dev-smoke.md` lists, in order, the commands and expected results to verify dev end to end: tables exist, ingest returns `202`, the index has documents with titles, a conversation can be created, an answer streams with citations, and the same `trace_id` appears in logs and Application Insights
  - it includes the failure signatures for the likely misses (wrong embedding dimension, missing admin key, ODBC driver missing)
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - run the smoke against dev and report the result
- **notes:**
  - 2026-10-06 — planned. Delegate to `runbook-writer`.
  - 2026-10-07 — from the api 4.3 reviews, add to the smoke: frames reach the client one by one through the BFF and any ingress (they do through uvicorn); after a client leaves in the middle of the final write, the database connection pool is still healthy (a cancelled `commit()` with `mssql+aioodbc` was not verified offline).

### 5.2 — Ingest lock that works across replicas

- **status:** todo
- **depends_on:** [3.3]
- **layers:** [model, migration, endpoint]
- **acceptance:**
  - a second ingest started from another replica while one runs also returns `409 ingest_running`; a lock left behind by a crashed run expires on its own
  - the lock is a small table (or row) in the project database, created by a new migration
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - apply the migration to dev after review and confirmation
- **notes:**
  - 2026-10-06 — planned, **only needed before the api runs more than one replica** (decision 19). Safe to leave `todo` until a deployment exists.

### 5.3 — More injection eval cases and a live eval run

- **status:** todo
- **depends_on:** [4.2, 4.4]
- **layers:** [ai]
- **acceptance:**
  - `tests/evals/cases.json` gains cases for: a passage that asks for a markdown image or link carrying the question (the answer must not contain image or link syntax), a passage that tells the model to cite a number that does not exist, and a poisoned earlier assistant turn in the history that tells the model to drop its rules; cases can carry a `history`, and the harness passes it through
  - the offline harness still proves what the model receives (rules first, one system message, nothing from history or passages in the system role beyond the context slot)
  - the live tier (`AI_EVAL_LIVE=true`) grades every case; the result is recorded in this item's notes, and a case that fails live is fixed in the prompt or code before the item is closed
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - run the live eval tier against the dev Foundry deployment (`AZURE_AI_*` in `apps/api/.env`) and report the result — an agent cannot call the real model
- **notes:**
  - 2026-10-06 — planned from the api 3.1 threat model (G12). The offline tier only pins the plumbing and the prompt's wording; this is the item that shows how the real model behaves under injection.

### 5.4 — Bound a thread read by bytes

- **status:** done
- **depends_on:** [4.2]
- **layers:** [endpoint]
- **acceptance:**
  - `GET /v1/conversations/{id}` never returns more than about 4 MiB of message text: when the 500 most recent messages are larger than that, it returns fewer (the most recent ones, oldest first) and `truncated` is `true`
  - the bound is on bytes as the response is written (UTF-8 length of content plus citations), not on characters, and the check does not read more rows than the 500 it already reads
  - tests build a thread of 500 maximum-length emoji messages with long citation titles and prove the response stays under the bound; the BFF's 8 MiB response limit is never reached
  - `docs/reference/http-api.md` and OpenAPI describe the byte bound next to the 500-message bound
- **how_to_test:**
  - `uv run --directory apps/api pytest` — expect **1052 passed, 5 skipped** (new: `tests/test_conversation_thread_bytes.py`, 31 tests; the repository tests that used plain strings as rows now use message-like rows); `ruff format --check` and `ruff check` clean; `docs/reference/openapi.json` current (`tests/test_openapi_export.py`) and `pnpm -C apps/web typecheck` clean with the regenerated `api-types.ts`
  - **the worst thread, as the response is written:** `uv run --directory apps/api pytest tests/test_conversation_thread_bytes.py -k worst -v` — 501 rows of 4,000 emoji with five 200-emoji citation titles each (about 11 MB if returned whole) come back as about 190 messages, `truncated: true`, and `len(response.content)` is between 90 % and 100 % of 4 MiB, far under the BFF's 8 MiB
  - **the count never understates:** `TestTheCountNeverUnderstates` compares the count with the bytes `MessageOut` really writes for quotes, backslashes, tabs and newlines, NUL, other control characters, U+2028, accents and emoji — never below, never more than 200 bytes above
  - **with a real database (optional):** in the dev database store a thread of long emoji answers (an ordinary ask cannot — answers are capped at 4,000 characters, which is the point), open it with `curl -s localhost:8000/v1/conversations/<id> | python -c "import sys; print(len(sys.stdin.buffer.read()))"` → under 4,194,304 and `"truncated": true`
  - prove the tests bite: in `app/repositories/conversations.py` count characters instead of bytes, use `ensure_ascii=True`, drop the citations or the 256-byte allowance, make the bound `>=`, stop guaranteeing the newest message, skip the `truncated` flag, `continue` instead of `break`, drop the count slice, keep the oldest instead of the newest, or double the bound; in the router stop passing `max_bytes`; in OpenAPI drop the byte wording: the matching tests fail (15 such changes tried; one survived — skipping a message that does not fit instead of stopping — and a mixed-size test now catches it)
- **needs_human:**
  - the web app still says "Showing the most recent 500 messages." (`apps/web/src/components/chat/ChatWorkspace.tsx`) whatever cut the thread; a byte-cut thread shows about 190. It is the web layer's text (and its test, `ChatWorkspace.test.tsx`, and a comment in `mocks/conversations.test.ts` and `lib/conversations-bff.ts`), so I did not change it here — say "web chore" and I will reword it to "Showing only the most recent messages." with a web patch release, or add it to the web roadmap
  - nothing else
- **notes:**
  - 2026-10-07 — planned from the api 4.2 reviews: a per-message cap (4,000 characters, 4 bytes each) cannot guarantee that 500 messages fit the BFF's 8 MiB limit; ordinary threads do, but a thread of emoji-only messages with 200-character titles can reach about 10 MB, after which the BFF returns an upstream error and the conversation cannot be opened. Changes the api 2.1 endpoint, hence a separate item.
  - 2026-10-09 — started by Claude.
  - 2026-10-09 — implemented by Claude, awaiting test by Yousef Wahid. Files: `app/repositories/conversations.py` (`MAX_THREAD_BYTES`, `MESSAGE_OVERHEAD_BYTES`, `message_wire_bytes`, the loop in `get_conversation_with_messages`), `app/routers/conversations.py` (passes both caps; the `truncated` description), `tests/test_conversation_thread_bytes.py` (new), `tests/test_repositories_conversations.py`, stale comments in `app/routers/ask.py` and `tests/test_ask_api.py`, the regenerated `docs/reference/openapi.json` + `apps/web/src/lib/api-types.ts` (a description only), `docs/reference/http-api.md`, the threat model (I1), `apps/api/CLAUDE.md`, `CHANGELOG.md` + `pyproject.toml`/`uv.lock` (0.15.0). Reviews: `code-reviewer` and `security-reviewer`; no CRITICAL or HIGH.
  - 2026-10-09 — fixed after review: the count is tested never to fall below what the response holds (control characters, NUL, escapes, U+2028); the byte cut stops at the first message that does not fit (no gap); the endpoint order test checks the order; stale comments that called 5.4 future work; docs said "the newest few hundred" (it is about 190) and "no thread can exceed 8 MiB" without the caveat that the newest message is always returned and a message is capped at 4,000 characters on write.
  - 2026-10-09 — DECISIONS: the bound is on the JSON the response writes (escapes count) plus a fixed 256 bytes a message, not on characters; the newest message is always returned even over the bound, so a thread is never empty (the bound holds because writes are capped); the byte check runs on the 501 rows the query already reads, so no extra query or row; version 0.15.0 (MINOR: `truncated` now also means cut by bytes, no field changed).
  - 2026-10-09 — not done, recorded: (1) a stored message that is itself over 8 MiB (written by other means; threat model G2) is still returned whole and the BFF turns that into a 502; capping content on write closes it; (2) a stored `citations` value of the wrong shape still makes the read a clean 500 (pre-existing, only the ask path writes); (3) the rows are still all read from the database before the cut, so the bound saves bytes on the wire and CPU, not database transfer; (4) the web notice wording (above).
  - 2026-10-09 — confirmed by Yousef Wahid.

### 5.5 — Request-size cap in the api

- **status:** done
- **depends_on:** []
- **layers:** [endpoint]
- **acceptance:**
  - a request body larger than a configured limit (default 64 KB; `/v1/conversations/{id}/ask` and `POST /v1/conversations` need far less) is refused with `413 payload_too_large` and the standard error body, and the oversized body is not buffered whole: the limit is checked from `Content-Length` and while the body streams (a chunked upload is cut off at the limit)
  - applies to every `/v1` route that reads a body, as one middleware; operational routes are unaffected; `x-trace-id` is echoed on the `413`
  - the limit is documented in `.env.example` and `environment-variables.md`
  - tests: a body over the limit with and without `Content-Length`, a body just under it, a streamed body with no length, and no effect on a normal ask
- **how_to_test:**
  - `cd apps/api && uv run pytest tests/test_body_limit.py` → **45 passed**; the whole suite `uv run pytest` → **839 passed, 4 skipped** (the 4 are the ingest symlink tests); `uv run ruff check app tests alembic` clean (on Windows, `uv` may need to be on `PATH`)
  - against the running api (`uv run uvicorn app.main:app --port 8000`; no Azure settings are needed for these): a 70 KB body is refused whichever way it arrives — I ran both —
    ```
    python -c "import sys; sys.stdout.write('{\"question\":\"' + 'q'*70000 + '\"}')" > big.json
    curl -i -X POST http://localhost:8000/v1/conversations/5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e01/ask -H "content-type: application/json" --data-binary @big.json
    curl -i -X POST http://localhost:8000/v1/conversations/5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e01/ask -H "content-type: application/json" -H "transfer-encoding: chunked" --data-binary @big.json
    ```
    both answer `413` with `{"error":"payload_too_large","trace_id":"…"}` and an `x-trace-id` header equal to that id (the first carries `connection: close`); `curl -i http://localhost:8000/ping` is still `200`
  - lower the limit and watch it move: `MAX_REQUEST_BODY_BYTES=100` in `apps/api/.env`, restart, and `curl -i -X POST http://localhost:8000/v1/conversations -H "content-type: application/json" -d '{"title":"a title that is well over a hundred bytes long, repeated: ... ... ... ... ... ... ... ..."}'` is `413`, a short `{"title":"x"}` is not; remove the line afterwards
  - the logs show one line per refusal, `request body refused kind=declared` or `kind=streamed` — no size, no path, no body
  - prove the tests bite: in `app/body_limit.py` ignore the declared length, the streamed count (or make it per chunk instead of cumulative), the exemption of `/ping`/`/info`/`/health`, the `root_path` strip or the largest-of-repeated-lengths rule; in `app/main.py` drop the middleware; in `app/config.py` let `0`, a typo or a huge value through: the matching tests fail (17 such changes tried, all caught)
- **needs_human:**
  - decide whether the ingress (when one exists) should also cap request sizes; the api cap is a second line of defence (Container Apps ingress has no request-size setting of its own that I know of — check with the cloud team)
  - decide whether the api needs a **read deadline** for slow bodies: the cap bounds size, not speed, and uvicorn has no body-read timeout, so a client that trickles bytes holds a connection until the ingress's 240 s limit; locally and in compose nothing bounds it. I did not add one (it is a separate change to how the server is run); say so and I will plan an item
  - reply **done** to close the item, or tell me what to change
- **notes:**
  - 2026-10-07 — planned from the api 4.2 security review: FastAPI reads and parses the whole body before a route's own check runs (so `413` for a long question arrives only after the body is in memory several times over). The BFF caps its own requests (web roadmap 3.1), but the api may also be reached directly (threat model S3).
  - 2026-10-07 — started by Claude.
  - 2026-10-07 — implemented by Claude, awaiting test by Yousef Wahid. Files: `app/body_limit.py` (new: `BodyLimitMiddleware`, `BodyTooLarge`), `app/main.py` (wiring: Trace → BodyLimit → UnhandledError), `app/config.py` (`MAX_REQUEST_BODY_BYTES`, `read_body_limit`), `tests/test_body_limit.py` (new, 45 tests), `.env.example`, `docker-compose.yml`, `docs/reference/environment-variables.md` and `http-api.md`, api `CLAUDE.md`, `CHANGELOG.md` + `pyproject.toml` + `uv.lock` (0.11.0). Reviews: `security-reviewer` and `code-reviewer`, both run; the findings below were verified before acting. Also run for real: the api started locally and both a declared and a chunked 70 KB body answered `413` with the contract body.
  - 2026-10-07 — fixed after review: **MEDIUM** — the cap only looked at paths starting `/v1/`, so a server started with a `root_path` skipped it, `/v1` itself and the next API version (`/v2`) were uncapped, and so was anything added later: every route is capped now except the three operational ones, compared without the `root_path`; a repeated `Content-Length` counts as its largest; a value above 1 MiB for the setting is the default (a huge value switches the cap off in practice); the declared-length refusal says `connection: close`; the log is info, not warning (an unauthenticated client could otherwise produce warnings at request rate); the changelog claimed uvicorn bounds a slow body — it does not, now worded accurately; a few weak or duplicated tests removed or tightened.
  - 2026-10-07 — DECISIONS: the limit is 64 KB because the largest legitimate body is a 4,000-character question, about 48 KB when every character is a JSON surrogate-pair escape, and the web BFF caps its requests at the same 64 KiB (keep `MAX_REQUEST_BODY_BYTES` at or above about 48 KB); the setting is read once when the app is created (a change needs a restart); version 0.11.0 (MINOR: a new limit, backward compatible for any body under 64 KB).
  - 2026-10-07 — not done, recorded: a read deadline for slow bodies (above); `POST /v1/conversations` does not declare a `413` response in the OpenAPI schema (only the ask routes do; the behaviour is the same on every route) — declaring it everywhere would mean regenerating `docs/reference/openapi.json` and the web's generated types, which I left for web 4.2.
  - 2026-10-07 — confirmed by Yousef Wahid.

### 5.6 — Fit the ask deadline inside the ingress limit

- **status:** done
- **depends_on:** []
- **layers:** [ai]
- **acceptance:**
  - the default `AI_MAX_RETRIES` is 1 (was 2), so one answer's worst case — the model's timeout times (retries + 1), plus the two retrieval steps — is 180 s at the other defaults, inside the 240 s request timeout of Azure Container Apps ingress; the worst case is computed in one place (not repeated as a number in several) and covered by a test that fails if the defaults add up to more than 220 s
  - `docs/reference/http-api.md`, `.env.example`, `environment-variables.md`, `apps/api/CLAUDE.md` and `.claude/rules/70-ai.md` give the new defaults and say why; the web BFF's 5 minute hop timeout is left as an upper bound
- **how_to_test:**
  - `cd apps/api && uv run pytest tests/ai/test_ai_config_client.py tests/test_ask_deadline_startup.py tests/test_ask_json_deadline.py` → green; the whole suite `uv run pytest` → **857 passed, 4 skipped**; `uv run ruff check app tests alembic` clean (on Windows, `uv` may need to be on `PATH`)
  - the number: `uv run python -c "from app.ai.config import get_ai_settings as g; s = g(); print(s.max_retries, s.worst_case_seconds, s.fits_the_ingress_limit)"` → `1 180.0 True`; with `AI_MAX_RETRIES=2` in the environment it prints `2 240.0 False`
  - the startup warning: in PowerShell, `$env:AI_MAX_RETRIES=2; uv run uvicorn app.main:app --port 8000` → one WARNING `the ask deadline is longer than the ingress request timeout` with `worst_case_seconds=240.0 limit_seconds=220.0 ingress_seconds=240.0` and nothing else; without the variable there is no such line
  - the deadline itself: `tests/test_ask_json_deadline.py` (a graph that never answers ends in `503 ai_unavailable` after the configured worst case, not after 30 s) and `tests/test_ask_stream_hardening.py` (the stream ends in an `error` frame, kind `timeout`)
  - prove the tests bite: in `app/ai/config.py` put the default retries back to 2, drop the first attempt or one retrieval step from the formula, raise the margin; in `app/routers/ask.py` stop using `worst_case_seconds` (stream) or drop the `asyncio.timeout` (JSON route); in `app/main.py` drop the warning or let it raise: the matching tests fail (12 such changes tried, all caught)
- **needs_human:**
  - none for the code. Two things to know: a deployment that sets `AI_MAX_RETRIES=2` (or longer timeouts) in its own environment keeps the old behaviour and gets the startup warning — check the Container Apps environment variables when the api is deployed (`infra/main.bicep` sets none today)
  - the web's source comments still say the api's worst case is "about 4 minutes" (`apps/web/src/lib/ask-bff.ts`, the two ask route files, `apps/web/CLAUDE.md`); the values they describe (the 5 minute hop timeout) are unchanged and still an upper bound, only the wording is stale — say so and I will fix the comments as a web chore
  - reply **done** to close the item, or tell me what to change
- **notes:**
  - 2026-10-07 — planned from the ingress timeout decision of the threat model §8a (Azure Container Apps ingress: fixed 240 s request timeout, not configurable). Proposed by Claude on Yousef Wahid's instruction to decide; a project that wants more retries can set the variable and accepts the limit.
  - 2026-10-07 — started by Claude.
  - 2026-10-07 — implemented by Claude, awaiting test by Yousef Wahid. Files: `app/ai/config.py` (`DEFAULT_MAX_RETRIES` 1, `INGRESS_REQUEST_TIMEOUT_SECONDS`, `ASK_WORST_CASE_LIMIT_SECONDS`, `AISettings.worst_case_seconds`, `fits_the_ingress_limit`), `app/routers/ask.py` (the stream's deadline now comes from the property; the JSON route got the same deadline), `app/main.py` (the startup warning), `tests/ai/test_ai_config_client.py`, `tests/test_ask_deadline_startup.py` and `tests/test_ask_json_deadline.py` (new), `.env.example` (both), `docker-compose.yml`, `docs/reference/environment-variables.md` and `http-api.md`, `docs/architecture/ai.md`, `.claude/rules/70-ai.md`, api `CLAUDE.md`, the threat model (two lines), `CHANGELOG.md` + `pyproject.toml` + `uv.lock` (0.12.0). Review: `code-reviewer` (no security review: no boundary, external call or data path changed).
  - 2026-10-07 — fixed after review: **MEDIUM** — the JSON `/ask` route had no overall deadline, so "180 s" was true only for the stream: the client's timeouts are per phase and a retry's wait (a 429's `Retry-After`) is not counted, so a slow provider could have outlasted the ingress limit and the caller would have seen a dropped connection instead of `503 ai_unavailable`; the JSON route now has the same `asyncio.timeout(worst_case_seconds)` and a test; `apps/api/CLAUDE.md` (named in the acceptance) was not updated; two stale "about 4 minutes" in `http-api.md` and the threat model; the formula's assumptions (each retrieval step has its own timeout; one model call, no tool loop) and what the 40 s margin is for are now written next to it.
  - 2026-10-07 — DECISIONS: the margin is 40 s (limit 220 s of the 240 s): history read and question store come before the deadline starts and the answer store and `done` after it; version 0.12.0 (MINOR: a changed default and a new warning); the startup check swallows a mistyped AI setting (it is reported where the setting is used) rather than stopping the app.
  - 2026-10-07 — not done, recorded: stale comments in `apps/web` (above); the Azure Search client has its own retry count (2, bounded by the asyncio timeout), separate from `AI_MAX_RETRIES`.
  - 2026-10-07 — confirmed by Yousef Wahid.

### 5.7 — Secret scan of the corpus in CI

- **status:** todo
- **depends_on:** []
- **layers:** [docs]
- **acceptance:**
  - a CI job (in `.github/workflows/ci.yml`, on every pull request) scans `docs/**/*.md` for credentials — private key blocks, `AKIA…` and similar cloud key shapes, `ghp_`/`github_pat_` tokens, `sk-` style keys, JWT-shaped strings, connection strings with a password, long high-entropy values next to `key`/`secret`/`token`/`password` — and fails the build with the file and line **kinds** only (never the value)
  - an allow-list file for known false positives (placeholders such as `00000000-0000-…` and `<your-key>`), reviewed in the same pull request that adds to it
  - the scan's patterns are tested with a few fake secrets and a few look-alikes (offline, in the api's or a script's test suite), and the job is described in `docs/reference/commands.md` and the threat model (G7)
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - none
- **notes:**
  - 2026-10-07 — planned from the threat model §8a (G7): the corpus stays `docs/**/*.md`, and the scan is the control. Proposed by Claude on Yousef Wahid's instruction to decide. A real secret scanner (a maintained tool) is preferable to hand-written patterns if the team already uses one; that is a stack decision to confirm when this item starts.

### 5.8 — Conversation purge runbook

- **status:** todo
- **depends_on:** [1.2]
- **layers:** [docs]
- **acceptance:**
  - `docs/operations/runbooks/` has a procedure to remove one conversation (or all conversations older than a date) from the Azure SQL database by hand — the exact reviewed statements (parameterised, deleting messages before the conversation), the permission needed, how to check the result, and the reminder that the stored data are shared and may hold personal data (ADR-0014)
  - it says whether `pii-purge.md` covers the database (it covers telemetry) and links the two; it states the retention decision: none automatic for now, revisit when auth lands
  - no statement is run by an agent; the runbook is reviewed like any runbook (`runbook-writer`)
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - a person with write access to the database runs it when a removal is requested
- **notes:**
  - 2026-10-07 — planned from the threat model §8a (G11): proposed by Claude on Yousef Wahid's instruction to decide.
