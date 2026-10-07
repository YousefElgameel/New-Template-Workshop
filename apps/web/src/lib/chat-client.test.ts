import { describe, expect, it, vi } from 'vitest';
import { askStream } from '@/lib/chat-client';
import { sseFrame, sseStreamFromFrames } from '@/lib/sse';

const TRACE = '0c700' + 'd'.repeat(27);
const OTHER_TRACE = '0eb01' + 'a'.repeat(27);

function sseResponse(frames: string[], headers: Record<string, string> = {}) {
  return new Response(sseStreamFromFrames(frames), {
    status: 200,
    headers: { 'content-type': 'text/event-stream', ...headers },
  });
}

function handlers() {
  return {
    onSources: vi.fn(),
    onToken: vi.fn(),
    onDone: vi.fn(),
    onError: vi.fn(),
  };
}

type H = ReturnType<typeof handlers>;

async function run(response: Response | Error, h: H = handlers(), signal?: AbortSignal) {
  const fetchImpl = vi.fn(async () => {
    if (response instanceof Error) throw response;
    return response;
  });
  await askStream('/api/v1/conversations/x/ask/stream', 'q', h, { fetchImpl, signal });
  return { h, fetchImpl };
}

const DOC_A = { title: 'Doc A', path: 'docs/a.md' };
const DOC_B = { title: 'Doc B', path: 'docs/b.md' };

describe('askStream (browser → BFF) — the conversation stream', () => {
  it('posts the question and dispatches sources → tokens → done', async () => {
    const { h, fetchImpl } = await run(
      sseResponse([
        sseFrame('sources', { sources: [DOC_A, DOC_B], count: 3 }),
        sseFrame('token', { text: 'Hel' }),
        sseFrame('token', { text: 'lo' }),
        sseFrame('done', {
          message_id: 'c0000000-0000-4000-8000-000000000001',
          input_tokens: 3,
          output_tokens: 2,
          citations: [DOC_A],
        }),
      ]),
    );

    const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/v1/conversations/x/ask/stream');
    expect(init.method).toBe('POST');
    expect(init.headers).toEqual({
      'content-type': 'application/json',
      accept: 'text/event-stream',
    });
    expect(JSON.parse(init.body as string)).toEqual({ question: 'q' });
    expect(h.onSources).toHaveBeenCalledWith([DOC_A, DOC_B]);
    expect(h.onToken.mock.calls.map((c) => c[0]).join('')).toBe('Hello');
    expect(h.onDone).toHaveBeenCalledWith({
      messageId: 'c0000000-0000-4000-8000-000000000001',
      citations: [DOC_A],
      inputTokens: 3,
      outputTokens: 2,
    });
    expect(h.onError).not.toHaveBeenCalled();
  });

  it('passes the abort signal to fetch', async () => {
    const controller = new AbortController();
    const { fetchImpl } = await run(
      sseResponse([sseFrame('done', {})]),
      handlers(),
      controller.signal,
    );
    const [, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(init.signal).toBe(controller.signal);
  });

  describe('frame validation', () => {
    it('keeps only well-formed citations, bounds the list and the strings', async () => {
      const many = Array.from({ length: 80 }, (_, i) => ({ title: `T${i}`, path: `docs/${i}.md` }));
      const { h } = await run(
        sseResponse([
          sseFrame('sources', {
            sources: [
              DOC_A,
              'docs/legacy-string.md',
              null,
              7,
              { title: 'no path' },
              { path: 'no title' },
              { title: 5, path: 'docs/x.md' },
              { title: 'x'.repeat(500), path: 'p'.repeat(500) },
              ...many,
            ],
          }),
          sseFrame('done', { citations: 'nope' }),
        ]),
      );
      const [sources] = h.onSources.mock.calls[0] as [{ title: string; path: string }[]];
      expect(sources[0]).toEqual(DOC_A);
      expect(sources[1].title).toHaveLength(200);
      expect(sources[1].path).toHaveLength(200);
      expect(sources).toHaveLength(50); // bounded
      expect(sources.every((s) => typeof s.title === 'string' && typeof s.path === 'string')).toBe(
        true,
      );
      expect(h.onDone).toHaveBeenCalledWith({
        messageId: null,
        citations: [],
        inputTokens: null,
        outputTokens: null,
      });
    });

    it('a sources frame without a list is an empty list, not an error', async () => {
      const { h } = await run(
        sseResponse([sseFrame('sources', { count: 2 }), sseFrame('done', {})]),
      );
      expect(h.onSources).toHaveBeenCalledWith([]);
    });

    it.each([
      [-1, null],
      [1.5, null],
      ['7', null],
      [Number.NaN, null],
      [true, null],
      [null, null],
      [0, 0],
      [41, 41],
    ])('token count %j becomes %j', async (given, expected) => {
      const { h } = await run(
        sseResponse([sseFrame('done', { input_tokens: given, output_tokens: given })]),
      );
      const [info] = h.onDone.mock.calls[0] as [{ inputTokens: unknown; outputTokens: unknown }];
      expect(info.inputTokens).toBe(expected);
      expect(info.outputTokens).toBe(expected);
    });

    it.each([
      ['', null],
      ['x'.repeat(65), null],
      [5, null],
      [null, null],
      ['abc', 'abc'],
    ])('message id %j becomes %j', async (given, expected) => {
      const { h } = await run(sseResponse([sseFrame('done', { message_id: given })]));
      expect((h.onDone.mock.calls[0] as [{ messageId: unknown }])[0].messageId).toBe(expected);
    });

    it('skips malformed frames and frames of the wrong shape, and ignores unknown events', async () => {
      const { h } = await run(
        sseResponse([
          'event: token\ndata: {broken\n\n',
          'event: token\ndata: ["not","an","object"]\n\n',
          'event: token\ndata: "a string"\n\n',
          'event: token\ndata: null\n\n',
          sseFrame('token', { text: 5 }),
          sseFrame('token', {}),
          sseFrame('mystery', { text: 'ignored' }),
          sseFrame('token', { text: 'ok' }),
          sseFrame('done', {}),
        ]),
      );
      expect(h.onToken.mock.calls).toEqual([['ok']]);
      expect(h.onDone).toHaveBeenCalledTimes(1);
      expect(h.onError).not.toHaveBeenCalled();
    });

    it('stops at the terminal frame: nothing after done is delivered', async () => {
      const { h } = await run(
        sseResponse([
          sseFrame('done', {}),
          sseFrame('token', { text: 'late' }),
          sseFrame('error', {}),
        ]),
      );
      expect(h.onToken).not.toHaveBeenCalled();
      expect(h.onError).not.toHaveBeenCalled();
      expect(h.onDone).toHaveBeenCalledTimes(1);
    });

    it('delivers a token that contains Unicode line separators intact', async () => {
      const text = `a${String.fromCharCode(0x2028)}event: done${String.fromCharCode(0x2029)}b${String.fromCharCode(0x85)}c`;
      const { h } = await run(sseResponse([sseFrame('token', { text }), sseFrame('done', {})]));
      expect(h.onToken.mock.calls).toEqual([[text]]);
      expect(h.onDone).toHaveBeenCalledTimes(1); // the fake `event: done` inside the token did not count
    });
  });

  describe('error frames', () => {
    it('carries the kind, the code and the trace id', async () => {
      const { h } = await run(
        sseResponse([
          sseFrame('sources', { sources: [DOC_A] }),
          sseFrame('token', { text: 'part' }),
          sseFrame('error', { error_kind: 'timeout', error: 'ai_unavailable', trace_id: TRACE }),
          sseFrame('token', { text: 'never' }),
        ]),
      );
      expect(h.onError).toHaveBeenCalledTimes(1);
      expect(h.onError).toHaveBeenCalledWith({
        kind: 'timeout',
        code: 'ai_unavailable',
        traceId: TRACE,
      });
      expect(h.onToken.mock.calls).toEqual([['part']]);
      expect(h.onDone).not.toHaveBeenCalled();
    });

    it.each([
      ['TIMEOUT'],
      ['has space'],
      ['<script>alert(1)</script>'],
      ['x'.repeat(41)],
      [''],
      [5],
      [null],
    ])('an unusable kind %j becomes plain "error"', async (kind) => {
      const { h } = await run(sseResponse([sseFrame('error', { error_kind: kind })]));
      expect(h.onError).toHaveBeenCalledWith({ kind: 'error', code: null, traceId: null });
    });

    it('accepts only a 32-hex trace id and falls back on the response header', async () => {
      const bad = await run(
        sseResponse([sseFrame('error', { error_kind: 'error', trace_id: '<b>x</b>' })], {
          'x-trace-id': OTHER_TRACE,
        }),
      );
      expect(bad.h.onError).toHaveBeenCalledWith({
        kind: 'error',
        code: null,
        traceId: OTHER_TRACE,
      });
      const none = await run(sseResponse([sseFrame('error', { error_kind: 'error' })]));
      expect(none.h.onError).toHaveBeenCalledWith({ kind: 'error', code: null, traceId: null });
    });
  });

  describe('bounds on the stream', () => {
    it('stops with "too_long" once the answer outgrows the cap, and ignores the rest', async () => {
      const { h } = await run(
        sseResponse([
          sseFrame('token', { text: 'a'.repeat(5000) }),
          sseFrame('token', { text: 'b'.repeat(5000) }),
          sseFrame('token', { text: 'late' }),
          sseFrame('done', { message_id: 'm', citations: [] }),
        ]),
      );
      expect(h.onToken).toHaveBeenCalledTimes(1);
      expect(h.onError).toHaveBeenCalledTimes(1);
      expect(h.onError.mock.calls[0][0].kind).toBe('too_long');
      expect(h.onDone).not.toHaveBeenCalled();
    });

    it('cancels the response body when a terminal frame arrives before the stream ends', async () => {
      const cancel = vi.fn();
      const encoder = new TextEncoder();
      const body = new ReadableStream<Uint8Array>({
        start(c) {
          c.enqueue(encoder.encode(sseFrame('done', { message_id: 'm', citations: [] })));
        },
        cancel,
      });
      const { h } = await run(new Response(body, { status: 200 }));
      expect(h.onDone).toHaveBeenCalledTimes(1);
      expect(cancel).toHaveBeenCalled();
    });

    it('reports "aborted" when the caller stops while a refusal body is being read', async () => {
      const res = new Response(null, { status: 429 });
      vi.spyOn(res, 'json').mockRejectedValue(new DOMException('stop', 'AbortError'));
      const { h } = await run(res);
      expect(h.onError).toHaveBeenCalledWith({ kind: 'aborted', code: null, traceId: null });
    });

    it('does not call onError when onDone itself throws', async () => {
      const h = handlers();
      h.onDone.mockImplementation(() => {
        throw new Error('boom');
      });
      await expect(
        run(sseResponse([sseFrame('done', { message_id: 'm', citations: [] })]), h),
      ).rejects.toThrow('boom');
      expect(h.onError).not.toHaveBeenCalled();
    });
  });

  describe('failures before and around the stream', () => {
    it('a stream that ends without done or error is "incomplete", not a finished answer', async () => {
      const { h } = await run(
        sseResponse(
          [sseFrame('sources', { sources: [DOC_A] }), sseFrame('token', { text: 'half' })],
          {
            'x-trace-id': TRACE,
          },
        ),
      );
      expect(h.onError).toHaveBeenCalledTimes(1);
      expect(h.onError).toHaveBeenCalledWith({ kind: 'incomplete', code: null, traceId: TRACE });
      expect(h.onDone).not.toHaveBeenCalled();
    });

    it.each([
      [429, { error: 'busy', trace_id: TRACE }, 'busy', 'busy', TRACE],
      [
        413,
        { error: 'payload_too_large', trace_id: TRACE },
        'payload_too_large',
        'payload_too_large',
        TRACE,
      ],
      [404, { error: 'not_found', trace_id: TRACE }, 'not_found', 'not_found', TRACE],
      [
        422,
        { error: 'validation_error', trace_id: TRACE },
        'validation_error',
        'validation_error',
        TRACE,
      ],
      [
        400,
        { error: 'invalid_request', trace_id: TRACE },
        'invalid_request',
        'invalid_request',
        TRACE,
      ],
    ])(
      'a refused request (%i) reports the api code and trace id',
      async (status, body, kind, code, trace) => {
        const { h } = await run(new Response(JSON.stringify(body), { status }));
        expect(h.onError).toHaveBeenCalledWith({ kind, code, traceId: trace });
        expect(h.onToken).not.toHaveBeenCalled();
      },
    );

    it.each([
      [429, 'busy'],
      [502, 'upstream_error'],
      [503, 'upstream_error'],
      [400, 'request_rejected'],
      [404, 'request_rejected'],
    ])('a refusal with no usable body (%i) falls back to "%s"', async (status, kind) => {
      const plain = await run(new Response('<html>nope</html>', { status }));
      expect(plain.h.onError).toHaveBeenCalledWith({ kind, code: null, traceId: null });
      const hostile = await run(
        new Response(JSON.stringify({ error: '<script>alert(1)</script>', trace_id: 'x' }), {
          status,
        }),
      );
      expect(hostile.h.onError).toHaveBeenCalledWith({ kind, code: null, traceId: null });
    });

    it('takes the trace id from the header when the body has none', async () => {
      const { h } = await run(
        new Response(JSON.stringify({ error: 'busy' }), {
          status: 429,
          headers: { 'x-trace-id': OTHER_TRACE },
        }),
      );
      expect(h.onError).toHaveBeenCalledWith({ kind: 'busy', code: 'busy', traceId: OTHER_TRACE });
    });

    it('maps a network failure and an abort before the response to bounded kinds', async () => {
      const down = await run(new Error('down'));
      expect(down.h.onError).toHaveBeenCalledWith({
        kind: 'network_error',
        code: null,
        traceId: null,
      });
      const abort = new Error('x');
      abort.name = 'AbortError';
      const aborted = await run(abort);
      expect(aborted.h.onError).toHaveBeenCalledWith({
        kind: 'aborted',
        code: null,
        traceId: null,
      });
    });

    it('an abort or a break in the middle of the stream is reported once, with the header trace id', async () => {
      const make = (error: Error) => {
        let sent = false;
        const body = new ReadableStream<Uint8Array>({
          pull(controller) {
            if (!sent) {
              sent = true;
              controller.enqueue(new TextEncoder().encode(sseFrame('token', { text: 'a' })));
            } else {
              controller.error(error);
            }
          },
        });
        return new Response(body, { status: 200, headers: { 'x-trace-id': TRACE } });
      };
      const abort = new Error('x');
      abort.name = 'AbortError';
      const a = await run(make(abort));
      expect(a.h.onToken.mock.calls).toEqual([['a']]);
      expect(a.h.onError).toHaveBeenCalledTimes(1);
      expect(a.h.onError).toHaveBeenCalledWith({ kind: 'aborted', code: null, traceId: TRACE });
      const b = await run(make(new Error('socket reset')));
      expect(b.h.onError).toHaveBeenCalledWith({
        kind: 'stream_error',
        code: null,
        traceId: TRACE,
      });
    });

    it('a 200 with no body is a stream error', async () => {
      const response = new Response(null, { status: 200 });
      const { h } = await run(response);
      expect(h.onError).toHaveBeenCalledWith({ kind: 'stream_error', code: null, traceId: null });
    });

    it('an event that never ends is cut off as a stream error, not buffered for ever', async () => {
      const encoder = new TextEncoder();
      let sent = 0;
      const body = new ReadableStream<Uint8Array>({
        pull(controller) {
          sent += 1;
          controller.enqueue(encoder.encode('data: ' + 'x'.repeat(200_000)));
          if (sent > 30) controller.close();
        },
      });
      const { h } = await run(new Response(body, { status: 200 }));
      expect(h.onError).toHaveBeenCalledTimes(1);
      expect(h.onError.mock.calls[0][0].kind).toBe('stream_error');
      expect(sent).toBeLessThan(30); // it stopped reading well before the end
    });

    it('calls onError at most once and never after onDone', async () => {
      const { h } = await run(
        sseResponse([sseFrame('done', {}), sseFrame('error', { error_kind: 'timeout' })]),
      );
      expect(h.onDone).toHaveBeenCalledTimes(1);
      expect(h.onError).not.toHaveBeenCalled();
    });
  });
});
