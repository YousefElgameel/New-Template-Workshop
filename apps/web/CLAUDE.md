# apps/web — CLAUDE.md

Next.js 16 (App Router) **BFF + UI** for the **Team Assistant** (the brandless starter
template Orion Digital Solutions built for the Diriyah Company AI team). Port **3000**, trace
origin **`0eb0`**, OTEL service `team-assistant-web`. Project-wide rules and the shared
`trace_id` / observability contract live in the root [CLAUDE.md](../../CLAUDE.md) and
[README.md](../../README.md) — this file is the web-specific context. Treat it as a starting
map; read the actual files when you need detail.

## The BFF rule (non-negotiable)

The browser **only** ever talks to this Next server. The two buttons call **same-origin**
route handlers under `src/app/api/*`; those handlers (the BFF) call the api (FastAPI)
backend **server-side** using `API_BASE_URL` — the sole backend since ADR-0003. **Never**
expose service URLs or secrets to client code, and never use `NEXT_PUBLIC_*` for a service base
URL.

## Layout

- `src/app/page.tsx` — client UI: two button rows over the one path (web → api) — **Ping**
  (`/api/ping-backend`) and **Info** (`/api/info-backend`); renders each response + its
  `trace_id`.
- `src/app/api/ping-backend/route.ts`, `src/app/api/info-backend/route.ts` — BFF route
  handlers (server-side) targeting the api's `/ping` and `/info`. Wrap the handler in
  `withBff`, call upstream with `fetchUpstream`; on upstream failure return `502` with a
  `trace_id` (and `target: 'api'`) so the UI degrades gracefully.
- `src/app/health/route.ts` — `/health`.
- `src/app/api/v1/conversations/[id]/ask/route.ts` and `ask/stream/route.ts` (`POST`) — the BFF mirror of the api's
  ask routes; the stream is forwarded **unchanged** (`streamUpstream` + `proxyStream`). `src/lib/ask-bff.ts` holds what
  they share: `readAskRequest` (UUID → `400`; `application/json` → `415`; ≤ 64 KiB read within 5 s → `413`/`400`; only
  `question`, trimmed, not blank, no NUL/lone surrogate → `400`; ≤ 4,000 code points → `413`), `ASK_HOP_TIMEOUT_MS`
  (5 min — above the api's ~4 min worst case; never the 10 s default) and `guardStream` (an api connection that breaks,
  or > 1 MiB, ends the stream with one terminal `error` frame carrying the trace id; the browser leaving cancels the
  api stream). The api's `404`/`413`/`422`/`429`/`503` pass through via `relayJson`; mock mode serves the 1.1
  fixtures (`?mock=failure` for the failing stream). Logs: route pattern + status, never the id or the question.
- `src/app/api/v1/conversations/route.ts` (`GET` list, `POST` create) + `[id]/route.ts` (`GET` open) — the BFF
  mirror of `/v1/conversations`. `src/lib/conversations-bff.ts` holds what they share: the UUID check for `[id]`
  (it goes into the upstream URL), `limit` 1–100, the create-body validation (only `title`, ≤ 200 UTF-16 units,
  `application/json` when a body is sent → else `415`), a **4 KB body cap enforced while streaming** with a 5 s read
  timeout (`413`/`400`; the api reads a whole body before validating it), and `callApi` (api status + JSON pass
  through; `502` for an unreachable api, a redirect, a non-JSON or over-8-MiB reply, or an error body that is not
  the api's `{ error, trace_id }`). Routes log their PATTERN, never the URL (it holds an id); in Next 16 `params` is
  a promise. Both modes are tested in `routes.test.ts`. The web's own request and outbound spans never carry the
  concrete id either: `src/lib/telemetry-scrub.ts` rewrites every UUID in `http.target`, `http.url`, `url.path` and
  `url.full` — and Next's `next.span_name` and its fetch span's name, which carry the upstream URL — to `{id}` (`IdScrubSpanProcessor` passed to the Azure distro, plus the undici instrumentation's
  `startSpanHook`); fail-safe, tested with an in-memory exporter. Any new id-bearing route is covered if its ids are UUIDs.
- `src/app/api/v1/assistant/ask/route.ts` + `ask/stream/route.ts` — **example business routes**
  (JSON + SSE) that proxy `${API_BASE_URL}/v1/assistant/…` or, with `MOCK_UPSTREAM=true`, serve
  `src/mocks/assistant.ts` (the template's single-turn demo; removed in roadmap web 4.1).
  `src/app/chat/page.tsx` — the chat page: a thin shell around `ChatWorkspace`, which talks to the
  conversation routes (`/api/v1/conversations…`) through `src/lib/conversations-client.ts`.
- `src/components/chat/` — `ChatThread`, `MessageInput`, `ChatPanel`, `ConversationList`, `ChatWorkspace` and `Sources`
  (Tailwind, RTL-tested). `Sources` is the block under an assistant message: each cited document's title with its path
  beside it, as **plain text** (they are untrusted indexed text: no markup, no links), not numbered (the `[n]` in an answer
  are passage numbers, not list positions); nothing is drawn when there are none.
  `ChatWorkspace` is the `/chat` page body: it owns the state and fetches (list after mount, open a
  conversation — latest click wins —, **+ New chat**, a drawer for the list below 768 px) and **sends**: the question joins
  the thread at once, the answer streams in via `askStream` (`/api/v1/conversations/{id}/ask/stream`; no conversation open →
  one is created first), the input is disabled meanwhile, `done` moves the conversation to the top of the list (an untitled
  one takes the question as its title), a failure shows the code + trace id under the question with **Retry**. `stopStream`
  (a `sendSeq` counter + an `AbortController`) ends the answer when another conversation is opened, a new chat is started
  or the page is left; its late frames are ignored. `MessageInput` takes an optional `textareaRef` (focus) and `maxChars`
  (a "n / max" counter in code points; over the limit nothing is sent); `ChatThread` takes `onRetry` and makes only its
  newest message a live region (`aria-busy` while streaming); `ConversationList` takes `showEmpty`.
  `ConversationList` is the chat page's sidebar: a **+ New chat** button and one `<button>` row per conversation
  (title + `formatRelativeTime` from `src/lib/relative-time.ts`: "today 10:12" / "yesterday" / "5 Oct"), the open row marked
  `aria-current="true"`, long titles truncated, "No chats yet" when empty. Presentational: `onSelect(id)` / `onNewChat()`
  are callbacks, it never fetches. Times are read in the browser's time zone, which the server does not know, so the rows must not be in
  server-rendered markup: fetch the list after mount so the first render has none. The page also has to bound the column's
  width (for truncation) and height (for the list to scroll).
- `src/lib/stream.ts` (`streamUpstream`/`proxyStream`), `src/lib/sse.ts` (frame parser/builder),
  `src/lib/chat-client.ts` (`askStream`, browser → BFF: the conversation stream's `sources`, `token`\*, `done` / `error`
  frames, every field validated — see below), `src/lib/mocks.ts`
  (`isMockUpstream`/`mockJson`/`mockSse`), `src/lib/api-types.ts` (**generated** — `make openapi`).
- `src/lib/conversation-types.ts` — the conversation contract (`/api/v1/conversations…`): create/list/open
  types **derived from the generated `api-types`**; the ask response and the stream frames (`sources`/`token`/`done`/
  `error`) are **hand-declared** (the api serves them since 4.3; `docs/reference/http-api.md` is the reference). `src/mocks/conversations.ts` — the
  fixtures typed from it: an empty list, three listed conversations, a detail for each (long, short, empty), a created
  conversation, a JSON answer, a streamed answer and a streamed failure (`error` frame); pass the frame arrays to
  `mockSse`. `src/mocks/conversations.test.ts` keeps them honest (ordering, limits, frame order).
- `src/lib/trace.ts` — `withBff` (adopt/generate `x-trace-id`, run inside the `AsyncLocalStorage`
  store, echo the header on the response, record the request-duration metric — pass
  `{ routeClass }` for dynamic segments), `fetchUpstream` (the ONLY sanctioned HTTP client:
  forwards `x-trace-id`, bounds every call with `AbortSignal.timeout(UPSTREAM_TIMEOUT_MS)` (10 s)
  unless the caller passes its own `signal`, records hop-duration + upstream-failure metrics; traceparent +
  dependency span via the undici instrumentation), `getTraceId`, origin `0eb0`.
- `src/lib/metrics.ts` — `getMeter(scope)` (namespaced `team-assistant.<scope>`, no-op when
  degraded), `statusClass`/`resolveTarget` (bounded labels: `api` | `other`), starter instruments:
  request-duration + chain-hop histograms, upstream-failure counter. **Bounded attributes
  only** — rule 60 → _Metrics & events_.
- `src/lib/events.ts` — `trackEvent(name, attrs)` → App Insights `customEvents` (via the
  `microsoft.custom_event.name` log-record marker); local structured line always prints;
  `service.start` wired in `instrumentation.ts`.
- `src/lib/logger.ts` — pino → JSON stdout (`service/origin/env/trace_id/message`);
  **redaction at source** (`DEFAULT_REDACTED_FIELDS`/`buildLogger({redactPaths})`), **OTel
  Logs bridge** teeing every line to Azure when enabled (opt out with
  `buildLogger({telemetryBridge: false})`), **async stdout sink** + exit flush. See rule 60 →
  _Log pipeline_.
- `src/lib/observability.ts` — `initObservability` (fail-safe `useAzureMonitor`, **idempotent**
  via a `globalThis` Symbol.for guard shared across Next's module graphs) +
  `getObservabilityState` (env-derived when not yet initialized — see gotchas). Pins
  **fixed-percentage sampling** (`TRACE_SAMPLING_RATIO`, default 1.0; `OTEL_TRACES_SAMPLER*`
  wins) and defaults the cloud role identity (`service.name`/`service.instance.id`,
  append-only). On success registers the **undici** (fetch dependency spans — safe post-load:
  `diagnostics_channel`) + **runtime-node** (event-loop/GC/heap metrics) instrumentations.
  Ingestion auth via **`TELEMETRY_AUTH_MODE`** (`managed_identity` → Entra ID credential; a
  mistyped value disables observability VISIBLY — rule 60 → _Ingestion auth_).
  See `.claude/rules/60-observability.md`.
- `src/instrumentation.ts` — `register()` → `if (process.env.NEXT_RUNTIME === 'nodejs')` →
  `initObservability()`, then `trackEvent('service.start')`.
- `next.config.ts` — `output: 'standalone'`, `serverExternalPackages`
  (`@azure/monitor-opentelemetry`, the `@opentelemetry/*` api/instrumentation packages, `pino`),
  and `turbopack.root` + `outputFileTracingRoot` pinned to this directory.

## Conventions

- New **business** BFF endpoint → `src/app/api/v1/<feature>/route.ts` (→ `/api/v1/<feature>`),
  wrapped in `withBff`, calling the matching `${API_BASE_URL}/v1/…` upstream — API versioning
  is mandatory (ADR-0001 / rule 05). The demo routes (`/api/ping-backend`, `/api/info-backend`)
  are exempt and stay as-is.
- Any upstream call from a route → `fetchUpstream` (trace propagation + echo), never bare `fetch`;
  SSE upstreams → `streamUpstream` + `proxyStream` (`src/lib/stream.ts`).
- Server-only secrets/URLs; client components only call same-origin Next routes.
- **Working ahead of the api:** `MOCK_UPSTREAM=true` in `.env.local` makes a route serve its
  `src/mocks/<feature>.ts` fixture (`isMockUpstream()` / `mockJson` / `mockSse` in
  `src/lib/mocks.ts`); fixtures are typed against `src/lib/api-types.ts` (generated — `make
openapi`). Example: `/api/v1/assistant/ask[/stream]` + the `/chat` page.
- **UI (ADR-0010/0011):** Tailwind v4 utilities; components in `src/components/<area>/` with a
  `*.test.tsx` beside each (`// @vitest-environment jsdom`, RTL, `afterEach(cleanup)`); the
  browser-side streaming client is `src/lib/chat-client.ts` (`askStream`), SSE parsing in
  `src/lib/sse.ts`. The stream is untrusted input, so `askStream` skips malformed frames, checks every field
  (citations `{title, path}` bounded; token counts non-negative integers or `null`; kinds, codes and trace ids matched
  against patterns) and calls `onError` once with `{ kind, code, traceId }` — including for a request refused before the
  stream (`busy`, `payload_too_large`, …) and for a stream that ends without `done`/`error` (`incomplete`: the api
  stored nothing, so it is not a finished answer). The parser splits lines on CR, LF and CRLF only and refuses an event
  over 64,000 characters, and `askStream` ends an answer over 8,000 characters with `too_long`.

## Commands

```bash
pnpm -C apps/web install
pnpm -C apps/web dev            # next dev :3000  (MOCK_UPSTREAM=true in .env.local to use the /chat page without the api)
pnpm -C apps/web build          # next build (standalone output)
pnpm -C apps/web test           # Vitest (lib + BFF routes in node; components in jsdom); test:cov for coverage
pnpm -C apps/web typecheck      # tsc --noEmit
pnpm -C apps/web api-types      # regenerate src/lib/api-types.ts from docs/reference/openapi.json (or: make openapi)
pnpm -C apps/web test:e2e       # Playwright smoke — MANUAL/local, needs web + api up
```

## Gotchas

- **Dev cache:** do **not** run a local production `pnpm build` and then `next dev` against the
  same `.next` — it mixes prod + dev artifacts and throws
  `components.ComponentMod.handler is not a function`. Delete `.next` to recover. (Docker builds
  are isolated, so `just build` / `just dev` never collide.)
- `instrumentation.ts` lives in **`src/`** (this is a src-dir app). The Azure OTel package is a
  `serverExternalPackage` to avoid bundling conflicts.
- `getObservabilityState` re-derives state from env inside route handlers because Next can put
  `instrumentation.ts` and route bundles in **separate module graphs** (the init flag isn't
  shared), so `/health` still reports the correct reason.
- **pnpm 11:** `pnpm-workspace.yaml` here carries the build-script settings; the `Dockerfile`
  must COPY it into the deps stage.
