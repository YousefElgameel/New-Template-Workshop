# HTTP API reference

The complete HTTP surface of both services as they exist today: the operational foundation
(`/ping`, `/info`, `/health`), the first business endpoints (`/v1/conversations`), and the two demo
BFF routes in `apps/web` that exercise the `web → api` hop, plus the BFF mirror of the conversation
endpoints (`/api/v1/conversations`, including `/api/v1/conversations/[id]/ask[/stream]`). The template's single-turn
`/api/v1/assistant/ask[/stream]` demo routes were removed in web 0.7.0.
Derived from the route
source, cross-checked against the test suites (`routes.test.ts`, `test_routes.py`).

`apps/api` (FastAPI) is the **only backend**; it took the `api` name in
[ADR-0005](../adr/0005-name-services-by-role.md). The former **NestJS** `apps/api` service — a
different service (origin `0a71`, retired) — and with it `/ping/chain`, `/info/chain`, and the
BFF routes `/api/ping-python`, `/api/info-python`, `/api/ping-chain`, `/api/info-chain` — was
removed in [ADR-0003](../adr/0003-remove-nestjs-api-layer.md).

## Route map

| Method | Path                                    | Service          | Purpose                                             | Versioned?       |
| ------ | --------------------------------------- | ---------------- | --------------------------------------------------- | ---------------- |
| GET    | `/ping`                                 | `apps/api`       | Liveness greeting                                   | No (operational) |
| GET    | `/info`                                 | `apps/api`       | Status/version/runtime report                       | No (operational) |
| GET    | `/health`                               | `apps/api`       | Health + observability state                        | No (operational) |
| POST   | `/v1/conversations`                     | `apps/api`       | Start a conversation                                | Yes (`/v1`)      |
| GET    | `/v1/conversations`                     | `apps/api`       | List conversations, newest first                    | Yes (`/v1`)      |
| GET    | `/v1/conversations/{id}`                | `apps/api`       | Open a conversation with its messages               | Yes (`/v1`)      |
| POST   | `/v1/conversations/{id}/ask`            | `apps/api`       | Ask a question; the answer cites the docs           | Yes (`/v1`)      |
| POST   | `/v1/conversations/{id}/ask/stream`     | `apps/api`       | Ask a question; the answer streams in (SSE)         | Yes (`/v1`)      |
| GET    | `/api/v1/conversations`                 | `apps/web` (BFF) | Proxies `GET /v1/conversations`                     | Yes (`/api/v1`)  |
| POST   | `/api/v1/conversations`                 | `apps/web` (BFF) | Proxies `POST /v1/conversations` (body ≤ 4 KB)      | Yes (`/api/v1`)  |
| GET    | `/api/v1/conversations/[id]`            | `apps/web` (BFF) | Proxies `GET /v1/conversations/{id}`                | Yes (`/api/v1`)  |
| POST   | `/api/v1/conversations/[id]/ask`        | `apps/web` (BFF) | Proxies `POST /v1/conversations/{id}/ask`           | Yes (`/api/v1`)  |
| POST   | `/api/v1/conversations/[id]/ask/stream` | `apps/web` (BFF) | Proxies `…/ask/stream` (SSE, unchanged)             | Yes (`/api/v1`)  |
| GET    | `/api/ping-backend`                     | `apps/web` (BFF) | Proxies `apps/api` `/ping`                          | No (demo route)  |
| GET    | `/api/info-backend`                     | `apps/web` (BFF) | Proxies `apps/api` `/info`                          | No (demo route)  |
| GET    | `/health`                               | `apps/web` (BFF) | Own health + observability state (no upstream call) | No (operational) |

Business endpoints are `/v1/<feature>` on the api (routers attached to `v1_router`) and
`/api/v1/<feature>` on web (folder-enforced). See [Versioning](#versioning).

---

## `apps/api` — FastAPI (port 8000, origin `0c70`)

Source: `apps/api/app/routes.py` (operational router, mounted at root by `app/main.py`).
`app/routers/v1.py` defines `v1_router` (`prefix="/v1"`); `app/routers/conversations.py` and
`app/routers/ask.py` are attached to it (`/v1/conversations…`).

### `GET /ping`

```json
{
  "service": "api",
  "message": "hello from the Team Assistant api service",
  "trace_id": "0c70…"
}
```

- `service` — fixed string `"api"`.
- `message` — fixed greeting string.
- `trace_id` — the request-scoped trace id (adopted from a valid inbound `x-trace-id`, or
  generated with origin `0c70`). When called through the BFF it is the **web-minted** `0eb0…`
  id, because the api validates only the 32-hex **format**, not the origin prefix
  (`apps/api/app/tracing.py`).

No upstream call — the api is the end of the hop.

### `GET /info`

```json
{
  "status": "ok",
  "service": "api",
  "version": "0.0.0",
  "env": "local",
  "uptime_seconds": 12.345,
  "runtime": { "name": "python", "version": "3.14.0" },
  "system": {
    "platform": "Linux",
    "arch": "x86_64",
    "pid": 1,
    "hostname": "api-abc123"
  },
  "observability": "disabled",
  "reason": "no Azure connection string",
  "trace_id": "0c70…"
}
```

Field notes (`apps/api/app/routes.py`):

- `version` — `importlib.metadata.version("team-assistant-api")` (the dist name from
  `pyproject.toml`); falls back to `"unknown"` if the package metadata isn't installed.
- `env` — `APP_ENV` lowercased, default `"local"`.
- `uptime_seconds` — `time.monotonic() - _STARTED` (captured at module import), rounded to 3
  decimals.
- `runtime` — `{ "name": "python", "version": platform.python_version() }` — the **interpreter** name, not the service (which is `"api"`); unchanged by the rename (ADR-0005).
- `system` — `{ platform: platform.system(), arch: platform.machine(), pid: os.getpid(), hostname: socket.gethostname() }`
  (`platform.system()` returns `"Linux"`/`"Windows"`/`"Darwin"`, capitalized).
- `observability` / `reason` — see the reason-string table in
  [Observability state reasons](#observability-state-reasons) below.

### `GET /health`

```json
{
  "status": "ok",
  "service": "api",
  "trace_id": "0c70…",
  "observability": "disabled",
  "reason": "no Azure connection string"
}
```

`status` is unconditionally `"ok"` — this endpoint reports process liveness + observability
state, not database health. **The database is never queried here**: the SQLAlchemy engine is
lazy and the baseline issues no queries, so `/health` is green with the placeholder
`DATABASE_URL`.

### `/v1/conversations` — start, list and open a conversation

Source: `apps/api/app/routers/conversations.py` (roadmap api 2.1). Conversations are **shared** — no
owner, any user of the internal deployment can read any of them ([ADR-0014](../adr/0014-shared-conversations-until-auth.md)).
Every response echoes `x-trace-id`; every non-2xx body is `{ "error", "trace_id" }`. Timestamps are ISO-8601 UTC
with a `Z`.

**`POST /v1/conversations`** — body optional: `{ "title"?: string }` (≤ 200 characters).

```json
{
  "id": "5b1f…",
  "title": "New conversation",
  "created_at": "2026-10-06T10:00:00.123Z",
  "updated_at": "2026-10-06T10:00:00.123Z"
}
```

`201`. A blank or missing title becomes `"New conversation"`, replaced by the first question once one is
asked. `422 validation_error` for a title longer than the column (200 UTF-16 code units: an emoji counts as 2, so
100 fit), a non-string title, an unknown field or a malformed JSON body. **Cap the request body at the ingress or
the BFF** (a few KB is plenty here): the api itself reads the whole body before validating it.

**`GET /v1/conversations?limit=50`** — newest first by `updated_at`; `limit` is 1–100 (default 50).

```json
{
  "items": [{ "id": "5b1f…", "title": "Trace ids", "updated_at": "2026-10-06T10:12:00.000Z" }],
  "count": 1
}
```

`count` is the number of items returned, not a total. `422 validation_error` for a `limit` outside 1–100 or
not an integer. There is no cursor yet (decision 15): page by lowering the limit.

**`GET /v1/conversations/{id}`** — the conversation and its messages, **oldest first**.

```json
{
  "id": "5b1f…",
  "title": "Trace ids",
  "created_at": "…",
  "updated_at": "…",
  "truncated": false,
  "messages": [
    { "id": "…", "role": "user", "content": "…", "citations": null, "created_at": "…" },
    {
      "id": "…",
      "role": "assistant",
      "content": "…",
      "citations": [{ "title": "…", "path": "docs/a.md" }],
      "created_at": "…"
    }
  ]
}
```

A read never returns more than **500** messages: when a conversation holds more, only the most recent 500 are
returned (still oldest first) and `truncated` is `true`. `404 not_found` for an unknown id; `422` for an id that
is not a UUID.

### `POST /v1/conversations/{id}/ask` — ask a question

Source: `apps/api/app/routers/ask.py` (roadmap api 4.2). The answer is written from the team's docs
(retrieval, then the model — `app/ai/graph.py`) with the conversation's last 10 messages as history.

```json
// request
{ "question": "Which port does the api listen on?" }

// 200
{
  "message_id": "9c1e…",
  "answer": "The api listens on port 8000 [1].",
  "citations": [{ "title": "HTTP API reference", "path": "docs/reference/http-api.md" }]
}
```

- **`question`** — trimmed; not blank; at most **4,000 characters** (characters, not bytes); no NUL character and
  no invalid text (a lone surrogate from a JSON `\ud800` escape). A blank, missing or wrong-typed field is
  `422 validation_error`; over the limit is `413 payload_too_large`. The body is read before it is checked, so
  the BFF caps the request size too.
- **`citations`** — the retrieved documents whose `[n]` the answer cites, each once, in citation order; `[]` when
  it cites none (for example "I do not know"). They are built from what retrieval returned, never from text the
  model wrote, and the same list is stored on the assistant message (so `GET /v1/conversations/{id}` returns it).
  `title` and `path` are one bounded line each but **untrusted**: show them as plain text, never as markup or a
  link target. The answer text is returned as the model wrote it (cut at 4,000 characters, ending in `…`): do not
  render raw HTML or load images from it.
- **What is stored, and when** — the question is stored and committed **first**, so it is kept whatever happens
  next; the assistant message (with its citations and the output token count) is stored only after an answer
  exists. The conversation's `updated_at` moves with each message, and a conversation still titled "New
  conversation" takes the question (≤ 200 characters) as its title.
- **History** — the 10 most recent earlier messages, oldest first, then the retrieved context, then the question.
  Only `user` and `assistant` rows are replayed; a row of any other role is dropped, never sent as a system message.

- **Text cleaning** — in the question and in the stored answer, NUL and lone surrogates are dropped and every
  other control character except tab, newline and carriage return becomes a space (a control character costs 6
  bytes as a JSON escape). So no stored character costs more than 4 bytes in a thread read, and ordinary
  500-message threads fit the BFF's 8 MiB. A thread of maximum-length emoji-only messages with very long
  citation titles can still exceed it; bounding a thread read by bytes is api roadmap 5.4.
- **Replayed history** is cut to 4,000 characters per message.

| Situation                                                                        | Status | `error`             |
| -------------------------------------------------------------------------------- | ------ | ------------------- |
| blank / missing / wrong-typed `question`, NUL, invalid text, `{id}` not a UUID   | 422    | `validation_error`  |
| `question` longer than 4,000 characters                                          | 413    | `payload_too_large` |
| no such conversation                                                             | 404    | `not_found`         |
| `AI_MAX_CONCURRENT_ASKS` asks are already running in this process (default 4)    | 429    | `busy`              |
| model or search failed or timed out, the AI layer is not configured, blank reply | 503    | `ai_unavailable`    |

On `429` nothing was stored. On `503` the **question was kept** and no answer was stored; ask again to retry.
No question, answer, passage, title or id is written to a log, a span, a metric or an event: the logs carry
counts, a boolean for "answer was cut", and a bounded error kind.

**Timing.** A normal answer takes a few seconds. The bounds: the model call has a 60 s timeout per attempt and 2
retries (`AI_REQUEST_TIMEOUT_SECONDS`, `AI_MAX_RETRIES`), the query embedding and the search each have 30 s
(`AI_RETRIEVAL_TIMEOUT_SECONDS`), the answer is at most `AI_MAX_OUTPUT_TOKENS` (1,024) tokens. The worst case is
therefore about **4 minutes**; whatever calls this route must wait longer than that (the BFF's default 10 s
`fetchUpstream` timeout is far too short — web roadmap 3.1).

### `POST /v1/conversations/{id}/ask/stream` — ask a question, streamed

Source: `apps/api/app/routers/ask.py` (roadmap api 4.3). The same request, history, storage rules and
limits as `POST …/ask` above; the answer arrives as server-sent events (`text/event-stream`,
`Cache-Control: no-cache`, `X-Accel-Buffering: no`). Request body: `{ "question": "…" }`.

Each frame is one `event:` line, one JSON `data:` line and a blank line.

| Event     | `data`                                                                  | When                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --------- | ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sources` | `{ "sources": [{ "title", "path" }], "count": 3 }`                      | Once, as soon as retrieval ends and before any token. `sources` lists **every retrieved document once** (not only the cited ones); `count` is the number of passages retrieved.                                                                                                                                                                                                                                                     |
| `token`   | `{ "text": "partial " }`                                                | Zero or more times. Concatenated, the texts are the answer **exactly as it is stored** (control characters cleaned).                                                                                                                                                                                                                                                                                                                |
| `done`    | `{ "message_id", "input_tokens", "output_tokens", "citations": [...] }` | The last frame on success. `citations` are the documents the answer cites, as stored (`[]` when none). The token counts are `null` when the model did not report them, or when the answer was cut (the model stops being read).                                                                                                                                                                                                     |
| `error`   | `{ "error_kind", "error", "trace_id" }`                                 | The last frame on failure: no `done` follows and no answer is stored. `error` is `ai_unavailable` (model, search, timeout, unconfigured, blank reply) or `internal_error` (the answer could not be stored); `error_kind` is a bounded word (`timeout`, `rate_limited`, `network`, `error`, `empty_answer`, `persist_failed`; an authentication failure is reported as `error`). `trace_id` equals the `x-trace-id` response header. |

```
event: sources
data: {"sources": [{"title": "HTTP API reference", "path": "docs/reference/http-api.md"}], "count": 1}

event: token
data: {"text": "The api listens "}

event: token
data: {"text": "on port 8000 [1]."}

event: done
data: {"message_id": "9c1e…", "input_tokens": 812, "output_tokens": 14, "citations": [{"title": "HTTP API reference", "path": "docs/reference/http-api.md"}]}
```

- **Before the first byte** the route answers with ordinary JSON errors, exactly as `POST …/ask`: `422` (blank or
  invalid question), `413`, `404`, `429 busy`. Nothing is stored for those. From the first byte on the status is
  already `200` and a failure is an `error` frame.
- **What is stored, and when** — the question is stored and committed before the stream starts, so it survives any
  failure. The assistant message is stored once the whole answer exists and **before** `done` is sent (its id is
  `done.message_id`); an `error` frame, or a client that leaves before then, stores no answer. If the client leaves
  exactly during that final write, the answer may still be stored.
- **The concurrency slot** (`AI_MAX_CONCURRENT_ASKS`) is held until the stream ends: `done`, `error` or the client
  leaving.
- **Time bounds** — the whole generation has one deadline: the model's timeout × (retries + 1) plus the two
  retrieval steps, about 4 minutes at the defaults. A provider that keeps trickling tokens past it ends in an
  `error` frame with kind `timeout`. A client that accepts no bytes for 30 seconds is dropped: the connection
  ends and the slot is freed.
- **Framing** — each frame is two physical lines. The JSON escapes every control character, and also U+2028,
  U+2029, U+0085 and U+007F, which a parser that splits lines on more than CR, LF and CRLF would otherwise take
  for line breaks inside a model-written token. A client should still split lines only on CR, LF and CRLF.
- **`sources` is wider than `citations`** — every retrieved document's title and path is sent early, cited or
  not (accepted while the deployment is internal only, ADR-0014). Both lists are untrusted text.
- **The cap** — an answer that would reach 4,000 characters is cut at 3,999 plus `…`, and the model is no longer
  read, so what the client saw is exactly what is stored. (The JSON route cuts only above 4,000.)
- **For the BFF and the page** — forward the stream unchanged (`streamUpstream` / `proxyStream`, not
  `fetchUpstream`); the hop timeout must exceed the worst case of about 4 minutes. Show `sources` as soon as it
  arrives, and replace it with `done.citations` when `done` arrives. Titles and paths are untrusted text: show them
  as plain text.
- No question, answer, passage, title or id is written to a log, a span or a metric. A database error while storing
  the answer is caught and reported as `internal_error` / `persist_failed`; it is never raised after the response
  has started, because the server span would record its text.

---

## `apps/web` — Next.js BFF demo routes (port 3000, origin `0eb0`)

Source: `apps/web/src/app/api/{ping,info}-backend/route.ts`. Every handler is wrapped in
`withBff` (`src/lib/trace.ts`), which resolves/adopts `x-trace-id`, runs the handler inside the
trace-id async-local scope, and **unconditionally echoes `x-trace-id`** on whatever `Response`
the handler returns (success or error). Upstream calls use `fetchUpstream` (never bare `fetch`).

| Route                   | Upstream call             | Base URL env var (default)               |
| ----------------------- | ------------------------- | ---------------------------------------- |
| `GET /api/ping-backend` | `GET {API_BASE_URL}/ping` | `API_BASE_URL` (`http://localhost:8000`) |
| `GET /api/info-backend` | `GET {API_BASE_URL}/info` | `API_BASE_URL` (`http://localhost:8000`) |

In `docker-compose.yml` the base URL is overridden to `http://api:8000` (compose DNS).

### Success response shape

On success, each route returns the **upstream JSON body verbatim** (`Response.json(data, {
status: res.status })`) — the BFF does not add its own envelope. So:

- `/api/ping-backend` → the api's `/ping` shape (see above).
- `/api/info-backend` → the api's `/info` shape.
- The upstream's own `trace_id` field matches the response's `x-trace-id` header, because the
  BFF forwards its resolved trace id to the api, which adopts it (valid 32-hex format,
  origin-agnostic). This single hop is the `x-trace-id` propagation the contract exists to
  demonstrate: one `0eb0…` id on the browser response, on the api's body, and on every log line
  and span in between.
- HTTP status is **mirrored from upstream** — e.g. an upstream 404/500 with a valid JSON body is
  passed through with that same status, not converted to a BFF error.

### `GET /health` (web's own, no upstream call)

```json
{
  "status": "ok",
  "service": "web",
  "trace_id": "0eb0…",
  "observability": "disabled",
  "reason": "no Azure connection string"
}
```

Unlike the two demo routes, this one never calls upstream — it reports `apps/web`'s own process
liveness + observability state (`apps/web/src/app/health/route.ts`).

---

## api error contract (`apps/api`, `app/errors.py`)

Every non-2xx response from the api — operational routes and `/v1` alike — has exactly this body,
and the same trace id in the `x-trace-id` header:

```json
{ "error": "not_found", "trace_id": "0c70…" }
```

| Situation                                      | Status  | `error`                                                                                   |
| ---------------------------------------------- | ------- | ----------------------------------------------------------------------------------------- |
| unknown route                                  | 404     | `not_found`                                                                               |
| wrong method                                   | 405     | `method_not_allowed` (+ `Allow` header)                                                   |
| request body/query fails validation            | 422     | `validation_error` — fields logged, not echoed                                            |
| `raise NotFound()` / `Conflict("<code>")`      | 404/409 | the class code or the one given                                                           |
| `raise HTTPException(403, detail="read_only")` | 403     | `read_only` (a snake_case `detail` is the code; prose maps to the default for the status) |
| a request body over a route's size limit       | 413     | `payload_too_large`                                                                       |
| too many AI calls at once (`ask`)              | 429     | `busy`                                                                                    |
| model/search not configured or unavailable     | 503     | `ai_unavailable`                                                                          |
| anything else                                  | 500     | `internal_error` — kind + location logged only                                            |

Codes are stable identifiers the BFF and UI can switch on; messages, validation values and stack
traces never leave the api (rule 50). Streaming routes emit `event: error` with a bounded
`error_kind` once the stream has started (rule 70).

## `apps/web` — conversation BFF routes (`/api/v1/conversations…`)

Source: `apps/web/src/app/api/v1/conversations/route.ts` and `[id]/route.ts`, helpers in
`src/lib/conversations-bff.ts` (web roadmap 1.2). They mirror the api endpoints above: the api's status and JSON
body come back unchanged, so a `404 not_found` or `422 validation_error` from the api reaches the browser with its
own `{ error, trace_id }`. Every response carries `x-trace-id`; the BFF validates what it forwards, and none of it
is logged (routes log their pattern and the status only — a rejected request logs a bounded `bff rejected request`).

| Route                              | Upstream                     | The BFF answers itself when                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| ---------------------------------- | ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/v1/conversations?limit=` | `GET /v1/conversations`      | `limit` is present and not an integer 1–100 → `400 invalid_request`                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| `POST /api/v1/conversations`       | `POST /v1/conversations`     | the body is over **4 KB** → `413 payload_too_large`; it cannot be read (the upload breaks off, or stalls for more than 5 s) → `400`; a body is sent without `Content-Type: application/json` → `415 unsupported_media_type`; it is not JSON, not an object, has a key other than `title`, or the title is not a string of ≤ 200 characters (UTF-16 units: 100 emoji) → `400 invalid_request`. An empty body, `null`, `{}` and `{"title":null}` mean "no title" (no content type needed when there is no body). Only `title` is forwarded. |
| `GET /api/v1/conversations/[id]`   | `GET /v1/conversations/{id}` | `[id]` is not a UUID → `400 invalid_request` (the id goes into the upstream URL, so it is checked first)                                                                                                                                                                                                                                                                                                                                                                                                                                  |

- **Two codes for the same mistake.** A title that is too long, or an unknown field, is `400 invalid_request` from the
  BFF. The api would answer `422 validation_error` — it only gets the chance if the two checks drift apart. A UI
  needs to handle `400` for these; a `422` is a sign of a bug.
- The size cap is enforced while reading the body (a chunked upload declares no length), because the api reads a
  whole body before validating it. A declared `Content-Length` over the cap is refused without reading anything.
- An api that is down, a redirect, an api reply that is not JSON, one over 8 MiB, or an **error status whose body is not
  the api's `{ error, trace_id }` shape** (a framework default such as `{"detail": …}`) is a `502`:
  `{ "error": "upstream_unreachable" | "upstream_error", "target": "api", "trace_id" }`.
- With `MOCK_UPSTREAM=true` the routes serve `src/mocks/conversations.ts` (marked `x-mock-upstream: true`), behind
  the same validation; `limit` is honoured, an unknown id is a marked `404`. Mock mode is refused when `APP_ENV` is
  `prod` (case and surrounding whitespace ignored).

### The ask routes (`/api/v1/conversations/[id]/ask` and `…/ask/stream`)

Source: `apps/web/src/app/api/v1/conversations/[id]/ask/route.ts` and `ask/stream/route.ts`, helpers in
`src/lib/ask-bff.ts` (web roadmap 3.1). Body `{ "question": string }`. The BFF answers itself, with no upstream call,
in this order:

| When                                                                                         | Status                          |
| -------------------------------------------------------------------------------------------- | ------------------------------- |
| `[id]` is not a UUID (it goes into the upstream URL)                                         | `400 invalid_request`           |
| the body is not declared `application/json` (a cross-site "simple" request has no preflight) | `415 unsupported_media_type`    |
| the body is over 64 KiB (counted while reading), or cannot be read within 5 s                | `413 payload_too_large` / `400` |
| not JSON, not an object, a key other than `question`, blank, NUL, a lone surrogate           | `400 invalid_request`           |
| the question is over 4,000 characters (code points, like the api)                            | `413 payload_too_large`         |

- The api's `404 not_found`, `413`, `422`, `429 busy` and `503 ai_unavailable` are **not rewritten**: status and
  `{ error, trace_id }` reach the browser (no other field of the body does). For the stream route that holds for errors the api reports **before the first byte**
  (JSON); from the first byte on, the status is `200` and a failure is an `error` frame.
- An api that cannot be reached, a reply that is not JSON, or an error body without the api's shape is
  `502 { error: "upstream_unreachable" | "upstream_error", target: "api", trace_id }`. A `200` that is not
  `text/event-stream` is a `502` too.
- **Timeout.** The BFF → api hop timeout is 5 minutes (`ASK_HOP_TIMEOUT_MS`): above the api's worst case of about 4
  minutes, so the 10 s `fetchUpstream` default does not cut an answer short. It also ends when the browser leaves, which
  frees the api's concurrency slot. The ingress in front of the web app has its own request timeout; this repo does not
  set it.
- **A stream that breaks.** The frames are forwarded unchanged. If the api connection fails mid-answer, or more than
  1 MiB would be forwarded, the BFF ends the stream with one frame
  `event: error` / `{ "error_kind": "network" | "too_long", "error": "upstream_error", "trace_id" }`. A browser that
  leaves cancels the api stream, which frees the api's `AI_MAX_CONCURRENT_ASKS` slot.
- Logs carry the route pattern and a status; never the id, the question, a token or the URL. `/v1/admin/ingest` is not
  exposed. With `MOCK_UPSTREAM=true` both routes serve `src/mocks/conversations.ts`; `?mock=failure` (mock mode only)
  serves the stream that fails after the answer started.

## Error behavior (BFF)

> This section describes the two **demo** routes. The conversation routes above answer a reply that is not JSON with
> `upstream_error`, not `upstream_unreachable`.

Each of the two demo routes wraps its upstream `fetch` **and** its `res.json()` parse in a single
`try/catch`. Only failures in that block — a network error (e.g. `ECONNREFUSED`, timeout) or a
non-JSON/unparseable upstream body — produce the BFF's own synthesized error response:

```json
{
  "error": "upstream_unreachable",
  "target": "api",
  "trace_id": "0eb0…"
}
```

- HTTP status: **`502`**.
- `target` — always `"api"` today (the bounded target label; `resolveTarget()` maps any
  other host to `"other"`).
- `trace_id` — the BFF's resolved trace id (same value echoed in the `x-trace-id` header).
- The literal error code `"upstream_unreachable"` is used even when the actual cause is a JSON
  parse failure rather than a connectivity failure — the catch block doesn't distinguish the two
  (`apps/web/src/app/api/*/route.ts`).
- An upstream **non-2xx status with a valid JSON body** (e.g. upstream 500) is **not** converted
  to a 502 — it's mirrored through as described above. The 502 path is reserved for cases where no
  upstream `Response` was successfully obtained/parsed at all.

## Headers

| Header                     | Direction                 | Behavior                                                                                                                                                                         |
| -------------------------- | ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `x-trace-id`               | Inbound                   | Adopted if it matches `^[0-9a-f]{32}$` (any origin prefix accepted); otherwise a fresh id is generated with the receiving service's origin (`web=0eb0`, `api=0c70`).             |
| `x-trace-id`               | Outbound (every response) | Always echoed — success, upstream passthrough, and the BFF's 502 all carry it. Set unconditionally by `withBff` (web) and `TraceMiddleware` (api) regardless of handler outcome. |
| `x-trace-id`               | Outbound (upstream calls) | Forwarded unchanged on every proxied call — `fetchUpstream` (web); `traced_client()` (api, unused in the baseline — the api has no downstream calls today).                      |
| `accept: application/json` | Outbound (web only)       | Set by `fetchUpstream` on every upstream call (`apps/web/src/lib/trace.ts`).                                                                                                     |

## Observability state reasons

The exact `reason` string on `/health` and `/info` depends on the service and its state — useful
when comparing responses across services (full detail:
[`environment-variables.md`](environment-variables.md#per-variable-notes)):

| State                          | `apps/web` reason                                                                            | `apps/api` reason                       |
| ------------------------------ | -------------------------------------------------------------------------------------------- | --------------------------------------- |
| No connection string (default) | `no Azure connection string`                                                                 | `no Azure connection string`            |
| Enabled successfully           | `ok`                                                                                         | `azure monitor configured`              |
| Invalid `TELEMETRY_AUTH_MODE`  | `invalid TELEMETRY_AUTH_MODE "<value>" — expected "connection_string" or "managed_identity"` | same wording                            |
| Init/config threw              | `<raw error message>` (or `unknown initialization error`)                                    | `configure_azure_monitor failed: <exc>` |

In all cases `observability` is `"enabled"`/`"disabled"` (never any other value), and a
disabled/degraded state never crashes the service — see `.claude/rules/60-observability.md`.

## Versioning

- **Every business HTTP endpoint MUST be URI-versioned (`/v{n}`, starting at `v1`)** — decided in
  [`../adr/0001-api-versioning.md`](../adr/0001-api-versioning.md) and enforced per
  [`../../.claude/rules/05-api-versioning.md`](../../.claude/rules/05-api-versioning.md).
- **The only exemption**: the operational endpoints `/ping`, `/info`, `/health` (both services)
  stay unversioned — container/Azure health probes target fixed paths.
- **This reference documents the foundation surface only** — there are no business endpoints
  yet. `apps/api`'s `v1_router` is mounted but empty; `apps/web` has no `src/app/api/v1/`
  folder yet — the first business BFF route creates it.
