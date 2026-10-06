import type { components } from '@/lib/api-types';

/**
 * Types for the conversation surface (`/api/v1/conversations…`), the contract in
 * `docs/architecture/ARCHITECTURE.md` B4a.
 *
 * Two sources, on purpose:
 *
 * - **Generated** — everything the api already serves (create, list, open: api 2.1) is derived
 *   from `@/lib/api-types` (`make openapi`), so a backend change breaks the type-check here
 *   instead of drifting silently.
 * - **Hand-declared** — the ask response and the stream frames, because the api routes for them
 *   (api 4.2 / 4.3) do not exist yet. Shapes follow api roadmap 4.2 / 4.3.
 *   TODO(contract): replace with generated types once api:4.2 / api:4.3 land (web roadmap 4.2).
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

// --- hand-declared (api 4.2 / 4.3) ----------------------------------------------------------

/** `POST /api/v1/conversations/{id}/ask` → 200. */
export interface ConversationAskResponse {
  message_id: string;
  answer: string;
  citations: Citation[];
}

/**
 * `event: sources` — retrieval finished; arrives before any token. `count` is carried over from
 * the template's frame (`apps/api/app/ai/streaming.py`); api 4.3 does not mention it, so treat it
 * as unconfirmed.
 */
export interface SourcesFrame {
  sources: Citation[];
  count: number;
}

/** `event: token` — one chunk of the answer. */
export interface TokenFrame {
  text: string;
}

/** `event: done` — the answer is complete and stored. */
export interface DoneFrame {
  message_id: string;
  input_tokens: number;
  output_tokens: number;
}

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
