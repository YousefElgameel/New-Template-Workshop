import type { components } from '@/lib/api-types';

/**
 * Types for the conversation surface (`/api/v1/conversations…`), the contract in
 * `docs/architecture/ARCHITECTURE.md` B4a.
 *
 * Two sources, on purpose:
 *
 * - **Generated** — every JSON body the api serves (create, list, open, ask) is derived from
 *   `@/lib/api-types` (`make openapi`), so a backend change breaks the type-check here instead of
 *   drifting silently.
 * - **Declared** — the stream frames, and only them: OpenAPI describes `…/ask/stream` as one
 *   `text/event-stream` string, so there is nothing to generate them from. Their reference is
 *   `docs/reference/http-api.md` (the frame table), and `conversation-contract.test.ts` fails when
 *   that table, these types, the fixtures or the client disagree about a field's name (the types' keys
 *   are checked by `pnpm typecheck`, the rest by Vitest).
 */
type Schemas = components['schemas'];

// --- generated (api 2.1) --------------------------------------------------------------------

/** A document an answer cites: its title and the repo path it came from. */
export type Citation = Schemas['Citation'];
/** `POST /api/v1/conversations` body — `title` is optional (blank → "New conversation"). */
export type CreateConversationBody = Schemas['ConversationCreate'];
/** `POST /api/v1/conversations` → 201. */
export type Conversation = Schemas['ConversationOut'];
/** One row of the conversation list. */
export type ConversationListItem = Schemas['ConversationListItem'];
/** `GET /api/v1/conversations` → `{ items, count }`, newest first. */
export type ConversationList = Schemas['ConversationList'];
/** A stored message: `role` is `user` or `assistant`; `citations` is null for user messages. */
export type ConversationMessage = Schemas['MessageOut'];
/** `GET /api/v1/conversations/{id}` — messages oldest first, at most 500 (`truncated`). */
export type ConversationDetail = Schemas['ConversationDetail'];

/** `POST /api/v1/conversations/{id}/ask` → 200: the stored message id, the answer, what it cites. */
export type ConversationAskResponse = Schemas['AskOut'];

// --- declared: the stream frames (api 4.3; `docs/reference/http-api.md`) --------------------

/**
 * `event: sources` — retrieval finished; arrives before any token. `sources` lists **every
 * retrieved document once**, cited or not; `count` is the number of passages retrieved (api 4.3,
 * `docs/reference/http-api.md`). Show it early and replace it with `DoneFrame.citations`.
 */
export interface SourcesFrame {
  sources: Citation[];
  count: number;
}

/** `event: token` — one chunk of the answer. */
export interface TokenFrame {
  text: string;
}

/**
 * `event: done` — the answer is complete and stored. `citations` are the documents the answer
 * actually cites, in citation order, exactly as stored (empty when it cites none). The token counts
 * are `null` when the model did not report them or the answer was cut.
 */
export interface DoneFrame {
  message_id: string;
  input_tokens: number | null;
  output_tokens: number | null;
  citations: Citation[];
}

/**
 * The failure kinds an `error` frame carries (api `error_kind`). The browser client reports more
 * as plain strings: `aborted`, `network_error`, `stream_error`, `incomplete`, `too_long`,
 * `upstream_error`, `request_rejected` and the api's JSON codes (`busy`, `not_found`, …) for a
 * request refused before the stream. All are short snake_case words, never exception text.
 */
export type StreamErrorKind =
  | 'timeout'
  | 'rate_limited'
  | 'network'
  | 'error'
  | 'empty_answer'
  | 'persist_failed';

/**
 * `event: error` — terminal: no `done` follows and no assistant message is stored. `error_kind`
 * is a bounded kind (never exception text); `error` is the api error code; `trace_id` is the
 * request's trace id so the UI can show it.
 */
export interface ErrorFrame {
  error_kind: string;
  error: string;
  trace_id: string;
}

/** Every frame of `POST /api/v1/conversations/{id}/ask/stream`, by event name. */
export type AskStreamEvent =
  | { event: 'sources'; data: SourcesFrame }
  | { event: 'token'; data: TokenFrame }
  | { event: 'done'; data: DoneFrame }
  | { event: 'error'; data: ErrorFrame };
