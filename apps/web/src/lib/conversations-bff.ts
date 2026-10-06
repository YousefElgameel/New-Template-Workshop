import { log } from '@/lib/logger';
import { fetchUpstream } from '@/lib/trace';

/**
 * Shared pieces of the conversation BFF routes (`/api/v1/conversations…`, web roadmap 1.2).
 *
 * The BFF is the one inbound edge, so it validates what it forwards (rule 50): an id is placed
 * into an upstream URL, a body is read in full before anyone parses it. None of it is user
 * content we may log — routes log their PATTERN and the status, never an id, a title or text.
 */

/** Most characters of a conversation title — the api's `NVARCHAR(200)` counts UTF-16 units, like `String.length`. */
export const TITLE_MAX_UNITS = 200;
/** Fewest/most conversations one list call may ask for (api 2.1). */
export const MIN_LIMIT = 1;
export const MAX_LIMIT = 100;
/**
 * Largest body a create call may carry. The api reads a whole body before validating it, so the
 * cap lives here; `{ "title": "…" }` with 200 characters is well under 1 KB even in UTF-8.
 */
export const MAX_CREATE_BODY_BYTES = 4096;
/** How long a client may take to send that body — a slow trickle must not hold a handler open. */
export const BODY_READ_TIMEOUT_MS = 5_000;
/**
 * Largest api reply the BFF will buffer. A thread is bounded by count (500 messages) but not yet
 * by bytes, so a defensive ceiling keeps one oversized conversation from exhausting memory.
 */
export const MAX_UPSTREAM_BYTES = 8 * 1024 * 1024;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(value: string): boolean {
  return UUID_RE.test(value);
}

/** The base URL of the api (server-side env only — never `NEXT_PUBLIC_*`). */
function apiBase(): string {
  return process.env.API_BASE_URL ?? 'http://localhost:8000';
}

function reject(route: string, traceId: string, status: number, error: string): Response {
  // Bounded fields only: the pattern and the status. What was wrong is deliberately not echoed.
  log('warn', 'bff rejected request', { route, status });
  return Response.json({ error, trace_id: traceId }, { status });
}

/** `400` — invalid input or an unreadable body. */
export function badRequest(route: string, traceId: string): Response {
  return reject(route, traceId, 400, 'invalid_request');
}

/** `413` — the body was larger than a create call can legitimately be. */
export function payloadTooLarge(route: string, traceId: string): Response {
  return reject(route, traceId, 413, 'payload_too_large');
}

/** `415` — a body that is not declared as JSON. */
export function unsupportedMediaType(route: string, traceId: string): Response {
  return reject(route, traceId, 415, 'unsupported_media_type');
}

/** `limit` query parameter → a number in 1–100, `undefined` when absent, `null` when invalid. */
export function parseLimit(value: string | null): number | undefined | null {
  if (value === null) return undefined;
  if (!/^\d{1,3}$/.test(value)) return null;
  const limit = Number(value);
  return limit >= MIN_LIMIT && limit <= MAX_LIMIT ? limit : null;
}

/** Whether the request declares a JSON body (`application/json`, optionally with parameters). */
export function isJsonContentType(req: Request): boolean {
  return /^application\/json\s*(;|$)/i.test(req.headers.get('content-type') ?? '');
}

export type BodyRead =
  | { kind: 'ok'; text: string }
  | { kind: 'too_large' }
  | { kind: 'unreadable' };

/** Concatenate chunks and decode them as UTF-8. */
function decode(chunks: Uint8Array[], total: number): string {
  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}

/**
 * Read a stream but never more than `maxBytes`, and never for longer than `timeoutMs`. `null`
 * means the cap was exceeded; a failure or a timeout is `unreadable`. The reader is cancelled in
 * both cases so the rest of an oversized or stalled upload is not consumed.
 */
async function readCapped(
  body: ReadableStream<Uint8Array>,
  maxBytes: number,
  timeoutMs: number,
): Promise<BodyRead> {
  const reader = body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    void reader.cancel().catch(() => undefined); // makes the pending read() settle
  }, timeoutMs);
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (timedOut) return { kind: 'unreadable' };
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel().catch(() => undefined);
        return { kind: 'too_large' };
      }
      chunks.push(value);
    }
    return { kind: 'ok', text: decode(chunks, total) };
  } catch {
    // The client went away mid-upload, or the stream errored.
    return { kind: 'unreadable' };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Read a request body: `too_large` if the declared length, or the bytes actually streamed (a
 * chunked upload declares none), exceed `maxBytes`; `unreadable` on a failure or a stall.
 */
export async function readBodyCapped(
  req: Request,
  maxBytes: number,
  timeoutMs = BODY_READ_TIMEOUT_MS,
): Promise<BodyRead> {
  const declared = req.headers.get('content-length');
  if (declared !== null && Number(declared) > maxBytes) return { kind: 'too_large' };
  if (!req.body) return { kind: 'ok', text: '' };
  return readCapped(req.body, maxBytes, timeoutMs);
}

export type CreateBody = { ok: true; title: string | null | undefined } | { ok: false };

/**
 * Validate a create body: empty, `null` or `{}` are fine; otherwise a JSON object whose only
 * allowed key is `title`, a string of at most 200 characters (or null). Anything else is
 * rejected here — and only `title` is ever forwarded.
 */
export function parseCreateBody(text: string): CreateBody {
  if (text.trim() === '') return { ok: true, title: undefined };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { ok: false };
  }
  if (parsed === null) return { ok: true, title: undefined };
  if (typeof parsed !== 'object' || Array.isArray(parsed)) return { ok: false };

  const record = parsed as Record<string, unknown>;
  if (Object.keys(record).some((key) => key !== 'title')) return { ok: false };
  const title = record.title;
  if (title === undefined || title === null) return { ok: true, title: title as null | undefined };
  if (typeof title !== 'string' || title.length > TITLE_MAX_UNITS) return { ok: false };
  return { ok: true, title };
}

/** The api's error shape, `{ error, trace_id }` — the only error body the browser may see. */
function isApiErrorBody(data: unknown): boolean {
  if (typeof data !== 'object' || data === null) return false;
  const record = data as Record<string, unknown>;
  return typeof record.error === 'string' && typeof record.trace_id === 'string';
}

function upstreamError(traceId: string): Response {
  return Response.json(
    { error: 'upstream_error', target: 'api', trace_id: traceId },
    { status: 502 },
  );
}

/**
 * Call the api and hand its JSON answer (status included) to the browser: a `404` or `422`
 * from the api passes through with its own `{ error, trace_id }` body. A network failure, a
 * redirect, an oversized reply, a reply that is not JSON, or an error status whose body is not
 * the api's `{ error, trace_id }` shape (a framework default must not reach the browser)
 * becomes a `502` carrying our trace id. Logs the route PATTERN and the status — never the
 * URL, because it can contain a conversation id.
 */
export async function callApi(
  path: string,
  traceId: string,
  route: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    // `redirect: 'error'`: the api never redirects; following one would send the trace header on.
    const res = await fetchUpstream(`${apiBase()}${path}`, traceId, { ...init, redirect: 'error' });
    const read = res.body
      ? await readCapped(res.body, MAX_UPSTREAM_BYTES, BODY_READ_TIMEOUT_MS * 6)
      : ({ kind: 'ok', text: '' } as BodyRead);
    if (read.kind !== 'ok') {
      log('error', 'bff upstream body unusable', { route, status: res.status, why: read.kind });
      return upstreamError(traceId);
    }
    let data: unknown;
    try {
      data = JSON.parse(read.text);
    } catch {
      log('error', 'bff upstream returned a non-JSON body', { route, status: res.status });
      return upstreamError(traceId);
    }
    if (res.status >= 400 && !isApiErrorBody(data)) {
      log('error', 'bff upstream error without the api error shape', { route, status: res.status });
      return upstreamError(traceId);
    }
    log('info', 'bff upstream ok', { route, status: res.status });
    return Response.json(data, { status: res.status });
  } catch {
    // The error text can carry the URL (and so an id): log the route only.
    log('error', 'bff upstream failed', { route });
    return Response.json(
      { error: 'upstream_unreachable', target: 'api', trace_id: traceId },
      { status: 502 },
    );
  }
}
