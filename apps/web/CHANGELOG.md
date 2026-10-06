# Changelog — `apps/web`

All notable changes to the **web** service (Next.js BFF + UI). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) from the consumer's perspective (root `CLAUDE.md`, rule 15).
The two services version independently.

## [Unreleased]

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
