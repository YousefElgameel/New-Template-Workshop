import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { POST as ask } from '@/app/api/v1/conversations/[id]/ask/route';
import { POST as askStream } from '@/app/api/v1/conversations/[id]/ask/stream/route';
import { POST as create } from '@/app/api/v1/conversations/route';
import { ASK_HOP_TIMEOUT_MS } from '@/lib/ask-bff';
import { askLimiter, __resetAskLimiterForTests } from '@/lib/ask-limits';
import { log } from '@/lib/logger';
import { sseFrame, sseStreamFromFrames } from '@/lib/sse';
import { TRACE_HEADER } from '@/lib/trace';
import {
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
const HOST = 'app.example.com';

const ctx = (id: string) => ({ params: Promise.resolve({ id }) });
const askPath = (id: string = ID) => `/api/v1/conversations/${id}/ask`;
const streamPath = (id: string = ID) => `${askPath(id)}/stream`;

function post(
  path: string,
  headers: Record<string, string> = {},
  body: object = { question: 'q' },
) {
  return new Request(`https://${HOST}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', host: HOST, ...headers },
    body: JSON.stringify(body),
  });
}

const forwarded = (address: string) => ({ 'x-forwarded-for': `1.1.1.1, ${address}` });

/** An api event stream the test ends when it chooses. */
function liveStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: () =>
      new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    push: (frame: string) => controller.enqueue(encoder.encode(frame)),
    end: () => controller.close(),
  };
}

const sseOf = (frames: string[]) =>
  new Response(sseStreamFromFrames(frames), {
    status: 200,
    headers: { 'content-type': 'text/event-stream' },
  });

let fetchSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  process.env.API_BASE_URL = API;
  delete process.env.MOCK_UPSTREAM;
  vi.stubEnv('ASK_MAX_INFLIGHT', '3');
  vi.stubEnv('ASK_MAX_INFLIGHT_PER_CLIENT', '2');
  __resetAskLimiterForTests();
  fetchSpy = vi.spyOn(globalThis, 'fetch');
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  vi.mocked(log).mockClear();
  delete process.env.MOCK_UPSTREAM;
  __resetAskLimiterForTests();
});

const inflight = () => askLimiter().snapshot().inflight;

async function expectError(res: Response, status: number, error: string) {
  expect(res.status).toBe(status);
  const body = await res.json();
  expect(body.error).toBe(error);
  expect(body.trace_id).toMatch(/^0eb0[0-9a-f]{28}$/);
  expect(res.headers.get(TRACE_HEADER)).toBe(body.trace_id);
}

describe('asks in flight — the stream route', () => {
  it('refuses a third ask from one client with 429 busy while two are streaming, then lets it in', async () => {
    const streams = [liveStream(), liveStream(), liveStream()];
    let n = 0;
    fetchSpy.mockImplementation(async () => streams[n++].response());
    const client = forwarded('10.0.0.1');

    const one = await askStream(post(streamPath(), client), ctx(ID));
    const two = await askStream(post(streamPath(), client), ctx(ID));
    expect(inflight()).toBe(2);

    const third = await askStream(
      post(streamPath(), { ...client, [TRACE_HEADER]: TRACE }),
      ctx(ID),
    );
    await expectError(third, 429, 'busy');
    expect(third.headers.get(TRACE_HEADER)).toBe(TRACE);
    expect(fetchSpy).toHaveBeenCalledTimes(2); // the refused ask never reached the api

    // One answer ends: the slot comes back.
    streams[0].end();
    await new Response(one.body).text();
    expect(inflight()).toBe(1);
    const again = await askStream(post(streamPath(), client), ctx(ID));
    expect(again.status).toBe(200);

    streams[1].end();
    streams[2].end();
    await new Response(two.body).text();
    await new Response(again.body).text();
    expect(inflight()).toBe(0);
  });

  it("another client is not affected by the first one's cap", async () => {
    const held = [liveStream(), liveStream(), liveStream()];
    let n = 0;
    fetchSpy.mockImplementation(async () => held[n++].response());
    await askStream(post(streamPath(), forwarded('10.0.0.1')), ctx(ID));
    await askStream(post(streamPath(), forwarded('10.0.0.1')), ctx(ID));
    const other = await askStream(post(streamPath(), forwarded('10.0.0.2')), ctx(ID));
    expect(other.status).toBe(200);
    held.forEach((h) => h.end());
  });

  it('refuses when the instance is full, whoever asks', async () => {
    const held = [liveStream(), liveStream(), liveStream()];
    let n = 0;
    fetchSpy.mockImplementation(async () => held[n++].response());
    for (const address of ['10.0.0.1', '10.0.0.2', '10.0.0.3']) {
      expect((await askStream(post(streamPath(), forwarded(address)), ctx(ID))).status).toBe(200);
    }
    const fourth = await askStream(post(streamPath(), forwarded('10.0.0.4')), ctx(ID));
    await expectError(fourth, 429, 'busy');
    held.forEach((h) => h.end());
  });

  it('keys the client on the rightmost address: a forged leftmost one buys nothing', async () => {
    const held = [liveStream(), liveStream()];
    let n = 0;
    fetchSpy.mockImplementation(async () => held[n++].response());
    const headers = (forged: string) => ({ 'x-forwarded-for': `${forged}, 10.0.0.1` });
    await askStream(post(streamPath(), headers('6.6.6.1')), ctx(ID));
    await askStream(post(streamPath(), headers('6.6.6.2')), ctx(ID));
    const third = await askStream(post(streamPath(), headers('6.6.6.3')), ctx(ID));
    await expectError(third, 429, 'busy');
    held.forEach((h) => h.end());
  });

  it('puts requests with no forwarded address in one shared bucket', async () => {
    const held = [liveStream(), liveStream()];
    let n = 0;
    fetchSpy.mockImplementation(async () => held[n++].response());
    await askStream(post(streamPath()), ctx(ID));
    await askStream(post(streamPath()), ctx(ID));
    await expectError(await askStream(post(streamPath()), ctx(ID)), 429, 'busy');
    held.forEach((h) => h.end());
  });

  it('frees the slot when the browser leaves mid-stream', async () => {
    const live = liveStream();
    fetchSpy.mockImplementation(async () => live.response());
    const res = await askStream(post(streamPath()), ctx(ID));
    expect(inflight()).toBe(1);
    const reader = res.body!.getReader();
    live.push(sseFrame('token', { text: 'x' }));
    await reader.read();
    await reader.cancel();
    expect(inflight()).toBe(0);
  });

  it('frees the slot when the api connection breaks mid-answer', async () => {
    let sent = false;
    const broken = new ReadableStream<Uint8Array>({
      pull(c) {
        if (sent) throw new Error('reset');
        sent = true;
        c.enqueue(new TextEncoder().encode(sseFrame('token', { text: 'half' })));
      },
    });
    fetchSpy.mockResolvedValue(
      new Response(broken, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    const res = await askStream(post(streamPath()), ctx(ID));
    await new Response(res.body).text();
    expect(inflight()).toBe(0);
  });

  it.each([
    ['the api is unreachable', () => fetchSpy.mockRejectedValue(new Error('ECONNREFUSED'))],
    [
      'the api refuses (429 busy)',
      () =>
        fetchSpy.mockResolvedValue(
          Response.json({ error: 'busy', trace_id: '0c70' + '0'.repeat(28) }, { status: 429 }),
        ),
    ],
    [
      'the reply is not an event stream',
      () => fetchSpy.mockResolvedValue(new Response('<html>', { status: 200 })),
    ],
  ])('frees the slot at once when %s', async (_name, arrange) => {
    arrange();
    await askStream(post(streamPath()), ctx(ID));
    expect(inflight()).toBe(0);
  });

  it('does not take a slot for a request that is invalid', async () => {
    for (const bad of [
      post(streamPath('not-a-uuid')),
      post(streamPath(), {}, { question: '   ' }),
      post(streamPath(), {}, { question: 'x'.repeat(4001) }),
      post(streamPath(), { 'content-type': 'text/plain' }),
    ]) {
      await askStream(bad, ctx(bad.url.includes('not-a-uuid') ? 'not-a-uuid' : ID));
    }
    expect(inflight()).toBe(0);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('does not slow a normal ask down', async () => {
    fetchSpy.mockImplementation(async () => sseOf(conversationAskStreamFrames));
    const res = await askStream(post(streamPath(), forwarded('10.0.0.1')), ctx(ID));
    expect(res.status).toBe(200);
    await expect(new Response(res.body).text()).resolves.toBe(conversationAskStreamFrames.join(''));
    expect(inflight()).toBe(0);
  });
});

describe('the order of the checks, with the client already at its cap', () => {
  async function saturate() {
    const held = [liveStream(), liveStream()];
    let n = 0;
    fetchSpy.mockImplementation(async () => held[n++].response());
    const client = forwarded('10.0.0.1');
    await askStream(post(streamPath(), client), ctx(ID));
    await askStream(post(streamPath(), client), ctx(ID));
    return { client, held };
  }

  it('a foreign origin is 403, not 429', async () => {
    const { client, held } = await saturate();
    const res = await askStream(
      post(streamPath(), { ...client, origin: 'https://evil.example' }),
      ctx(ID),
    );
    await expectError(res, 403, 'invalid_origin');
    held.forEach((h) => h.end());
  });

  it('an invalid request is 400/413/415, not 429', async () => {
    const { client, held } = await saturate();
    await expectError(
      await askStream(post(streamPath('nope'), client), ctx('nope')),
      400,
      'invalid_request',
    );
    await expectError(
      await askStream(post(streamPath(), client, { question: ' ' }), ctx(ID)),
      400,
      'invalid_request',
    );
    await expectError(
      await askStream(post(streamPath(), client, { question: 'x'.repeat(4001) }), ctx(ID)),
      413,
      'payload_too_large',
    );
    await expectError(
      await askStream(post(streamPath(), { ...client, 'content-type': 'text/plain' }), ctx(ID)),
      415,
      'unsupported_media_type',
    );
    await expectError(await askStream(post(streamPath(), client), ctx(ID)), 429, 'busy'); // and a valid one is
    held.forEach((h) => h.end());
  });

  it('the JSON route checks in the same order', async () => {
    const { client, held } = await saturate();
    await expectError(
      await ask(post(askPath(), { ...client, origin: 'https://evil.example' }), ctx(ID)),
      403,
      'invalid_origin',
    );
    await expectError(
      await ask(post(askPath(), client, { question: '' }), ctx(ID)),
      400,
      'invalid_request',
    );
    held.forEach((h) => h.end());
  });
});

describe('slots, to the end', () => {
  it('the browser leaving cancels the api stream and gives the slot back (the source is cancelled, not drained)', async () => {
    const cancelled = vi.fn();
    const source = new ReadableStream<Uint8Array>({
      pull(c) {
        c.enqueue(new TextEncoder().encode(sseFrame('token', { text: 'x' })));
      },
      cancel: cancelled,
    });
    fetchSpy.mockResolvedValue(
      new Response(source, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    const res = await askStream(post(streamPath()), ctx(ID));
    const reader = res.body!.getReader();
    await reader.read();
    expect(inflight()).toBe(1);
    await reader.cancel();
    expect(cancelled).toHaveBeenCalled();
    expect(inflight()).toBe(0);
  });

  it('a reader that stops reading cannot hold the slot for ever', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      const source = new ReadableStream<Uint8Array>({
        pull(c) {
          c.enqueue(new TextEncoder().encode(sseFrame('token', { text: 'x' })));
        },
      });
      fetchSpy.mockResolvedValue(
        new Response(source, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
      );
      const res = await askStream(post(streamPath()), ctx(ID));
      await res.body!.getReader().read(); // one read, then silence
      expect(inflight()).toBe(1);
      await vi.advanceTimersByTimeAsync(ASK_HOP_TIMEOUT_MS + 6_000);
      expect(inflight()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('building the response failing does not leak the slot', async () => {
    // A 204 may not carry a body: constructing the Response throws.
    fetchSpy.mockResolvedValue(
      new Response(sseStreamFromFrames(['event: a\ndata: 1\n\n']), {
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
      }),
    );
    const real = globalThis.Response;
    let calls = 0;
    class Throwing extends real {
      constructor(body?: BodyInit | null, init?: ResponseInit) {
        calls += 1;
        // the first Response built by the route after the upstream call: the wrapper
        if (calls === 1 && body instanceof ReadableStream) throw new TypeError('bad init');
        super(body, init);
      }
    }
    vi.stubGlobal('Response', Throwing);
    try {
      await askStream(post(streamPath()), ctx(ID)).catch(() => undefined);
    } finally {
      vi.stubGlobal('Response', real);
    }
    expect(inflight()).toBe(0);
  });
});

describe('asks in flight — the JSON route', () => {
  it('refuses while the client already has two answers pending, and frees the slot when they finish', async () => {
    const gates: ((r: Response) => void)[] = [];
    fetchSpy.mockImplementation(() => new Promise<Response>((resolve) => gates.push(resolve)));
    const client = forwarded('10.0.0.1');
    const a = ask(post(askPath(), client), ctx(ID));
    const b = ask(post(askPath(), client), ctx(ID));
    await vi.waitFor(() => expect(gates).toHaveLength(2));
    expect(inflight()).toBe(2);

    await expectError(await ask(post(askPath(), client), ctx(ID)), 429, 'busy');
    expect(fetchSpy).toHaveBeenCalledTimes(2);

    gates.forEach((resolve) => resolve(Response.json(conversationAskResponse)));
    expect((await a).status).toBe(200);
    expect((await b).status).toBe(200);
    expect(inflight()).toBe(0);
    fetchSpy.mockImplementation(async () => Response.json(conversationAskResponse));
    expect((await ask(post(askPath(), client), ctx(ID))).status).toBe(200);
  });

  it('frees the slot when the api cannot be reached', async () => {
    fetchSpy.mockRejectedValue(new Error('ECONNREFUSED'));
    expect((await ask(post(askPath()), ctx(ID))).status).toBe(502);
    expect(inflight()).toBe(0);
  });

  it('frees the slot when the answer is a refusal from the api', async () => {
    fetchSpy.mockResolvedValue(
      Response.json(
        { error: 'ai_unavailable', trace_id: '0c70' + '0'.repeat(28) },
        { status: 503 },
      ),
    );
    expect((await ask(post(askPath()), ctx(ID))).status).toBe(503);
    expect(inflight()).toBe(0);
  });

  it('does not take a slot for an invalid request', async () => {
    await ask(post(askPath(), {}, { question: '' }), ctx(ID));
    await ask(post(askPath('x')), ctx('x'));
    expect(inflight()).toBe(0);
  });
});

describe('in mock mode', () => {
  beforeEach(() => {
    process.env.MOCK_UPSTREAM = 'true';
  });

  it('applies the cap to the mocked stream too, and frees the slot when it has been read', async () => {
    const client = forwarded('10.0.0.1');
    const one = await askStream(post(streamPath(), client), ctx(ID));
    const two = await askStream(post(streamPath(), client), ctx(ID));
    expect(inflight()).toBe(2);
    await expectError(await askStream(post(streamPath(), client), ctx(ID)), 429, 'busy');
    await new Response(one.body).text();
    await new Response(two.body).text();
    expect(inflight()).toBe(0);
    expect(one.headers.get('x-mock-upstream')).toBe('true');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('a reader that never reads the mocked stream cannot hold the slot for ever', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] });
    try {
      await askStream(post(streamPath()), ctx(ID)); // the body is never read
      expect(inflight()).toBe(1);
      await vi.advanceTimersByTimeAsync(ASK_HOP_TIMEOUT_MS + 6_000);
      expect(inflight()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('frees the slot for the mocked JSON answer and for a mocked 404', async () => {
    expect((await ask(post(askPath()), ctx(ID))).status).toBe(200);
    const unknown = 'c0000000-0000-4000-8000-0000000000ff';
    expect((await ask(post(askPath(unknown)), ctx(unknown))).status).toBe(404);
    expect((await askStream(post(streamPath(unknown)), ctx(unknown))).status).toBe(404);
    expect(inflight()).toBe(0);
  });
});

describe('the same-origin check on the write routes', () => {
  const routes = [
    ['create', (h: Record<string, string>) => create(post('/api/v1/conversations', h, {}))],
    ['ask', (h: Record<string, string>) => ask(post(askPath(), h), ctx(ID))],
    ['ask/stream', (h: Record<string, string>) => askStream(post(streamPath(), h), ctx(ID))],
  ] as const;

  describe.each(routes)('%s', (_name, call) => {
    it.each([
      ['another site', { origin: 'https://evil.example' }],
      ['the null origin', { origin: 'null' }],
      ['a look-alike host', { origin: `https://${HOST}.evil.example` }],
      ['a browser that says cross-site', { 'sec-fetch-site': 'cross-site' }],
      [
        'a matching origin that the browser still calls cross-site',
        { origin: `https://${HOST}`, 'sec-fetch-site': 'cross-site' },
      ],
    ])('refuses %s with 403 invalid_origin and calls nothing', async (_why, headers) => {
      await expectError(await call(headers), 403, 'invalid_origin');
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(inflight()).toBe(0);
    });

    it("lets the app's own origin, a same-origin fetch and a request with no headers through", async () => {
      process.env.MOCK_UPSTREAM = 'true';
      const allowed: Record<string, string>[] = [
        { origin: `https://${HOST}`, 'sec-fetch-site': 'same-origin' },
        { origin: `http://${HOST}` },
        { 'sec-fetch-site': 'none' },
        {},
      ];
      for (const headers of allowed) {
        const res = await call(headers);
        expect([200, 201]).toContain(res.status);
        await res.text();
      }
    });
  });

  it('checks the origin before the body is read or validated', async () => {
    const res = await ask(
      new Request(`https://${HOST}${askPath()}`, {
        method: 'POST',
        headers: { origin: 'https://evil.example', host: HOST },
        body: 'not json at all',
      }),
      ctx(ID),
    );
    await expectError(res, 403, 'invalid_origin');
  });
});

describe('logs', () => {
  it('say which cap was reached and never an address', async () => {
    const held = [liveStream(), liveStream()];
    let n = 0;
    fetchSpy.mockImplementation(async () => held[n++].response());
    const client = forwarded('203.0.113.77');
    await askStream(post(streamPath(), client), ctx(ID));
    await askStream(post(streamPath(), client), ctx(ID));
    await askStream(post(streamPath(), client), ctx(ID));
    await create(post('/api/v1/conversations', { origin: 'https://evil.example' }, {}));

    const rendered = JSON.stringify(vi.mocked(log).mock.calls);
    expect(rendered).toContain('"why":"client"');
    expect(rendered).toContain('"status":403');
    expect(rendered).not.toContain('203.0.113.77');
    expect(rendered).not.toContain('evil.example');
    held.forEach((h) => h.end());
  });
});
