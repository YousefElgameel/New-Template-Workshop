# Changelog — `apps/web`

All notable changes to the **web** service (Next.js BFF + UI). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) from the consumer's perspective (root `CLAUDE.md`, rule 15).
The two services version independently.

## [Unreleased]

## [0.6.0] — 2026-10-07

### Added

- **Sending a question from `/chat`** (web roadmap 3.3, `ChatWorkspace`): **Send** posts the question to
  `/api/v1/conversations/{id}/ask/stream`. The question shows in the thread at once; the input is disabled while the
  answer streams; tokens appear as they arrive and the Sources block appears with the `sources` frame, then is replaced by
  the documents the answer cites when `done` arrives (none → no block). The conversation moves to the top of the list on
  `done`, and an untitled one takes the question as its title (flattened, at most 200 characters), as the api does.
  With no conversation open, one is created first.
  - **A failure** (an `error` frame, a refused request, a broken stream, a conversation that could not be created) removes
    the answer placeholder and shows the error code and trace id under the question, with a **Retry** button; the input
    works again. The api keeps a question it was asked, so a retry shows it twice when the conversation is reopened.
  - Opening another conversation, starting a new chat or leaving the page **ends the stream** (the api call is
    cancelled, which frees its concurrency slot) and nothing it sends later is applied.
- **A counter and a limit on the input** (`MessageInput`, prop `maxChars`): "0 / 4,000", counted in characters (code
  points, trimmed) like the api; an empty question or one over the limit cannot be sent, and nothing typed is lost.
- **`ChatThread` takes `onRetry`**: a failed question shows **Retry**.

### Changed

- **Screen readers hear the finished answer, not every token**: only the newest message in `ChatThread` is a live region,
  and it is `aria-busy` while it streams (the whole list used to be one).
- The mock ask routes accept the conversation a new chat creates, so "new chat, then ask" works with
  `MOCK_UPSTREAM=true`.
- `MessageInput`'s `keepText` prop is gone (it only served the placeholder that said sending was not available).

### Known limits

- The `ChatPanel` (template demo) is unchanged and has no Retry or counter; web 4.1 removes it.
- The list's order and title after an answer are updated locally; they are re-read from the api the next time the page
  loads.

## [0.5.0] — 2026-10-07

### Added

- **The ask routes** (web roadmap 3.1; backward compatible — new endpoints): `POST /api/v1/conversations/[id]/ask`
  (the whole answer, with its citations) and `POST /api/v1/conversations/[id]/ask/stream` (Server-Sent Events:
  `sources`, `token`, `done` / `error`), body `{ "question": string }`. The api's frames reach the browser unchanged.
  - **Checked here, with no upstream call:** `[id]` is a UUID (`400`), the body is `application/json` (`415`), at most
    64 KiB and read within 5 s (`413` / `400`), only the key `question`, not blank, no NUL or lone surrogate (`400`),
    at most 4,000 characters, counted as code points (`413 payload_too_large`).
  - **Not rewritten:** the api's `404`, `413`, `422`, `429 busy` and `503 ai_unavailable` reach the browser with their
    own `{ error, trace_id }`. An api that cannot be reached, a non-JSON reply or an error body in another shape is a
    `502` with the trace id.
  - **Timeout:** the BFF → api hop timeout is set explicitly to 5 minutes (`ASK_HOP_TIMEOUT_MS`), above the api's ~4
    minute worst case, instead of `fetchUpstream`'s 10 s default. A browser that leaves aborts the api call too.
  - An api error is relayed as `{ error, trace_id }` only, whatever else its body holds.
  - **A stream that breaks** (the api connection fails mid-answer, or more than 1 MiB is forwarded) ends with one
    terminal `error` frame (`error_kind` `network` / `too_long`, the request's `trace_id`) instead of a silent cut. A
    browser that leaves cancels the api stream, which frees the api's concurrency slot.
  - Logs carry the route pattern and a status only — never the id, the question or a token. `/v1/admin/ingest` is not
    exposed. With `MOCK_UPSTREAM=true` both routes serve the 1.1 fixtures; `?mock=failure` (mock mode only) serves the
    stream that fails after the answer started.

### Changed

- `src/lib/conversations-bff.ts` exports `relayJson`, `upstreamUnreachable` and `apiUrl` (what `callApi` did, split so
  the streaming route can relay an api error that arrives before the stream). `callApi` behaves as before.

### Known limits

- Nothing sends a question from the page yet (web 3.3). The ingress request timeout in front of the web app is not set by this repo (see the roadmap item's "Needs a human"). Nothing limits how many asks a client may run at once (the api's own `429 busy` is the only brake). The browser's `Origin` is not checked; a cross-site request is
  stopped by the `application/json` requirement (it needs a preflight), as for the other write route.
- The template's `/api/v1/assistant/ask[/stream]` routes remain until web 4.1.

## [0.4.0] — 2026-10-07

### Added

- **`Sources` block** (web roadmap 3.2, `src/components/chat/Sources.tsx`): under an assistant message, one row per cited
  document — its title with its path beside it. Titles and paths come from indexed documents, so they are **plain text
  only**: no markup, no `<a>`, no link target (a path such as `javascript:alert(1)` is just characters). The rows are not
  numbered, because the `[n]` in an answer are the numbers of the passages the model was shown, not positions in this
  list. Nothing is drawn for a message with no citations.
- **The error line shows the trace id** (`ChatThread`): "Something went wrong (timeout). Trace ID: …", when the failure
  has one.
- A **size limit on one SSE event** (64,000 characters): a stream that never ends an event is cut off as a stream
  error instead of being buffered without bound. The connection is cancelled when the client stops reading early.
- A **size limit on an answer** (8,000 characters, twice the api's cap): a stream that keeps sending tokens ends with
  the failure kind `too_long`. Leaving a `ChatPanel` aborts its stream.

### Changed

- **`askStream` speaks the conversation stream** (`src/lib/chat-client.ts`; api roadmap 4.3): `sources` is
  `[{ title, path }]` and is every retrieved document; `done` carries `message_id`, token counts (numbers or `null`) and
  `citations` — the documents the answer cites, which **replace** the early list; `error` carries `error_kind`, `error`
  and `trace_id`. The handlers changed shape: `onSources(citations)`, `onDone({ messageId, citations, inputTokens, outputTokens })`, `onError({ kind, code, traceId })`.
  - Every field of every frame is checked before a handler sees it; a malformed or wrong-shaped frame is skipped.
    Kinds, codes and trace ids must match a pattern, citation lists and strings are bounded.
  - A request refused before the stream (`busy`, `payload_too_large`, `not_found`, `validation_error`, …) reports the
    api's code and trace id.
  - **A stream that ends without `done` or `error` is now a failure (`incomplete`)**, not a finished answer: the api
    stores nothing in that case, so showing the partial text as complete would be wrong.
- **The SSE parser follows the specification for line breaks** (`src/lib/sse.ts`): lines end with CR, LF or CRLF — and
  only those, so a token containing U+2028, U+2029 or U+0085 cannot split a frame — a CRLF split between two chunks is
  one break, and a value is no longer trimmed.
- `ChatMessage.sources` is `{ title, path }[]` (was `string[]`), `ChatMessage.error` is `{ kind, traceId? }` (was a
  string); `ChatWorkspace` keeps the titles of stored citations. Fixtures follow the api's frames: the stream fixture
  lists a retrieved-but-uncited document in `sources` and puts the cited ones in `done.citations`.

### Known limits

- The BFF stream routes (web 3.1) and sending from the page (web 3.3) are not built yet: `askStream` is exercised
  against fixtures and the `ChatPanel` test harness only.
- The template's demo route `/api/v1/assistant/ask/stream` (real mode too) still proxies the old `sources: string[]` shape,
  so against a live template api the demo shows no sources until web 4.1 removes it. In mock mode it now emits the conversation frame shapes in mock mode; it is
  removed in web 4.1.

## [0.3.0] — 2026-10-06

### Added

- **The `/chat` page** (web roadmap 2.2, `src/components/chat/ChatWorkspace.tsx`): conversations on the left, the open
  thread on the right, the input at the bottom. On load it fetches `GET /api/v1/conversations` (after mount, so times
  are in the browser's time zone) and shows the list or "No chats yet". **+ New chat** creates a conversation, puts it
  first, selects it and focuses the input; clicking a row opens it with its messages and their citations (`docs/…`
  paths, each once). The list and the thread each show a loading state and, when the BFF fails, an error line with the
  error code and the `trace_id` (a **Retry** button for the list). Opening a conversation while another is loading keeps
  the latest click; **+ New chat** pressed twice creates one conversation. Below 768 px the list is a drawer opened by
  a **Show conversations** button. Works against `MOCK_UPSTREAM=true`. Sending a message is not wired yet (web 3.3): the
  input keeps what was typed and says so. "No chats yet" is shown only once the list has loaded.
- **`src/lib/conversations-client.ts`**: the browser-side client for the three conversation routes. It never throws; a
  failure carries the error code and the `trace_id` (from the body, else the `x-trace-id` header), and a 200 reply
  missing the field the page needs is `invalid_response` rather than an endless spinner.
- The home page links to `/chat`. `MessageInput` accepts an optional `textareaRef`.

### Changed

- `/chat` no longer shows the template's single-turn demo (`ChatPanel` on `/api/v1/assistant/ask/stream`); the component
  and its BFF routes stay until web 4.1.

## [0.2.1] — 2026-10-06

### Added

- **`ConversationList` component** (web roadmap 2.1, `src/components/chat/ConversationList.tsx`): the chat page's sidebar —
  a **+ New chat** button and one row per conversation with its title and when it was last active, the open row
  marked `aria-current="true"` (announced by screen readers; the selected row keeps its tint on hover), long titles cut
  with an ellipsis (the full text is a tooltip on the text, so a screen reader does not hear the title twice), a focus ring
  that is not clipped by the scrolling list, "No chats yet" when there are none. Every row is a real `<button>`, so Tab, Enter and Space work. It is
  presentational: `onSelect(id)` and `onNewChat()` are callbacks and it never fetches. Titles render as text, never
  as markup.
- **`formatRelativeTime`** (`src/lib/relative-time.ts`): "today 10:12", "yesterday", "5 Oct", "5 Oct 2025" — by calendar
  day in a given (or the browser's) time zone, DST-safe, with fixed three-letter month names (`en-GB` would write
  "Sept") and cached formatters, and an unreadable timestamp yields an empty string instead of
  an error.
  No page uses them yet (roadmap web 2.2): nothing changes at runtime, hence a PATCH.

## [0.2.0] — 2026-10-06

### Added

- **Conversation BFF routes** (web roadmap 1.2): `GET` and `POST /api/v1/conversations` and
  `GET /api/v1/conversations/[id]`, mirroring the api's `/v1/conversations` (api 2.1). The api's status and JSON body
  pass through unchanged. The BFF validates what it forwards: `[id]` must be a UUID (it goes into the upstream URL),
  `limit` is 1–100, a create body may only carry a `title` of at most 200 UTF-16 units, must be `application/json`
  (`415` otherwise, so a cross-site "simple" request cannot create), and is capped at **4 KB** — refused with `413`
  while it is being read (a chunked upload declares no length) and read within 5 s, because the api reads a whole
  body before validating it. Towards the api: redirects are not followed, a reply over 8 MiB or not JSON is a `502`,
  and an error status whose body is not the api's `{ error, trace_id }` (a framework default) is a `502` too, so
  nothing else reaches the browser. Routes log their pattern and status only — never an id, a title or the upstream
  URL — and label the duration metric with the route pattern. With `MOCK_UPSTREAM=true` they serve the fixtures from
  0.1.1 (honouring `limit`). Helpers in `src/lib/conversations-bff.ts`. Nothing in the UI calls them yet (roadmap web 2.2).
- **Mock mode ignores whitespace and case in `APP_ENV`**: `" PROD "` is refused like `prod` (`src/lib/mocks.ts`).
  **Known gap:** the web's own spans still carry the concrete conversation id (roadmap web 1.3).

## [0.1.1] — 2026-10-06

### Added

- **Conversation types and mock fixtures** (web roadmap 1.1) for the chat page to be built against:
  `src/lib/conversation-types.ts` derives the create/list/open types from the generated `api-types` and hand-declares
  the ask response and the stream frames (`sources`, `token`, `done`, and a terminal `error` carrying
  `error_kind`, `error` and `trace_id`) until the api serves them (`TODO(contract)`, api 4.2/4.3).
  `src/mocks/conversations.ts` holds typed fixtures — an empty list, three conversations newest first, a detail for each
  (long, short, empty), a created conversation, a JSON answer, a streamed answer and a streamed failure — and
  `conversations.test.ts` checks the rules a type cannot (ordering, limits, frame order, `mockSse` round trip).
  No route or page uses them yet (roadmap web 1.2 and 2.2): nothing changes at runtime, hence a PATCH — the
  next MINOR is the first page or route that ships. Fixtures follow the api's rules (`updated_at` is the time of
  the last message; citations are `docs/…` paths, the only corpus the assistant indexes).

## [0.1.0] — 2026-09-28

### Added

- **UI foundation** (ADR-0010): Tailwind CSS v4 (`@tailwindcss/postcss`), chat components
  (`src/components/chat/`: `ChatThread`, `MessageInput`, `ChatPanel`), the browser streaming
  client `src/lib/chat-client.ts` (`askStream`), SSE helpers `src/lib/sse.ts`, and the example
  page `/chat`.
- **Streaming pass-through** for the BFF: `streamUpstream` + `proxyStream` (`src/lib/stream.ts`,
  5-minute whole-answer bound, `x-trace-id` forwarded, no proxy buffering).
- **Mock-upstream mode**: `MOCK_UPSTREAM=true` (server-side, refused in `prod`) makes a BFF
  route serve fixtures from `src/mocks/` (`src/lib/mocks.ts`, responses marked
  `x-mock-upstream: true`).
- Example business routes `POST /api/v1/assistant/ask` and `POST /api/v1/assistant/ask/stream`
  (mock or proxy to the api's `/v1/assistant/…`, which a project adds on `app/ai/`).
- **API contract types**: `src/lib/api-types.ts` generated from `docs/reference/openapi.json`
  (`pnpm api-types` / `make openapi`).
- **Component tests** (ADR-0011): React Testing Library in Vitest with per-file jsdom; coverage
  now includes `src/components/**/*.tsx`. New scripts: `typecheck`, `api-types`.

## [0.0.0] — 2026-09-20

- Template baseline. No features shipped.
