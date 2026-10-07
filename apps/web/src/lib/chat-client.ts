import type { Citation } from '@/lib/conversation-types';
import { readSseStream } from '@/lib/sse';

/**
 * Browser-side client for a streamed assistant answer (same-origin BFF only — the BFF rule).
 * Framework-free so it is unit-tested in the node environment; components wrap it.
 *
 * It speaks the conversation stream of `POST /api/v1/conversations/{id}/ask/stream`
 * (api roadmap 4.3, `docs/reference/http-api.md`): `sources`, `token`\*, then `done` or `error`.
 *
 * The stream is untrusted input — its text is written by a model and its titles and paths come from
 * indexed documents — so every field is checked before it reaches a handler: wrong-typed or
 * malformed frames are skipped, strings that should be a short word or an id are matched against a
 * pattern, and lists and lengths are bounded. Nothing here builds markup or a URL.
 */

/** Why an answer did not finish. Everything is bounded: a short word, a code, a trace id. */
export interface StreamFailure {
  /** A short snake_case word: `timeout`, `busy`, `aborted`, `network_error`, `incomplete`, … */
  kind: string;
  /** The api's error code when there was one (`ai_unavailable`, `not_found`, …). */
  code: string | null;
  /** The request's trace id, so a person can quote it. Null when the request never got an answer. */
  traceId: string | null;
}

/** What the `done` frame says. */
export interface StreamDone {
  /** The stored assistant message. */
  messageId: string | null;
  /** The documents the answer cites, exactly as stored. Empty when it cites none. */
  citations: Citation[];
  inputTokens: number | null;
  outputTokens: number | null;
}

export interface StreamHandlers {
  /** Every retrieved document, as soon as retrieval ends. Replace with `onDone`'s citations. */
  onSources?: (sources: Citation[]) => void;
  onToken: (text: string) => void;
  onDone?: (info: StreamDone) => void;
  /** Called at most once, and never after `onDone`. */
  onError: (failure: StreamFailure) => void;
}

export interface AskStreamOptions {
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

const KIND = /^[a-z][a-z0-9_]{0,39}$/;
const TRACE_ID = /^[0-9a-f]{32}$/;
const MAX_CITATIONS = 50;
const MAX_FIELD_CHARS = 200;
const MAX_ID_CHARS = 64;

function word(value: unknown): string | null {
  return typeof value === 'string' && KIND.test(value) ? value : null;
}

function traceIdOf(value: unknown): string | null {
  return typeof value === 'string' && TRACE_ID.test(value) ? value : null;
}

function countOf(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
}

function idOf(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 && value.length <= MAX_ID_CHARS
    ? value
    : null;
}

/** The well-formed `{ title, path }` entries of a list; anything else in it is dropped. */
function citationsOf(value: unknown): Citation[] {
  if (!Array.isArray(value)) return [];
  const out: Citation[] = [];
  for (const entry of value) {
    if (out.length >= MAX_CITATIONS) break;
    if (typeof entry !== 'object' || entry === null) continue;
    const { title, path } = entry as Record<string, unknown>;
    if (typeof title !== 'string' || typeof path !== 'string') continue;
    out.push({ title: title.slice(0, MAX_FIELD_CHARS), path: path.slice(0, MAX_FIELD_CHARS) });
  }
  return out;
}

function failure(kind: string, code: string | null = null, traceId: string | null = null) {
  return { kind, code, traceId } satisfies StreamFailure;
}

/** The failure for a request the api or the BFF refused before any stream started. */
async function refusal(res: Response): Promise<StreamFailure> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch (err) {
    if ((err as Error)?.name === 'AbortError') return failure('aborted');
    // not JSON: fall back on the status
  }
  const record = typeof body === 'object' && body !== null ? (body as Record<string, unknown>) : {};
  const code = word(record.error);
  const fallback =
    res.status === 429 ? 'busy' : res.status >= 500 ? 'upstream_error' : 'request_rejected';
  return failure(
    code ?? fallback,
    code,
    traceIdOf(record.trace_id) ?? traceIdOf(res.headers.get('x-trace-id')),
  );
}

/** The api caps an answer at 4,000 characters; the browser does not take that on trust. */
const MAX_ANSWER_CHARS = 8_000;

/**
 * POST `{ question }` to a same-origin SSE route and dispatch its events.
 * Resolves when the stream ends; every failure path calls `onError` once with a bounded failure.
 * A stream that ends without `done` or `error` is a failure (`incomplete`), not a finished answer.
 */
export async function askStream(
  endpoint: string,
  question: string,
  handlers: StreamHandlers,
  options: AskStreamOptions = {},
): Promise<void> {
  const fetchImpl = options.fetchImpl ?? fetch;
  let res: Response;
  try {
    res = await fetchImpl(endpoint, {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'text/event-stream' },
      body: JSON.stringify({ question }),
      signal: options.signal,
    });
  } catch (err) {
    handlers.onError(failure((err as Error)?.name === 'AbortError' ? 'aborted' : 'network_error'));
    return;
  }
  if (!res.ok) {
    handlers.onError(await refusal(res));
    return;
  }
  if (!res.body) {
    handlers.onError(failure('stream_error', null, traceIdOf(res.headers.get('x-trace-id'))));
    return;
  }

  const traceHeader = traceIdOf(res.headers.get('x-trace-id'));
  // The terminal handler runs after the read loop, so a handler that throws cannot turn a finished
  // answer into a second, contradictory callback.
  let terminal: { done: StreamDone } | { error: StreamFailure } | null = null;
  let answerChars = 0;
  try {
    for await (const event of readSseStream(res.body)) {
      let data: unknown;
      try {
        data = JSON.parse(event.data);
      } catch {
        continue; // a malformed frame is skipped, never fatal
      }
      if (typeof data !== 'object' || data === null || Array.isArray(data)) continue;
      const frame = data as Record<string, unknown>;

      if (event.event === 'sources') {
        handlers.onSources?.(citationsOf(frame.sources));
      } else if (event.event === 'token') {
        if (typeof frame.text !== 'string') continue;
        answerChars += frame.text.length;
        if (answerChars > MAX_ANSWER_CHARS) {
          terminal = { error: failure('too_long', null, traceHeader) };
          break;
        }
        handlers.onToken(frame.text);
      } else if (event.event === 'done') {
        terminal = {
          done: {
            messageId: idOf(frame.message_id),
            citations: citationsOf(frame.citations),
            inputTokens: countOf(frame.input_tokens),
            outputTokens: countOf(frame.output_tokens),
          },
        };
        break;
      } else if (event.event === 'error') {
        terminal = {
          error: failure(
            word(frame.error_kind) ?? 'error',
            word(frame.error),
            traceIdOf(frame.trace_id) ?? traceHeader,
          ),
        };
        break;
      }
      // any other event is one this client does not know: ignored
    }
  } catch (err) {
    terminal = {
      error: failure(
        (err as Error)?.name === 'AbortError' ? 'aborted' : 'stream_error',
        null,
        traceHeader,
      ),
    };
  }
  // The stream closed without a terminal frame: the api stored no answer, so this is not "done".
  terminal ??= { error: failure('incomplete', null, traceHeader) };
  if ('done' in terminal) handlers.onDone?.(terminal.done);
  else handlers.onError(terminal.error);
}
