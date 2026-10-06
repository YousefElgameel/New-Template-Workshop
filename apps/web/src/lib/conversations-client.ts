import type { Conversation, ConversationDetail, ConversationList } from '@/lib/conversation-types';

/**
 * Browser-side client for the conversation BFF routes (`/api/v1/conversations…`) — same-origin
 * only (the BFF rule). Framework-free so it is unit-tested in the node environment; the chat
 * page wraps it.
 *
 * Every call resolves to a result and never throws: a failure carries the bounded `error` code
 * and the `trace_id` the page shows, so a person can quote it. The id comes from the error body
 * (`{ error, trace_id }`), else the `x-trace-id` response header; a failure with neither (the
 * network is down) has `traceId: null`.
 */
export type ClientResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: string; traceId: string | null };

export interface ClientOptions {
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

const BASE = '/api/v1/conversations';
const TRACE_HEADER = 'x-trace-id';

function failure(error: string, traceId: string | null = null): ClientResult<never> {
  return { ok: false, error, traceId };
}

async function request<T>(
  path: string,
  init: RequestInit,
  { signal, fetchImpl = fetch }: ClientOptions,
  isValid: (body: unknown) => boolean = () => true,
): Promise<ClientResult<T>> {
  let res: Response;
  try {
    res = await fetchImpl(path, { ...init, signal, cache: 'no-store' });
  } catch {
    // Offline, DNS, or an abort: nothing reached the BFF, so there is no trace id.
    return failure('network_error');
  }

  const headerTrace = res.headers.get(TRACE_HEADER);
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    return failure(res.ok ? 'invalid_response' : 'upstream_error', headerTrace);
  }

  if (!res.ok) {
    const record = (body ?? {}) as Record<string, unknown>;
    return failure(
      typeof record.error === 'string' ? record.error : 'upstream_error',
      typeof record.trace_id === 'string' ? record.trace_id : headerTrace,
    );
  }
  if (!isValid(body)) return failure('invalid_response', headerTrace);
  return { ok: true, data: body as T };
}

// A 200 whose body lacks the field the page iterates would throw there; reject it here instead.
const isRecord = (b: unknown): b is Record<string, unknown> =>
  typeof b === 'object' && b !== null && !Array.isArray(b);
const hasArray = (key: string) => (b: unknown) => isRecord(b) && Array.isArray(b[key]);
const hasId = (b: unknown) =>
  isRecord(b) && typeof b.id === 'string' && typeof b.title === 'string';

/** `GET /api/v1/conversations` — newest first. */
export function listConversations(options: ClientOptions = {}) {
  return request<ConversationList>(BASE, { method: 'GET' }, options, hasArray('items'));
}

/** `POST /api/v1/conversations` — an untitled conversation unless `title` is given. */
export function createConversation(title?: string, options: ClientOptions = {}) {
  const init: RequestInit =
    title === undefined
      ? { method: 'POST' }
      : {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ title }),
        };
  return request<Conversation>(BASE, init, options, hasId);
}

/** `GET /api/v1/conversations/{id}` — messages oldest first. */
export function getConversation(id: string, options: ClientOptions = {}) {
  return request<ConversationDetail>(
    `${BASE}/${encodeURIComponent(id)}`,
    { method: 'GET' },
    options,
    hasArray('messages'),
  );
}
