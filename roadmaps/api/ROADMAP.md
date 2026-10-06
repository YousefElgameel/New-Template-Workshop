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

- **status:** todo
- **depends_on:** []
- **layers:** [docs]
- **acceptance:**
  - `docs/security/threat-models/team-assistant.md` (STRIDE) covers: shared conversations holding personal data (ADR-0014), the ingest endpoint and its shared secret (ADR-0015), indirect prompt injection through the corpus, quota abuse, and the all-IP dev firewall (decision 22)
  - each threat has a mitigation that maps to a roadmap item or an accepted risk
  - ADR-0014 and ADR-0015 link to it
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - review and accept the threat model before 3.3 ships
- **notes:**
  - 2026-10-06 — planned. Delegate to `threat-modeler` (inputs listed in the 2026-10-06 architecture review).

### 3.2 — Ingestion with titles at 3072 dimensions

- **status:** todo
- **depends_on:** []
- **layers:** [ai]
- **acceptance:**
  - ingestion stores, for every chunk, its source path and a `title` (the file's first `# ` heading, else the file name) in the index, and retrieval returns both
  - the corpus root is configurable: `./docs` locally, `/app/corpus` when deployed (decision 1, ADR-0013); only `.md` files under `docs/` are read (decision 14)
  - the index vector dimension comes from `AZURE_AI_EMBEDDING_DIMENSIONS` (3072 for `text-embedding-3-large`, decision 20); if the existing index has a different dimension, ingestion stops with a clear error naming both values — it never silently reuses or recreates it
  - ingestion stays idempotent and logs counts and durations only, never document text
  - covered with fake search and embedding clients — no live service
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - if `team-assistant-docs` already exists at another dimension, its owner deletes it (the agent never deletes the index)
  - confirm the project `.env` has `AZURE_AI_EMBEDDING_DIMENSIONS=3072`
- **notes:**
  - 2026-10-06 — planned. Uses `feature-scaffold` target `rag`.

### 3.3 — Ingest endpoint

- **status:** todo
- **depends_on:** [3.1, 3.2]
- **layers:** [endpoint, ai]
- **acceptance:**
  - `POST /v1/admin/ingest` with the right `x-admin-key` → `202 { "status": "started" }` and ingestion runs in the background (the corpus from 3.2)
  - a second call while one runs → `409 ingest_running` (in-process, single replica — decision 19)
  - a missing or wrong key → `401`; if `INGEST_ADMIN_KEY` is not set the endpoint also returns `401` (fails closed); the key is compared in constant time and never logged
  - the outcome (counts, duration, success or error kind) is logged and emitted as a bounded event; no document text
  - the route is `/v1`-prefixed, echoes `x-trace-id`, and is **not** exposed through the web BFF
  - `INGEST_ADMIN_KEY` has a placeholder entry in `apps/api/.env.example` and `docs/reference/environment-variables.md`
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - set a real `INGEST_ADMIN_KEY` in your own `.env` (and Key Vault when deployed)
  - run the first ingestion against dev (`curl -X POST -H "x-admin-key: …" localhost:8000/v1/admin/ingest`) and check the index is populated
  - accept ADR-0015
- **notes:**
  - 2026-10-06 — planned. Delegate to `observability-instrumenter` for the event.

### 3.4 — Corpus baked into the api image

- **status:** todo
- **depends_on:** [3.3]
- **layers:** [ai]
- **acceptance:**
  - the api image contains the repo's `docs/**/*.md` at `/app/corpus` and the ingest endpoint reads it there when `APP_ENV` is not `local`
  - the build works with the image's real build context (the api image is built from `apps/api`, while `docs/` is at the repo root — adjust the context, `.dockerignore` and `docker-compose.yml` together)
  - the final image still runs as a non-root user, and `.env*` stays out of the image
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - build the image with Docker (not installed on this machine today; optional tool) or let CI build it — pushing an image is always a human step
  - accept ADR-0013
- **notes:**
  - 2026-10-06 — planned. Not needed for local runs; only for a deployed container.

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

- **status:** todo
- **depends_on:** [1.1, 2.1, 3.2, 4.1]
- **layers:** [endpoint, ai]
- **acceptance:**
  - `POST /v1/conversations/{id}/ask` with `{ "question": string }` → `200` `{ message_id, answer, citations }`, where `citations` is `[{ title, path }]` drawn from the retrieved documents (`top_k = 5`)
  - the user message is stored first; the assistant message (with citations JSON and token count) is stored after the answer; the conversation title and `updated_at` follow decision 11
  - the model receives the last 10 messages of the conversation as history, then the context, then the question
  - empty question → `422`; over 4,000 characters → `413 payload_too_large`; unknown conversation → `404`
  - a model or search failure → `503 ai_unavailable`, the user message is kept and no assistant message is stored
  - model calls go through the template's client and span; no question, answer or retrieved text appears in logs, spans, metrics or events
  - the call has a 60 s timeout and the standard retries; `x-trace-id` echoed; OpenAPI and `http-api.md` regenerated
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - a live answer needs 1.2 applied and an ingested index (3.3); an agent checks only with fakes
- **notes:**
  - 2026-10-06 — planned. The web hop timeout must exceed 60 s plus retries (rule 70) — note for the web roadmap.
  - 2026-10-06 — from the 1.1 security review: the request schema caps `question` at 4,000 characters, `citations` come only from retrieval results (never from the request body), each citation is `{title, path}` strings of bounded length, and `token_count` is non-negative.
  - 2026-10-06 — from the 4.1 security review (code-side, so they belong here, not in the prompt): (1) `Answer.sources` and the `sources` frame are every retrieved chunk's source, and the `[n]` markers in the answer are never checked — validate `[n]` against the number of passages and build the stored/returned `citations` from retrieval only, never from model-written paths; (2) `format_context` inserts `content` and `source` raw, so a passage can forge a `[n] (path)` header — delimit passages unforgeably (collapse newlines in `source`, neutralise header-like lines in `content`); (3) history: map stored rows with role `user` → `HumanMessage` and `assistant` → `AIMessage` only, never a `SystemMessage` from stored text, with a test; note that `[n]` is renumbered every turn, so old `[n]` in history do not refer to this turn's passages; (4) check how the web renders answers (markdown images/links) before the first live answer.

### 4.3 — Streaming ask

- **status:** todo
- **depends_on:** [4.2]
- **layers:** [endpoint, ai]
- **acceptance:**
  - `POST /v1/conversations/{id}/ask/stream` returns `text/event-stream` with frames `sources` (`[{ title, path }]`), then `token`\* (`{ text }`), then `done` (`{ message_id, input_tokens, output_tokens }`) or `error` (`{ error_kind, error, trace_id }`) — extending the template's frames (B3)
  - the user message is stored before streaming starts; the assistant message is stored only after `done`; an `error` frame or a client disconnect before `done` stores no assistant message
  - validation and not-found cases behave as in 4.2 (answered as normal JSON errors before the stream starts)
  - `x-trace-id` is on the response headers, and the `error` frame carries the same trace id; no content in telemetry
  - the frame shapes are documented in `http-api.md`
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - none beyond 4.2's live-check prerequisites
- **notes:**
  - 2026-10-06 — planned. The web roadmap's chat item depends on this one (`api:4.3`) and on the frame shapes above.
  - 2026-10-06 — from the 1.4 security review: if a database error escapes after the stream has started (for example persisting the assistant message at the end), `UnhandledErrorMiddleware` re-raises it and the FastAPI instrumentation records `str(exc)` and the traceback on the server span — including SQL Server's own message, which can quote the text. The stream must therefore catch database errors itself and end with the bounded `error` frame, never re-raise after `http.response.start`; add a test that fails a DB write mid-stream and checks the exported server span carries no driver text.

## Phase 5 — Hardening

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
