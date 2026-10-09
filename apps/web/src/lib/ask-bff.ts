import { log } from '@/lib/logger';
import { sseFrame } from '@/lib/sse';
import { askLimiter, clientKeyOf, isSameOriginRequest, type Release } from '@/lib/ask-limits';
import {
  badRequest,
  busy,
  invalidOrigin,
  isJsonContentType,
  isUuid,
  payloadTooLarge,
  readBodyCapped,
  unsupportedMediaType,
} from '@/lib/conversations-bff';

/**
 * Shared pieces of the ask BFF routes (`/api/v1/conversations/[id]/ask[/stream]`, web roadmap 3.1).
 *
 * The question and the answer are user content: nothing here logs either. Routes log their PATTERN
 * and a status, never the id, the question or a token.
 */

/** The api's limit on a question, in characters (`docs/reference/http-api.md`). */
export const QUESTION_MAX_CHARS = 4000;
/**
 * Largest request body an ask may carry. 4,000 characters cost at most 12 bytes each as JSON
 * escapes, so 64 KiB is generous; the api reads a whole body before checking it, so the cap is here.
 */
export const MAX_ASK_BODY_BYTES = 64 * 1024;
/**
 * How long the BFF waits for the api's answer — one deadline for the whole call. The api's worst
 * case is about 4 minutes (60 s model timeout × 3 attempts, plus two 30 s retrieval steps — api
 * roadmap 4.2), so this is 5: `fetchUpstream`'s 10 s default must not cut an answer short. (undici's own
 * header and body timeouts are also 300 s.) This is the BFF → api hop only: the ingress in front of the
 * web app has its own request timeout, which is not configured by this repo.
 */
export const ASK_HOP_TIMEOUT_MS = 300_000;
/**
 * A slot is never held longer than the hop timeout plus this, whatever the reader does: a client
 * that stops reading without closing the connection must not pin it.
 */
export const SLOT_HOLD_MS = ASK_HOP_TIMEOUT_MS + 5_000;

/**
 * The deadline for the api call: the hop timeout, or the browser leaving — whichever comes first.
 * Without the second, a browser that gives up on a JSON ask would leave the api generating (and
 * holding one of its few concurrency slots) for up to 5 minutes.
 */
export function askSignal(req: Request): AbortSignal {
  return AbortSignal.any([req.signal, AbortSignal.timeout(ASK_HOP_TIMEOUT_MS)]);
}
/** Most bytes the BFF forwards of one answer stream; an answer is at most 4,000 characters. */
export const MAX_STREAM_BYTES = 1024 * 1024;

/** `403 invalid_origin` for a request a browser sent on behalf of another site, else `null`. */
export function checkOrigin(req: Request, traceId: string, route: string): Response | null {
  return isSameOriginRequest(req) ? null : invalidOrigin(route, traceId);
}

export type Slot = { ok: true; release: Release } | { ok: false; response: Response };

/**
 * Take one of this instance's ask slots for the requesting client, or the `429 busy` to send back.
 * Call it after the request is validated (a bad request must not use a slot) and release it on every
 * path out: `releaseWhenDone` for a stream, a `finally` for a JSON call.
 */
export function acquireAsk(req: Request, traceId: string, route: string): Slot {
  const taken = askLimiter().tryAcquire(clientKeyOf(req));
  if (!taken.ok) return { ok: false, response: busy(route, traceId, taken.why) };
  return { ok: true, release: taken.release };
}

const NUL = String.fromCharCode(0);
const LONE_SURROGATE = /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/;

export type AskBody =
  | { ok: true; question: string }
  | { ok: false; reason: 'invalid' | 'too_long' };

/**
 * Validate an ask body: a JSON object whose only key is `question`, a string that is not blank
 * after trimming, without NUL or a lone surrogate, at most 4,000 characters. Only `question` is
 * ever forwarded, trimmed.
 */
export function parseAskBody(text: string): AskBody {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { ok: false, reason: 'invalid' };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false, reason: 'invalid' };
  }
  const record = parsed as Record<string, unknown>;
  if (Object.keys(record).some((key) => key !== 'question')) {
    return { ok: false, reason: 'invalid' };
  }
  const raw = record.question;
  if (typeof raw !== 'string') return { ok: false, reason: 'invalid' };
  const question = raw.trim();
  if (question === '' || question.includes(NUL) || LONE_SURROGATE.test(question)) {
    return { ok: false, reason: 'invalid' };
  }
  // The api counts characters (code points), not UTF-16 units.
  if ([...question].length > QUESTION_MAX_CHARS) return { ok: false, reason: 'too_long' };
  return { ok: true, question };
}

export type AskRequest =
  | { ok: true; id: string; question: string }
  | { ok: false; response: Response };

/**
 * Everything the BFF checks before it calls the api, in order; each failure is a response of its
 * own and the api is not called: `[id]` is a UUID (it goes into the upstream URL) → `400`; the body
 * is declared JSON (a cross-site "simple" request has no preflight) → `415`; at most 64 KiB, read
 * within 5 s → `413` / `400`; a valid question → `400`, or `413` above 4,000 characters.
 */
export async function readAskRequest(
  req: Request,
  rawId: string,
  traceId: string,
  route: string,
): Promise<AskRequest> {
  if (!isUuid(rawId)) return { ok: false, response: badRequest(route, traceId) };
  if (!isJsonContentType(req)) {
    return { ok: false, response: unsupportedMediaType(route, traceId) };
  }
  const read = await readBodyCapped(req, MAX_ASK_BODY_BYTES);
  if (read.kind === 'too_large') return { ok: false, response: payloadTooLarge(route, traceId) };
  if (read.kind === 'unreadable') return { ok: false, response: badRequest(route, traceId) };
  const body = parseAskBody(read.text);
  if (!body.ok) {
    return {
      ok: false,
      response:
        body.reason === 'too_long' ? payloadTooLarge(route, traceId) : badRequest(route, traceId),
    };
  }
  return { ok: true, id: rawId, question: body.question };
}

/**
 * Pass the api's byte stream on unchanged — except when it fails: an upstream read error, or more
 * than `maxBytes`, ends the stream with one terminal `error` frame (a bounded `error_kind` and the
 * request's `trace_id`) instead of a silent cut, so the page can say what happened. A browser that
 * leaves cancels the upstream read, which ends the api's request and frees its concurrency slot.
 */
export function guardStream(
  upstream: ReadableStream<Uint8Array>,
  traceId: string,
  route: string,
  maxBytes = MAX_STREAM_BYTES,
): ReadableStream<Uint8Array> {
  const reader = upstream.getReader();
  const encoder = new TextEncoder();
  let forwarded = 0;
  let tail = ''; // the last bytes forwarded: did the stream stop between frames?
  const fail = (controller: ReadableStreamDefaultController<Uint8Array>, kind: string) => {
    log('error', 'bff stream ended early', { route, why: kind });
    const frame = sseFrame('error', {
      error_kind: kind,
      error: 'upstream_error',
      trace_id: traceId,
    });
    // An upstream that died inside a frame must not have the error glued onto the half frame.
    const between = tail === '' || /(\n\n|\r\n\r\n|\r\r)$/.test(tail);
    controller.enqueue(encoder.encode((between ? '' : '\n\n') + frame));
    controller.close();
  };
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const { done, value } = await reader.read();
        if (done) return controller.close();
        forwarded += value.byteLength;
        if (forwarded > maxBytes) {
          await reader.cancel().catch(() => undefined);
          return fail(controller, 'too_long');
        }
        tail = (tail + new TextDecoder('latin1').decode(value.slice(-4))).slice(-4);
        controller.enqueue(value);
      } catch {
        fail(controller, 'network');
      }
    },
    async cancel(reason) {
      await reader.cancel(reason).catch(() => undefined);
    },
  });
}
