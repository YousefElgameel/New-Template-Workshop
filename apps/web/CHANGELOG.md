# Changelog — `apps/web`

All notable changes to the **web** service (Next.js BFF + UI). The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[SemVer](https://semver.org/) from the consumer's perspective (root `CLAUDE.md`, rule 15).
The two services version independently.

## [Unreleased]

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
