import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { POST as ask } from '@/app/api/v1/conversations/[id]/ask/route';
import { POST as askStream } from '@/app/api/v1/conversations/[id]/ask/stream/route';
import {
  ASK_HOP_TIMEOUT_MS,
  MAX_ASK_BODY_BYTES,
  QUESTION_MAX_CHARS,
  guardStream,
  parseAskBody,
} from '@/lib/ask-bff';
import { log } from '@/lib/logger';
import { MOCK_HEADER } from '@/lib/mocks';
import { readSseStream, sseFrame, sseStreamFromFrames } from '@/lib/sse';
import { TRACE_HEADER } from '@/lib/trace';
import {
  conversationAskFailureFrames,
  conversationAskResponse,
  conversationAskStreamFrames,
  conversationIds,
} from '@/mocks/conversations';

vi.mock('@/lib/logger', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/logger')>()),
  log: vi.fn(),
}));

const API = 'http://api:8000';
const ID = conversationIds.tracing;
const TRACE = `0eb00${'c'.repeat(27)}`;
const SECRET_QUESTION = 'SECRET-QUESTION-TEXT';

function post(
  path: string,
  body?: string | object,
  headers: Record<string, string> = { 'content-type': 'application/json' },
): Request {
  return new Request(`http://web${path}`, {
    method: 'POST',
    headers,
    body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body),
  });
}

const ctx = (id: string) => ({ params: Promise.resolve({ id }) });
const askPath = (id: string = ID) => `/api/v1/conversations/${id}/ask`;
const streamPath = (id: string = ID) => `${askPath(id)}/stream`;
const question = (q = SECRET_QUESTION) => ({ question: q });

let fetchSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  process.env.API_BASE_URL = API;
  delete process.env.MOCK_UPSTREAM;
  fetchSpy = vi.spyOn(globalThis, 'fetch');
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.mocked(log).mockClear();
  delete process.env.MOCK_UPSTREAM;
});

async function expectError(res: Response, status: number, error: string): Promise<void> {
  expect(res.status).toBe(status);
  const body = await res.json();
  expect(body.error).toBe(error);
  expect(body.trace_id).toMatch(/^0eb0[0-9a-f]{28}$/);
}

const sseResponse = (frames: string[], extra: Record<string, string> = {}) =>
  new Response(sseStreamFromFrames(frames), {
    status: 200,
    headers: { 'content-type': 'text/event-stream', ...extra },
  });

async function readAll(res: Response): Promise<string> {
  return new Response(res.body).text();
}

describe('what both ask routes check before they call the api', () => {
  const routes = [
    ['ask', ask, askPath],
    ['ask/stream', askStream, streamPath],
  ] as const;

  describe.each(routes)('POST …/%s', (_name, handler, path) => {
    it.each(['not-a-uuid', '1234', '..%2F..%2Fadmin', 'c0000000-0000-4000-8000-00000000000g'])(
      'refuses the id %j with 400 and makes no upstream call',
      async (id) => {
        await expectError(
          await handler(post(path(id), question()), ctx(id)),
          400,
          'invalid_request',
        );
        expect(fetchSpy).not.toHaveBeenCalled();
      },
    );

    it.each([
      ['an empty body', ''],
      ['not JSON', '{nope'],
      ['an array', '[]'],
      ['null', 'null'],
      ['a missing question', '{}'],
      ['an empty question', '{"question":""}'],
      ['a blank question', '{"question":"   \\n\\t "}'],
      ['a non-string question', '{"question":42}'],
      ['an unexpected key', '{"question":"hi","conversation_id":"x"}'],
      ['a NUL character', '{"question":"a\\u0000b"}'],
      ['a lone surrogate', '{"question":"a\\ud800b"}'],
    ])('answers 400 for %s and makes no upstream call', async (_why, body) => {
      await expectError(await handler(post(path(), body), ctx(ID)), 400, 'invalid_request');
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('answers 413 for a question over 4,000 characters, and accepts exactly 4,000', async () => {
      await expectError(
        await handler(post(path(), question('x'.repeat(QUESTION_MAX_CHARS + 1))), ctx(ID)),
        413,
        'payload_too_large',
      );
      expect(fetchSpy).not.toHaveBeenCalled();

      fetchSpy.mockImplementation(async () =>
        handler === ask ? Response.json(conversationAskResponse) : sseResponse([]),
      );
      const ok = await handler(post(path(), question('x'.repeat(QUESTION_MAX_CHARS))), ctx(ID));
      expect(ok.status).toBe(200);
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });

    it('counts characters, not UTF-16 units: 4,000 emoji pass, 4,001 do not', async () => {
      fetchSpy.mockImplementation(async () =>
        handler === ask ? Response.json(conversationAskResponse) : sseResponse([]),
      );
      const emoji = String.fromCodePoint(0x1f600);
      const ok = await handler(post(path(), question(emoji.repeat(QUESTION_MAX_CHARS))), ctx(ID));
      expect(ok.status).toBe(200);
      const tooMany = await handler(
        post(path(), question(emoji.repeat(QUESTION_MAX_CHARS + 1))),
        ctx(ID),
      );
      expect(tooMany.status).toBe(413);
    });

    it('answers 413 for a body over the cap before reading it', async () => {
      const big = JSON.stringify(question('x'.repeat(MAX_ASK_BODY_BYTES)));
      await expectError(await handler(post(path(), big), ctx(ID)), 413, 'payload_too_large');
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it.each([
      ['text/plain', { 'content-type': 'text/plain' }],
      ['a form post', { 'content-type': 'application/x-www-form-urlencoded' }],
      ['no content type', {}],
    ])('answers 415 for %s (a cross-site simple request)', async (_why, headers) => {
      const req = new Request(`http://web${path()}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(question()),
      });
      // A string body gets text/plain by default; "no content type" needs it removed.
      if (Object.keys(headers).length === 0) req.headers.delete('content-type');
      await expectError(await handler(req, ctx(ID)), 415, 'unsupported_media_type');
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('accepts application/json with parameters', async () => {
      fetchSpy.mockImplementation(async () =>
        handler === ask ? Response.json(conversationAskResponse) : sseResponse([]),
      );
      const res = await handler(
        post(path(), question(), { 'content-type': 'application/json; charset=utf-8' }),
        ctx(ID),
      );
      expect(res.status).toBe(200);
    });
  });
});

describe('POST /api/v1/conversations/[id]/ask', () => {
  it('forwards exactly { question } (trimmed) to the api with the same trace id, and returns the answer', async () => {
    fetchSpy.mockResolvedValue(Response.json(conversationAskResponse));
    const res = await ask(
      post(
        askPath(),
        { question: '  What port?  ' },
        {
          'content-type': 'application/json',
          [TRACE_HEADER]: TRACE,
        },
      ),
      ctx(ID),
    );

    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual(conversationAskResponse);
    expect(res.headers.get(TRACE_HEADER)).toBe(TRACE);
    expect(res.headers.get(MOCK_HEADER)).toBeNull();
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe(`${API}/v1/conversations/${ID}/ask`);
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({ question: 'What port?' });
    expect(new Headers(init?.headers).get(TRACE_HEADER)).toBe(TRACE);
    expect(new Headers(init?.headers).get('content-type')).toBe('application/json');
  });

  it('sets its own hop timeout, far above fetchUpstream default and above the api worst case', async () => {
    expect(ASK_HOP_TIMEOUT_MS).toBeGreaterThan(4 * 60_000);
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    fetchSpy.mockResolvedValue(Response.json(conversationAskResponse));
    await ask(post(askPath(), question()), ctx(ID));
    expect(timeout).toHaveBeenCalledWith(ASK_HOP_TIMEOUT_MS);
    expect(timeout).not.toHaveBeenCalledWith(10_000);
  });

  it.each([
    [404, 'not_found'],
    [413, 'payload_too_large'],
    [429, 'busy'],
    [503, 'ai_unavailable'],
    [422, 'validation_error'],
  ])(
    'does not rewrite the api %i %s: status, body and trace id reach the browser',
    async (status, error) => {
      const body = { error, trace_id: `0c700${'d'.repeat(27)}` };
      fetchSpy.mockResolvedValue(Response.json(body, { status }));
      const res = await ask(post(askPath(), question()), ctx(ID));
      expect(res.status).toBe(status);
      await expect(res.json()).resolves.toEqual(body);
    },
  );

  it('answers 502 upstream_unreachable when the api is down, and 502 for a framework error body', async () => {
    fetchSpy.mockRejectedValue(new Error('ECONNREFUSED'));
    const down = await ask(post(askPath(), question()), ctx(ID));
    await expectError(down, 502, 'upstream_unreachable');

    fetchSpy.mockResolvedValue(Response.json({ detail: 'Internal Server Error' }, { status: 500 }));
    await expectError(await ask(post(askPath(), question()), ctx(ID)), 502, 'upstream_error');
  });

  it('serves the answer fixture in mock mode, marked, without calling the api', async () => {
    process.env.MOCK_UPSTREAM = 'true';
    const res = await ask(post(askPath(), question()), ctx(ID));
    expect(res.status).toBe(200);
    expect(res.headers.get(MOCK_HEADER)).toBe('true');
    await expect(res.json()).resolves.toEqual(conversationAskResponse);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('answers 404 in mock mode for a conversation that has no fixture, and still validates', async () => {
    process.env.MOCK_UPSTREAM = 'true';
    const unknown = 'c0000000-0000-4000-8000-0000000000ff';
    await expectError(
      await ask(post(askPath(unknown), question()), ctx(unknown)),
      404,
      'not_found',
    );
    await expectError(await ask(post(askPath(), '{}'), ctx(ID)), 400, 'invalid_request');
  });
});

describe('POST /api/v1/conversations/[id]/ask/stream', () => {
  it('passes the api frames through unchanged with the trace id, and asks for an event stream', async () => {
    fetchSpy.mockResolvedValue(sseResponse(conversationAskStreamFrames));
    const res = await askStream(
      post(streamPath(), question(), {
        'content-type': 'application/json',
        [TRACE_HEADER]: TRACE,
      }),
      ctx(ID),
    );

    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('text/event-stream');
    expect(res.headers.get('cache-control')).toContain('no-cache');
    expect(res.headers.get(TRACE_HEADER)).toBe(TRACE);
    expect(res.headers.get(MOCK_HEADER)).toBeNull();
    await expect(readAll(res)).resolves.toBe(conversationAskStreamFrames.join(''));

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe(`${API}/v1/conversations/${ID}/ask/stream`);
    expect(init?.method).toBe('POST');
    expect(JSON.parse(String(init?.body))).toEqual({ question: SECRET_QUESTION });
    const headers = new Headers(init?.headers);
    expect(headers.get(TRACE_HEADER)).toBe(TRACE);
    expect(headers.get('accept')).toBe('text/event-stream');
  });

  it('does not cut the answer at the 10 s default: the hop timeout is explicit and above the api worst case', async () => {
    const timeout = vi.spyOn(AbortSignal, 'timeout');
    fetchSpy.mockResolvedValue(sseResponse([]));
    await readAll(await askStream(post(streamPath(), question()), ctx(ID)));
    expect(timeout).toHaveBeenCalledWith(ASK_HOP_TIMEOUT_MS);
    expect(timeout).not.toHaveBeenCalledWith(10_000);
  });

  it('forwards an error frame the api sends mid-stream as it is', async () => {
    fetchSpy.mockResolvedValue(sseResponse(conversationAskFailureFrames));
    const res = await askStream(post(streamPath(), question()), ctx(ID));
    await expect(readAll(res)).resolves.toBe(conversationAskFailureFrames.join(''));
  });

  it.each([
    [404, 'not_found'],
    [413, 'payload_too_large'],
    [429, 'busy'],
    [422, 'validation_error'],
    [503, 'ai_unavailable'],
  ])(
    'does not rewrite an api %i %s sent before the stream: JSON with its body and trace id',
    async (status, error) => {
      const body = { error, trace_id: `0c700${'d'.repeat(27)}` };
      fetchSpy.mockResolvedValue(Response.json(body, { status }));
      const res = await askStream(post(streamPath(), question()), ctx(ID));
      expect(res.status).toBe(status);
      expect(res.headers.get('content-type')).toContain('application/json');
      await expect(res.json()).resolves.toEqual(body);
    },
  );

  it('answers 502 upstream_unreachable when the api cannot be reached', async () => {
    fetchSpy.mockRejectedValue(new Error('ECONNREFUSED'));
    const res = await askStream(
      post(streamPath(), question(), {
        'content-type': 'application/json',
        [TRACE_HEADER]: TRACE,
      }),
      ctx(ID),
    );
    expect(res.status).toBe(502);
    await expect(res.json()).resolves.toEqual({
      error: 'upstream_unreachable',
      target: 'api',
      trace_id: TRACE,
    });
  });

  it('answers 502 when a 200 is not an event stream, and gives the body back', async () => {
    const cancelled = vi.fn();
    const body = new ReadableStream<Uint8Array>({ start() {}, cancel: cancelled });
    fetchSpy.mockResolvedValue(
      new Response(body, { status: 200, headers: { 'content-type': 'text/html' } }),
    );
    await expectError(
      await askStream(post(streamPath(), question()), ctx(ID)),
      502,
      'upstream_error',
    );
    expect(cancelled).toHaveBeenCalled();
  });

  it('answers 502 for an error status whose body is not the api error shape', async () => {
    fetchSpy.mockResolvedValue(new Response('<html>bad gateway</html>', { status: 502 }));
    await expectError(
      await askStream(post(streamPath(), question()), ctx(ID)),
      502,
      'upstream_error',
    );
  });

  it('ends a stream whose connection to the api breaks with one terminal error frame and the trace id', async () => {
    const encoder = new TextEncoder();
    let sent = false;
    const broken = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (sent) throw new Error('socket hang up with SECRET-DRIVER-TEXT');
        sent = true;
        controller.enqueue(encoder.encode(sseFrame('token', { text: 'half' })));
      },
    });
    fetchSpy.mockResolvedValue(
      new Response(broken, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    const res = await askStream(
      post(streamPath(), question(), { 'content-type': 'application/json', [TRACE_HEADER]: TRACE }),
      ctx(ID),
    );
    const text = await readAll(res);
    expect(text.startsWith(sseFrame('token', { text: 'half' }))).toBe(true);
    const last = text.slice(sseFrame('token', { text: 'half' }).length);
    expect(last).toBe(
      sseFrame('error', { error_kind: 'network', error: 'upstream_error', trace_id: TRACE }),
    );
    expect(text).not.toContain('SECRET-DRIVER-TEXT');
  });

  it('cancels the api stream when the browser leaves, so the api frees its slot', async () => {
    const cancelled = vi.fn();
    const upstream = new ReadableStream<Uint8Array>({
      pull(controller) {
        controller.enqueue(new TextEncoder().encode(sseFrame('token', { text: 'x' })));
      },
      cancel: cancelled,
    });
    fetchSpy.mockResolvedValue(
      new Response(upstream, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    const res = await askStream(post(streamPath(), question()), ctx(ID));
    const reader = res.body!.getReader();
    await reader.read();
    await reader.cancel();
    expect(cancelled).toHaveBeenCalled();
  });

  describe('in mock mode', () => {
    beforeEach(() => {
      process.env.MOCK_UPSTREAM = 'true';
    });

    it('serves the stream fixture, marked, without calling the api', async () => {
      const res = await askStream(post(streamPath(), question()), ctx(ID));
      expect(res.status).toBe(200);
      expect(res.headers.get(MOCK_HEADER)).toBe('true');
      expect(res.headers.get('content-type')).toContain('text/event-stream');
      const events = [];
      for await (const e of readSseStream(res.body!)) events.push(e.event);
      expect(events[0]).toBe('sources');
      expect(events.at(-1)).toBe('done');
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('serves the failure stream with ?mock=failure', async () => {
      const res = await askStream(post(`${streamPath()}?mock=failure`, question()), ctx(ID));
      const events = [];
      for await (const e of readSseStream(res.body!)) events.push(e.event);
      expect(events.at(-1)).toBe('error');
      expect(events).not.toContain('done');
    });

    it('ignores ?mock=failure outside mock mode', async () => {
      delete process.env.MOCK_UPSTREAM;
      fetchSpy.mockResolvedValue(sseResponse(conversationAskStreamFrames));
      const res = await askStream(post(`${streamPath()}?mock=failure`, question()), ctx(ID));
      await expect(readAll(res)).resolves.toBe(conversationAskStreamFrames.join(''));
    });

    it('answers 404 for a conversation with no fixture, and still validates', async () => {
      const unknown = 'c0000000-0000-4000-8000-0000000000ff';
      await expectError(
        await askStream(post(streamPath(unknown), question()), ctx(unknown)),
        404,
        'not_found',
      );
      await expectError(await askStream(post(streamPath(), '{}'), ctx(ID)), 400, 'invalid_request');
    });
  });
});

describe('logs and the trace id', () => {
  it('replaces an invalid inbound trace id with one minted by the web origin', async () => {
    fetchSpy.mockResolvedValue(Response.json(conversationAskResponse));
    const res = await ask(
      post(askPath(), question(), { 'content-type': 'application/json', [TRACE_HEADER]: 'bad' }),
      ctx(ID),
    );
    expect(res.headers.get(TRACE_HEADER)).toMatch(/^0eb0[0-9a-f]{28}$/);
  });

  it('logs route patterns and statuses only — never the id, the question, a token or the URL', async () => {
    fetchSpy.mockResolvedValueOnce(Response.json(conversationAskResponse));
    await ask(post(askPath(), question()), ctx(ID));
    fetchSpy.mockResolvedValueOnce(sseResponse(conversationAskStreamFrames));
    await readAll(await askStream(post(streamPath(), question()), ctx(ID)));
    fetchSpy.mockRejectedValueOnce(
      new Error(`connect ECONNREFUSED ${API}/v1/conversations/${ID}/ask`),
    );
    await ask(post(askPath(), question()), ctx(ID));
    await ask(post(askPath('nope'), question()), ctx('nope'));
    await askStream(post(streamPath(), `{"question":"${SECRET_QUESTION}","x":1}`), ctx(ID));

    const logged = JSON.stringify(vi.mocked(log).mock.calls);
    expect(logged).toContain('/api/v1/conversations/[id]/ask');
    expect(logged).not.toContain(ID);
    expect(logged).not.toContain(SECRET_QUESTION);
    expect(logged).not.toContain('The api listens');
    expect(logged).not.toContain('api:8000');
    expect(logged).not.toContain('ECONNREFUSED');
  });
});

describe('the browser leaving, and what an api error may carry', () => {
  it.each([
    ['ask', ask, askPath],
    ['ask/stream', askStream, streamPath],
  ] as const)('%s: aborts the api call when the browser goes away', async (_n, handler, path) => {
    let seen: AbortSignal | undefined;
    fetchSpy.mockImplementation(async (_url: unknown, init?: RequestInit) => {
      seen = init?.signal ?? undefined;
      return handler === ask ? Response.json(conversationAskResponse) : sseResponse([]);
    });
    const browser = new AbortController();
    const req = new Request(`http://web${path()}`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(question()),
      signal: browser.signal,
    });
    await readAll(await handler(req, ctx(ID)));
    expect(seen?.aborted).toBe(false);
    browser.abort();
    expect(seen?.aborted).toBe(true);
  });

  it('relays only error and trace_id of an api error, whatever else its body holds', async () => {
    const body = {
      error: 'validation_error',
      trace_id: `0c700${'d'.repeat(27)}`,
      detail: 'SECRET-DETAIL',
      input: SECRET_QUESTION,
    };
    fetchSpy.mockResolvedValue(Response.json(body, { status: 422 }));
    const res = await ask(post(askPath(), question()), ctx(ID));
    expect(res.status).toBe(422);
    await expect(res.json()).resolves.toEqual({ error: body.error, trace_id: body.trace_id });
  });

  it('does not glue the error frame onto a frame the api died inside of', async () => {
    const encoder = new TextEncoder();
    let sent = false;
    const broken = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (sent) throw new Error('reset');
        sent = true;
        controller.enqueue(
          encoder.encode('event: token' + String.fromCharCode(10) + 'data: {"text":"hal'),
        );
      },
    });
    const events = [];
    for await (const e of readSseStream(guardStream(broken, TRACE, '/r'))) events.push(e);
    expect(events.at(-1)?.event).toBe('error');
    expect(JSON.parse(events.at(-1)!.data).trace_id).toBe(TRACE);
  });
});

describe('what reaches the api call, and the headers on a relayed error', () => {
  it.each([
    ['ask', ask, askPath],
    ['ask/stream', askStream, streamPath],
  ] as const)('%s: hands fetch a live deadline signal of its own', async (_n, handler, path) => {
    fetchSpy.mockImplementation(async () =>
      handler === ask ? Response.json(conversationAskResponse) : sseResponse([]),
    );
    await readAll(await handler(post(path(), question()), ctx(ID)));
    const signal = (fetchSpy.mock.calls[0][1] as RequestInit).signal;
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal?.aborted).toBe(false);
  });

  it('echoes x-trace-id on a relayed api error, as on every response', async () => {
    fetchSpy.mockResolvedValue(
      Response.json({ error: 'busy', trace_id: `0c700${'d'.repeat(27)}` }, { status: 429 }),
    );
    const res = await askStream(
      post(streamPath(), question(), { 'content-type': 'application/json', [TRACE_HEADER]: TRACE }),
      ctx(ID),
    );
    expect(res.status).toBe(429);
    expect(res.headers.get(TRACE_HEADER)).toBe(TRACE);
  });

  it('guardStream reports a broken upstream as kind "network" and cancels nothing twice', async () => {
    const broken = new ReadableStream<Uint8Array>({
      pull() {
        throw new Error('reset');
      },
    });
    const events = [];
    for await (const e of readSseStream(guardStream(broken, TRACE, '/r'))) events.push(e);
    expect(events).toHaveLength(1);
    expect(JSON.parse(events[0].data).error_kind).toBe('network');
  });
});

describe('parseAskBody', () => {
  it('trims the question and keeps only it', () => {
    expect(parseAskBody('{"question":"  hi  "}')).toEqual({ ok: true, question: 'hi' });
  });

  it('keeps inner whitespace and newlines', () => {
    expect(parseAskBody('{"question":"a\\n\\nb"}')).toEqual({ ok: true, question: 'a\n\nb' });
  });

  it('tells "too long" apart from "invalid"', () => {
    expect(parseAskBody(JSON.stringify(question('x'.repeat(4001))))).toEqual({
      ok: false,
      reason: 'too_long',
    });
    expect(parseAskBody('{}')).toEqual({ ok: false, reason: 'invalid' });
  });
});

describe('guardStream', () => {
  const bytes = (s: string) => new TextEncoder().encode(s);
  const streamOf = (...chunks: string[]) =>
    new ReadableStream<Uint8Array>({
      start(c) {
        for (const chunk of chunks) c.enqueue(bytes(chunk));
        c.close();
      },
    });

  it('passes bytes through untouched', async () => {
    const out = guardStream(streamOf('event: a\n', 'data: 1\n\n'), TRACE, '/r');
    await expect(new Response(out).text()).resolves.toBe('event: a\ndata: 1\n\n');
  });

  it('ends with a too_long error frame (and cancels upstream) once more than the cap went through', async () => {
    const cancelled = vi.fn();
    const upstream = new ReadableStream<Uint8Array>({
      pull(c) {
        c.enqueue(bytes('x'.repeat(100)));
      },
      cancel: cancelled,
    });
    const text = await new Response(guardStream(upstream, TRACE, '/r', 250)).text();
    expect(text.startsWith('x'.repeat(200))).toBe(true);
    expect(text).toContain(
      sseFrame('error', { error_kind: 'too_long', error: 'upstream_error', trace_id: TRACE }),
    );
    expect(text).not.toContain('x'.repeat(300));
    expect(cancelled).toHaveBeenCalled();
  });
});
