# Runbook — Team Assistant dev smoke

**When to use:** after the use-case infrastructure and the api migrations are on **dev**, after the first
deploy of the api and web, and again after any change to the database, the search index, the model
deployments, the ingress, or the ingest or ask paths. It proves the whole chain, in order: **tables → ingest →
index → conversation → streamed answer with citations → one `trace_id` across logs and Application Insights.**

A step that fails stops the run: every later step depends on the one before it. Each step lists the signature of
the likely miss and what to do about it.

> **No secret in this file or in your shell history.** The admin key and any connection string come from Key Vault
> (or your own `apps/api/.env`) into an environment variable you set for this shell only. Do not paste them into a
> chat, a ticket or a log. The api is on the internal network: run the `curl` lines from a machine that can reach it
> (a bastion, a jump host, or `az containerapp exec` into the web container), never by opening it to the internet.
> This procedure is run by a person; an agent cannot reach dev.

## Set up the shell

```bash
API=https://<api internal base URL>        # the api container app (internal)
WEB=https://<web base URL>                 # the web container app (what the team uses)
# read the admin key from Key Vault into the variable yourself; do not echo it:
read -rs ADMIN_KEY && export ADMIN_KEY
```

Every `curl` below prints the `x-trace-id` header with `-i`. Note the ones you will want in step 7.

## 0. Both services are up and observable

```bash
curl -si $API/health | head -20
curl -si $WEB/health | head -20
```

| Expect                                                                                  | If not                                                                                                                             |
| --------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| `200`, `"status":"ok"`, `"observability":"enabled"` on both, and an `x-trace-id` header | `"observability":"disabled"`: stop, run [observability-degraded](observability-degraded.md). Steps 7 and 8 cannot pass without it. |
| `curl $WEB/api/ping-backend` returns `200` with a `trace_id` field from the api         | `502`/`503`: the web cannot reach the api: check `API_BASE_URL` on the web and the api's ingress (internal), not the app code.     |

## 1. The tables exist

Run these in the dev database (the portal query editor, `sqlcmd`, or Azure Data Studio, as a person with read
access). Nothing here changes data.

```sql
SELECT name FROM sys.tables WHERE name IN ('conversations','messages','ingest_locks') ORDER BY name;
SELECT version_num FROM dbo.alembic_version;
```

| Expect                                                                                                                                                                              | If not                                                                                                                                                                                                                                                                                 |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| three rows (`conversations`, `ingest_locks`, `messages`) and `version_num` equal to the **latest** revision under `alembic/versions/` (`uv run --directory apps/api alembic heads`) | An empty result or an older revision: the migration has not been applied. Applying is the Environment-gated `migrate.yml` or a named human, never an agent. Until then: `POST /v1/conversations` is `500 internal_error` and `POST /v1/admin/ingest` is `503 ingest_lock_unavailable`. |

## 2. The api can reach the database (and the ODBC driver is there)

```bash
curl -si -X POST $API/v1/conversations -H 'content-type: application/json' -d '{"title":"smoke"}'
```

Expect `201` with an `id`. Keep the id as `CONV`; step 5 uses it.

| Signature                                                                                                                                                                                                                                                                      | Cause and fix                                                                                                                                                                                                                                                                                                                                                        |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `500 {"error":"internal_error"}`, and in `AppTraces` the line `unhandled exception` with `kind` `InterfaceError` (or `Error`) and `where` pointing into `pyodbc`/`aioodbc`; the container log may show `IM002` / "Data source name not found, and no default driver specified" | **ODBC Driver 18 is missing** from the image (or `driver=` in `DATABASE_URL` names another driver). The image installs `msodbcsql18` in its `Dockerfile`; rebuild from the repository's Dockerfile and check the `driver=ODBC+Driver+18+for+SQL+Server` part of the URL. The response never carries the driver's text; the **kind** and **where** are what you have. |
| the same `500` with `kind` `OperationalError` / `InterfaceError` and a login or network flavour in the container log                                                                                                                                                           | The firewall, the managed-identity login (`Authentication=ActiveDirectoryMsi`, the user's client id) or the contained SQL user is not set up. That is a human step on the database (`infra/grant-request.md`), not code.                                                                                                                                             |
| `500` with `kind` `ProgrammingError`                                                                                                                                                                                                                                           | The table is missing: go back to step 1.                                                                                                                                                                                                                                                                                                                             |

(The exact driver error text was not observed against dev when this was written; the signatures above are the usual
ones for these failures. If dev shows something else, add it here.)

## 3. Ingest starts

The key guards the route. The BFF never exposes it: call the **api** directly.

```bash
curl -si -X POST $API/v1/admin/ingest -H "x-admin-key: $ADMIN_KEY"
```

| Result                                    | Meaning                                                                                                                                                                                                                                                                                                                                             |
| ----------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `202 {"status":"started"}`                | The run is in the background. Go to step 4.                                                                                                                                                                                                                                                                                                         |
| `401 {"error":"unauthorized"}`            | **Missing or wrong admin key.** In Log Analytics: `admin key is not usable, so admin routes refuse every call` (`reason: not_set` or `too_short`) or `admin call refused` (`reason: wrong_key` / `no_key`). Fix the Key Vault secret or the container app's reference, restart the revision: [rotate-ingest-admin-key](rotate-ingest-admin-key.md). |
| `503 {"error":"ai_unavailable"}`          | `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_INDEX` or `AZURE_AI_EMBEDDING_DEPLOYMENT` (and `AZURE_AI_ENDPOINT`) is not set on the api. Nothing was started.                                                                                                                                                                                              |
| `503 {"error":"ingest_lock_unavailable"}` | The cross-replica lock row cannot be taken: the `ingest_locks` table is missing (step 1) or the database cannot be reached (step 2).                                                                                                                                                                                                                |
| `409 {"error":"ingest_running"}`          | A run is already in progress in this or another replica. Wait for it (up to `INGEST_MAX_RUN_SECONDS`, 30 minutes), or if its replica died, for the lock to expire (5 minutes).                                                                                                                                                                      |

The response does not say how the run ended. That is step 4.

## 4. The run finished cleanly

Wait a minute or two (a small corpus takes seconds; embedding is the slow part), then in Log Analytics:

```kusto
AppTraces
| where TimeGenerated > ago(30m) and AppRoleName == "team-assistant-api"
| where Message in ("ingest finished", "ingest failed")
| project TimeGenerated, Message, Properties
| order by TimeGenerated desc
```

and the event:

```kusto
AppEvents
| where TimeGenerated > ago(30m) and Name == "ingest.completed"
| project TimeGenerated, Properties
```

Expect one `ingest finished` with `outcome` `success`, `documents`/`chunks`/`uploaded` above zero, `failed` `0`.
`partial` means some chunks were not uploaded or not removed: read `failed`, `removal_failed`, `removal_refused`.

A failed run is `ingest failed` with an `error_kind` (a bounded word; never a document or an exception message):

| `error_kind`                                                                   | Signature and fix                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dimension_mismatch`                                                           | **Wrong embedding dimension.** Either the index already exists with vectors of another size than `AZURE_AI_EMBEDDING_DIMENSIONS`, or the embedding deployment returns another size (text-embedding-3-large is 3072). An existing index cannot be resized in place: set the variable to the size the index or the deployment really has, or ask the index's owner to delete the index so it is recreated. Run the command line once to read the exact message (`uv run --directory apps/api python -m app.ai.ingest`, which prints `ingest stopped: …` with both numbers). |
| `index_shape`                                                                  | The index exists but has no `content_vector` field: another team's index, or one made by hand. Its owner must delete it so ingestion can create it with the right shape.                                                                                                                                                                                                                                                                                                                                                                                                  |
| `not_configured`                                                               | A setting is missing (as the `503` in step 3, found later).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| `corpus_not_found` / `corpus_empty` / `corpus_too_large` / `corpus_unreadable` | The corpus in the image is missing, has no `.md`, is over `INGEST_MAX_FILES`/`INGEST_MAX_BYTES`, or has a file that is not UTF-8. The image bakes `docs/` in at build time (ADR-0013): rebuild, do not edit a running container.                                                                                                                                                                                                                                                                                                                                          |
| `timeout`                                                                      | A call to Search or the embedding deployment took longer than `AI_RETRIEVAL_TIMEOUT_SECONDS`, or the whole run passed `INGEST_MAX_RUN_SECONDS`. The index may be partly updated; ingestion is idempotent, so run it again once the service is healthy.                                                                                                                                                                                                                                                                                                                    |
| `lock_lost`                                                                    | The database could not renew the lock in time, or another replica took it over. Check step 2 and the replica count, then run again.                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `unexpected`                                                                   | Anything else; the container log has the exception **kind** only. Look at the surrounding `AppTraces` lines and `AppExceptions`.                                                                                                                                                                                                                                                                                                                                                                                                                                          |

## 5. The index has documents with titles

Look at the index itself, not only the log: in the portal, the AI Search service → **Indexes** → your index →
**Search explorer** (it uses your sign-in; do not copy a query key into a ticket).

| Check                      | Do                                                | Expect                                                                                                                                                   |
| -------------------------- | ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| the index has documents    | the index's overview shows a **Document count**   | equal to the `chunks` figure of step 4 (give the count a minute to settle)                                                                               |
| the documents have titles  | Search explorer, query `*`, look at a few results | each has `title` (the document's first heading, or its file name), `source` (`docs/<path>`), `chunk_index`, `content`; none has an empty or `null` title |
| the vector field is filled | the result of a document, field `content_vector`  | present (it is long; do not copy it anywhere)                                                                                                            |

An empty index after `outcome: success` points at the wrong index name (`AZURE_SEARCH_INDEX`) or a different
Search service than the one you are looking at.

## 6. A conversation can be created and read back

```bash
curl -si $WEB/api/v1/conversations -X POST -H 'content-type: application/json' -d '{"title":"smoke"}'
curl -si $WEB/api/v1/conversations | head -30
CONV=<id from the first response>
curl -si $WEB/api/v1/conversations/$CONV
```

Expect `201` with an `id`; the list contains it (newest first); the detail is `200` with `messages: []` and
`truncated: false`. This goes **browser-side path → web → api → database**, so it also proves the BFF route and
`API_BASE_URL`. (Step 2 proved the api and the database alone.) The conversations are shared until auth lands
(ADR-0014): anyone using the dev web sees yours; delete nothing, and use the title `smoke` so it is recognisable.

## 7. An answer streams in, with citations

First the non-streaming form (simpler to read), then the stream.

```bash
curl -si $WEB/api/v1/conversations/$CONV/ask -X POST -H 'content-type: application/json' \
  -d '{"question":"How do I run the tests?"}'
```

Expect `200` with `answer` (text with `[1]`-style markers) and `citations` (a list of `{title, path}`; each one a real
document of step 5). An answer with **no** citations to a question the docs cover means retrieval found nothing:
go back to step 5.

Now the stream, with the client reading as frames arrive (`-N` turns curl's own buffering off):

```bash
curl -siN $WEB/api/v1/conversations/$CONV/ask/stream -X POST -H 'content-type: application/json' \
  -d '{"question":"What is in the ingest runbook?"}'
```

Expect, in this order: `event: sources` once (every retrieved document), `event: token` many times, then
`event: done` with `message_id`, token counts and `citations`.

| Check                                                            | Expect                                                                                                                                                                                                                                                                                                                                |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **frames reach you one by one**, through the web and any ingress | the `token` lines **appear gradually** while the answer is generated, not all at once at the end. If they arrive in one burst, something between the api and you buffers (an ingress or proxy setting): the api sends `x-accel-buffering: no`, so look at the ingress, not at the app. (It streams one by one through uvicorn alone.) |
| an `error` frame instead of `done`                               | `{error_kind, error, trace_id}`: `ai_unavailable` with `timeout`, `rate_limited`, `network` or `error` is the model or Search (check the deployment, quota and the `AZURE_AI_*` settings); `internal_error` with `persist_failed` is the database (step 2). The `trace_id` is the one for step 8.                                     |
| `429 {"error":"busy"}`                                           | `AI_MAX_CONCURRENT_ASKS` asks are already in flight (the default is 4); retry.                                                                                                                                                                                                                                                        |
| `GET $WEB/api/v1/conversations/$CONV` afterwards                 | the question and the answer are both stored, the answer with its `citations`.                                                                                                                                                                                                                                                         |

### 7b. The database pool is healthy after a client leaves mid-answer

This was **not verifiable offline** (a cancelled `commit()` on `mssql+aioodbc`), so it is checked here:

1. Start the stream again and **stop the client in the middle** (Ctrl-C after a few `token` frames).
2. Immediately repeat step 6 (`GET $WEB/api/v1/conversations`) five times in a row, and ask a fresh question.

Expect every call to answer `200` quickly and the fresh question to stream normally. If calls hang or fail with a
connection or pool error (an `unhandled exception` line in `AppTraces`, or a `503`/timeout from the BFF), the cancelled commit left a
connection unusable: record the `trace_id`, the time and the kind, restart the api revision to recover, and report it
(this is a code fix, not a runbook step). Also check that no assistant message was stored for the aborted answer.

## 8. One `trace_id` across the logs and Application Insights

Take the `x-trace-id` header of the streamed ask in step 7 (32 hex characters; the web's starts with `0eb0`, the
api's ids with `0c70`; the web's id is the one adopted by the api). Wait 2–5 minutes for ingestion, then run query 1
of [`../kql-starter-pack.md`](../kql-starter-pack.md) with it:

```kusto
let tid = "<the 32-hex x-trace-id>";
union AppRequests, AppDependencies, AppTraces, AppExceptions, AppEvents
| where tostring(Properties.trace_id) == tid or OperationId in (
    (union AppRequests, AppTraces
     | where tostring(Properties.trace_id) == tid
     | distinct OperationId))
| project TimeGenerated, Type, AppRoleName, OperationId, Properties
| order by TimeGenerated asc
```

Expect:

- **one** `OperationId`, with rows from both `team-assistant-web` and `team-assistant-api`;
- an `AppDependencies` row for the web → api hop, and `mssql` dependencies under the api request (the conversation
  and message statements);
- the api's lines for the ask (for example `ask failed` only on a failure) carry the same `trace_id`, and **no line
  anywhere holds the question, the answer or a retrieved passage** (the contract: kinds, counts and durations only).
  If you can read the question in any row, stop and treat it as a leak: [pii-purge](pii-purge.md);
- on the **web**, the request path in the span shows `{id}`, not the conversation's UUID.

A missing half of the chain: [post-deploy-smoke](post-deploy-smoke.md) (the telemetry proofs) and
[observability-degraded](observability-degraded.md).

## Record the result

Note, on the roadmap item or the PR: the date, the environment, the api and web versions (`/info`), the
`trace_id` of step 8, and for each step **pass** or the signature you saw. A step you could not run (for example
no access to the index) is **not run**, not "passed".

## References

- Roadmap: api 5.1 · [ADR-0013](../../adr/0013-corpus-baked-into-api-image.md) (corpus in the image) ·
  [ADR-0014](../../adr/0014-shared-conversations-until-auth.md) · [ADR-0015](../../adr/0015-admin-key-for-ingest.md)
- [rotate-ingest-admin-key](rotate-ingest-admin-key.md) · [purge-conversations](purge-conversations.md) ·
  [post-deploy-smoke](post-deploy-smoke.md) · [`docs/reference/http-api.md`](../../reference/http-api.md)
