/**
 * Server-Sent Events helpers shared by the BFF (proxying / mocking a stream) and the
 * browser (consuming it). Pure functions + streams — no framework imports, so the parser
 * is unit-tested in the node environment.
 *
 * Wire format (matches apps/api `app/ai/streaming.py`):
 *   event: <name>\n
 *   data: <json>\n
 *   \n
 *
 * Lines end with CR, LF or CRLF — and only those, as the SSE specification says. Other
 * Unicode line separators (U+2028, U+2029, U+0085) are ordinary characters inside a line: a
 * model-written token that contains one must not be able to split a frame. (The api also
 * escapes them in the JSON; the parser does not rely on that.)
 */

export interface SseEvent {
  event: string;
  data: string;
}

/** The longest event the parser will buffer, in characters. A stream that never ends an event is cut off. */
export const MAX_SSE_EVENT_CHARS = 64_000;

export class SseEventTooLargeError extends Error {
  constructor() {
    super('SSE event exceeds the size limit');
    this.name = 'SseEventTooLargeError';
  }
}

/** Build one SSE frame. `data` is JSON-encoded. */
export function sseFrame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * Parse one blank-line-delimited block into an event. Comment lines (`:`) are ignored,
 * multiple `data:` lines are joined with `\n`, a block without `event:` is a `message`. Lines are
 * split on CR, LF and CRLF only; a value keeps everything but one leading space.
 * Returns null for a block with no data.
 */
export function parseSseBlock(block: string): SseEvent | null {
  let event = 'message';
  const data: string[] = [];
  for (const line of block.split(/\r\n|\r|\n/)) {
    if (!line || line.startsWith(':')) continue;
    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);
    if (field === 'event') event = value;
    else if (field === 'data') data.push(value);
  }
  if (data.length === 0) return null;
  return { event, data: data.join('\n') };
}

/**
 * Iterate the events of an SSE body stream. Line endings are normalised to LF as bytes arrive, so a
 * CRLF (or a lone CR) split across two chunks is still one line break, and blank-line boundaries
 * work for any of the three. Throws `SseEventTooLargeError` if one event outgrows
 * `MAX_SSE_EVENT_CHARS` without ending.
 */
export async function* readSseStream(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<SseEvent, void, undefined> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let heldCr = false; // a CR at the end of a chunk: wait to see whether an LF follows

  const normalise = (chunk: string, final: boolean): string => {
    let text = heldCr ? `\r${chunk}` : chunk;
    heldCr = false;
    if (!final && text.endsWith('\r')) {
      heldCr = true;
      text = text.slice(0, -1);
    }
    return text.replace(/\r\n?/g, '\n');
  };

  let finished = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        finished = true;
        break;
      }
      buffer += normalise(decoder.decode(value, { stream: true }), false);
      let boundary = buffer.indexOf('\n\n');
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseSseBlock(block);
        if (event) yield event;
        boundary = buffer.indexOf('\n\n');
      }
      if (buffer.length > MAX_SSE_EVENT_CHARS) throw new SseEventTooLargeError();
    }
    buffer += normalise(decoder.decode(), true);
    if (buffer.trim()) {
      const event = parseSseBlock(buffer);
      if (event) yield event;
    }
  } finally {
    // Left early (a terminal frame, an oversize event, the caller stopped): close the connection.
    if (!finished) await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

/** A byte stream that emits the given frames (optionally paced) — used by mocks and tests. */
export function sseStreamFromFrames(frames: string[], delayMs = 0): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    async start(controller) {
      for (const frame of frames) {
        if (delayMs > 0) await new Promise((r) => setTimeout(r, delayMs));
        controller.enqueue(encoder.encode(frame));
      }
      controller.close();
    },
  });
}
