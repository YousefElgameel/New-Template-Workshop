import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GET as getOne } from '@/app/api/v1/conversations/[id]/route';
import { GET as list, POST as create } from '@/app/api/v1/conversations/route';
import { MAX_CREATE_BODY_BYTES } from '@/lib/conversations-bff';
import { log } from '@/lib/logger';
import { recordServerDuration } from '@/lib/metrics';
import { MOCK_HEADER } from '@/lib/mocks';
import { TRACE_HEADER } from '@/lib/trace';
import {
  conversationIds,
  conversationListFixture,
  createdConversationFixture,
  findConversationFixture,
} from '@/mocks/conversations';

// Capture what the routes log and the metric labels, without hiding the real behaviour.
vi.mock('@/lib/logger', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/logger')>()),
  log: vi.fn(),
}));
vi.mock('@/lib/metrics', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/metrics')>()),
  recordServerDuration: vi.fn(),
}));

const API = 'http://api:8000';
const ID = conversationIds.tracing;
const TRACE = `0eb00${'c'.repeat(27)}`;
const SECRET_TITLE = 'SECRET-TITLE-TEXT';

function get(path: string, headers: Record<string, string> = {}): Request {
  return new Request(`http://web${path}`, { headers });
}

function post(path: string, body?: string | object, headers: Record<string, string> = {}): Request {
  return new Request(`http://web${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body),
  });
}

const ctx = (id: string) => ({ params: Promise.resolve({ id }) });

/** A POST whose body is a stream with no declared length (as a chunked upload would be). */
function chunkedPost(bytes: number): Request {
  const chunk = new TextEncoder().encode(' '.repeat(1024));
  let sent = 0;
  return new Request('http://web/api/v1/conversations', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: new ReadableStream<Uint8Array>({
      pull(controller) {
        if (sent >= bytes) return controller.close();
        controller.enqueue(chunk);
        sent += chunk.byteLength;
      },
    }),
    duplex: 'half',
  } as RequestInit);
}

let fetchSpy: ReturnType<typeof vi.spyOn>;
const upstreamJson = (body: unknown, status = 200) =>
  fetchSpy.mockResolvedValue(Response.json(body, { status }));

beforeEach(() => {
  process.env.API_BASE_URL = API;
  delete process.env.MOCK_UPSTREAM;
  fetchSpy = vi.spyOn(globalThis, 'fetch');
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.mocked(log).mockClear();
  vi.mocked(recordServerDuration).mockClear();
  delete process.env.MOCK_UPSTREAM;
});

async function expectError(res: Response, status: number, error: string): Promise<void> {
  expect(res.status).toBe(status);
  const body = await res.json();
  expect(body.error).toBe(error);
  expect(body.trace_id).toMatch(/^0eb0[0-9a-f]{28}$/);
}

// --- GET /api/v1/conversations ----------------------------------------------------------------

describe('GET /api/v1/conversations', () => {
  it('proxies to api GET /v1/conversations with the same trace id and returns its body', async () => {
    upstreamJson(conversationListFixture);
    const res = await list(get('/api/v1/conversations', { [TRACE_HEADER]: TRACE }));

    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual(conversationListFixture);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe(`${API}/v1/conversations`);
    expect(init?.method ?? 'GET').toBe('GET');
    expect(new Headers(init?.headers).get(TRACE_HEADER)).toBe(TRACE);
    expect(res.headers.get(TRACE_HEADER)).toBe(TRACE);
    expect(res.headers.get(MOCK_HEADER)).toBeNull();
  });

  it.each([1, 7, 100])('passes limit=%i through', async (limit) => {
    upstreamJson({ items: [], count: 0 });
    await list(get(`/api/v1/conversations?limit=${limit}`));
    expect(fetchSpy.mock.calls[0][0]).toBe(`${API}/v1/conversations?limit=${limit}`);
  });

  it.each(['0', '101', '-1', 'abc', '1.5', '', '1000', '%205', '5;drop'])(
    'rejects limit=%j with 400 and does not call the api',
    async (limit) => {
      await expectError(
        await list(get(`/api/v1/conversations?limit=${limit}`)),
        400,
        'invalid_request',
      );
      expect(fetchSpy).not.toHaveBeenCalled();
    },
  );

  it('passes an api error through with its own body and status', async () => {
    upstreamJson({ error: 'internal_error', trace_id: TRACE }, 500);
    const res = await list(get('/api/v1/conversations'));
    expect(res.status).toBe(500);
    await expect(res.json()).resolves.toEqual({ error: 'internal_error', trace_id: TRACE });
  });

  it('answers 502 upstream_unreachable with target and trace id when the api is down', async () => {
    fetchSpy.mockRejectedValue(new Error('ECONNREFUSED'));
    const res = await list(get('/api/v1/conversations', { [TRACE_HEADER]: TRACE }));
    expect(res.status).toBe(502);
    await expect(res.json()).resolves.toEqual({
      error: 'upstream_unreachable',
      target: 'api',
      trace_id: TRACE,
    });
    expect(res.headers.get(TRACE_HEADER)).toBe(TRACE);
  });

  it('answers 502 upstream_error when the api replies with something that is not JSON', async () => {
    fetchSpy.mockResolvedValue(new Response('<html>bad gateway</html>', { status: 200 }));
    await expectError(await list(get('/api/v1/conversations')), 502, 'upstream_error');
  });

  it('serves the list fixture in mock mode, marked, without calling the api', async () => {
    process.env.MOCK_UPSTREAM = 'true';
    const res = await list(get('/api/v1/conversations'));
    expect(res.status).toBe(200);
    expect(res.headers.get(MOCK_HEADER)).toBe('true');
    await expect(res.json()).resolves.toEqual(conversationListFixture);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('still validates limit in mock mode', async () => {
    process.env.MOCK_UPSTREAM = 'true';
    await expectError(await list(get('/api/v1/conversations?limit=0')), 400, 'invalid_request');
  });
});

// --- POST /api/v1/conversations ---------------------------------------------------------------

describe('POST /api/v1/conversations', () => {
  it('forwards no body when none was given and returns the api 201', async () => {
    upstreamJson(createdConversationFixture, 201);
    const res = await create(post('/api/v1/conversations'));
    expect(res.status).toBe(201);
    await expect(res.json()).resolves.toEqual(createdConversationFixture);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe(`${API}/v1/conversations`);
    expect(init?.method).toBe('POST');
    expect(init?.body).toBeUndefined();
  });

  it.each(['', '   ', 'null', '{}', '{"title":null}'])(
    'treats %j as "no title" and forwards no body',
    async (body) => {
      upstreamJson(createdConversationFixture, 201);
      expect((await create(post('/api/v1/conversations', body))).status).toBe(201);
      expect(fetchSpy.mock.calls[0][1]?.body).toBeUndefined();
    },
  );

  it('forwards exactly { title } with a JSON content type', async () => {
    upstreamJson(createdConversationFixture, 201);
    await create(post('/api/v1/conversations', { title: 'Tracing' }, { [TRACE_HEADER]: TRACE }));
    const [, init] = fetchSpy.mock.calls[0];
    expect(init?.body).toBe('{"title":"Tracing"}');
    expect(new Headers(init?.headers).get('content-type')).toBe('application/json');
    expect(new Headers(init?.headers).get(TRACE_HEADER)).toBe(TRACE);
  });

  it.each([
    ['malformed JSON', '{not json'],
    ['an array', '[1,2]'],
    ['a bare string', '"a title"'],
    ['a number', '12'],
    ['an unknown field', '{"title":"t","extra":1}'],
    ['a non-string title', '{"title":123}'],
    ['an object title', '{"title":{"a":1}}'],
    ['a title over 200 characters', JSON.stringify({ title: 't'.repeat(201) })],
    ['101 emoji (202 UTF-16 units)', JSON.stringify({ title: '\u{1F600}'.repeat(101) })],
  ])('rejects %s with 400 and does not call the api', async (_name, body) => {
    await expectError(await create(post('/api/v1/conversations', body)), 400, 'invalid_request');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each([['t'.repeat(200)], ['\u{1F600}'.repeat(100)]])(
    'accepts a title that fits the column (%#)',
    async (title) => {
      upstreamJson(createdConversationFixture, 201);
      expect((await create(post('/api/v1/conversations', { title }))).status).toBe(201);
    },
  );

  it('refuses an oversized body that is counted while it is read, and never forwards it', async () => {
    const big = JSON.stringify({ title: 'x' }) + ' '.repeat(MAX_CREATE_BODY_BYTES + 1);
    await expectError(await create(post('/api/v1/conversations', big)), 413, 'payload_too_large');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('refuses a streamed body that exceeds the cap even though it declares no length', async () => {
    const request = chunkedPost(MAX_CREATE_BODY_BYTES * 4);
    expect(request.headers.get('content-length')).toBeNull();
    await expectError(await create(request), 413, 'payload_too_large');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('accepts a body of exactly the cap', async () => {
    upstreamJson(createdConversationFixture, 201);
    const json = '{"title":"ok"}';
    const exact = json + ' '.repeat(MAX_CREATE_BODY_BYTES - json.length);
    expect(new TextEncoder().encode(exact).byteLength).toBe(MAX_CREATE_BODY_BYTES);
    expect((await create(post('/api/v1/conversations', exact))).status).toBe(201);
  });

  it('passes an api 422 through with its body', async () => {
    upstreamJson({ error: 'validation_error', trace_id: TRACE }, 422);
    const res = await create(post('/api/v1/conversations', { title: 'x' }));
    expect(res.status).toBe(422);
    await expect(res.json()).resolves.toEqual({ error: 'validation_error', trace_id: TRACE });
  });

  it('answers 502 when the api is unreachable', async () => {
    fetchSpy.mockRejectedValue(new Error('ECONNREFUSED'));
    const res = await create(post('/api/v1/conversations', { title: 'x' }));
    expect(res.status).toBe(502);
    expect((await res.json()).error).toBe('upstream_unreachable');
  });

  describe('in mock mode', () => {
    beforeEach(() => {
      process.env.MOCK_UPSTREAM = 'true';
    });

    it('returns the created fixture as a marked 201 without calling the api', async () => {
      const res = await create(post('/api/v1/conversations'));
      expect(res.status).toBe(201);
      expect(res.headers.get(MOCK_HEADER)).toBe('true');
      await expect(res.json()).resolves.toEqual(createdConversationFixture);
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('reflects the given title', async () => {
      const res = await create(post('/api/v1/conversations', { title: '  My topic ' }));
      expect((await res.json()).title).toBe('My topic');
    });

    it('still validates and still caps the body', async () => {
      await expectError(
        await create(post('/api/v1/conversations', '{"x":1}')),
        400,
        'invalid_request',
      );
      await expectError(
        await create(chunkedPost(MAX_CREATE_BODY_BYTES * 2)),
        413,
        'payload_too_large',
      );
    });
  });
});

// --- GET /api/v1/conversations/[id] -------------------------------------------------------------

describe('GET /api/v1/conversations/[id]', () => {
  it('proxies to api GET /v1/conversations/{id} and returns its body', async () => {
    const detail = findConversationFixture(ID);
    upstreamJson(detail);
    const res = await getOne(
      get(`/api/v1/conversations/${ID}`, { [TRACE_HEADER]: TRACE }),
      ctx(ID),
    );

    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual(detail);
    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe(`${API}/v1/conversations/${ID}`);
    expect(new Headers(init?.headers).get(TRACE_HEADER)).toBe(TRACE);
    expect(res.headers.get(TRACE_HEADER)).toBe(TRACE);
  });

  it('accepts an upper-case UUID', async () => {
    upstreamJson(findConversationFixture(ID));
    expect((await getOne(get('/x'), ctx(ID.toUpperCase()))).status).toBe(200);
  });

  it.each([
    'not-a-uuid',
    '',
    '../v1/health',
    '..%2F..%2Fhealth',
    `${ID}/messages`,
    `${ID}?x=1`,
    `${ID}%00`,
    ' ' + ID,
    '5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e0', // one character short
    '5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e0g', // not hex
  ])('rejects the id %j with 400 and never builds an upstream URL', async (id) => {
    await expectError(await getOne(get('/x'), ctx(id)), 400, 'invalid_request');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('passes an api 404 through with its own body', async () => {
    upstreamJson({ error: 'not_found', trace_id: TRACE }, 404);
    const res = await getOne(get('/x'), ctx(ID));
    expect(res.status).toBe(404);
    await expect(res.json()).resolves.toEqual({ error: 'not_found', trace_id: TRACE });
  });

  it('answers 502 when the api is unreachable, and 502 when it replies with non-JSON', async () => {
    fetchSpy.mockRejectedValueOnce(new Error(`ECONNREFUSED ${API}/v1/conversations/${ID}`));
    const down = await getOne(get('/x'), ctx(ID));
    expect(down.status).toBe(502);
    expect((await down.json()).error).toBe('upstream_unreachable');

    fetchSpy.mockResolvedValueOnce(new Response('oops', { status: 200 }));
    await expectError(await getOne(get('/x'), ctx(ID)), 502, 'upstream_error');
  });

  describe('in mock mode', () => {
    beforeEach(() => {
      process.env.MOCK_UPSTREAM = 'true';
    });

    it('serves the fixture of a known id, marked, without calling the api', async () => {
      const res = await getOne(get('/x'), ctx(ID));
      expect(res.status).toBe(200);
      expect(res.headers.get(MOCK_HEADER)).toBe('true');
      await expect(res.json()).resolves.toEqual(findConversationFixture(ID));
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('answers a marked 404 for an unknown id', async () => {
      const res = await getOne(get('/x'), ctx('00000000-0000-4000-8000-000000000000'));
      expect(res.status).toBe(404);
      expect(res.headers.get(MOCK_HEADER)).toBe('true');
      expect((await res.json()).error).toBe('not_found');
    });

    it('still rejects a malformed id', async () => {
      await expectError(await getOne(get('/x'), ctx('nope')), 400, 'invalid_request');
    });
  });
});

// --- hardening found in review ---------------------------------------------------------------

/** A POST with an explicit content-length and a body stream that records whether it was read. */
function declaredPost(declared: number, headers: Record<string, string> = {}) {
  const state = { read: false };
  const request = new Request('http://web/api/v1/conversations', {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'content-length': String(declared), ...headers },
    body: new ReadableStream<Uint8Array>(
      {
        pull(controller) {
          state.read = true;
          controller.enqueue(new TextEncoder().encode('{}'));
          controller.close();
        },
      },
      { highWaterMark: 0 },
    ),
    duplex: 'half',
  } as RequestInit);
  return { request, state };
}

describe('POST /api/v1/conversations — body hardening', () => {
  it('refuses on the DECLARED length alone, without reading any of the body', async () => {
    const { request, state } = declaredPost(MAX_CREATE_BODY_BYTES + 1);
    expect(request.headers.get('content-length')).toBe(String(MAX_CREATE_BODY_BYTES + 1));
    await expectError(await create(request), 413, 'payload_too_large');
    expect(state.read).toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each(['abc', '-5', 'NaN', '10, 20'])(
    'a lying or garbage content-length (%j) falls back to counting the real bytes',
    async (declared) => {
      upstreamJson(createdConversationFixture, 201);
      const { request } = declaredPost(0, { 'content-length': declared });
      expect((await create(request)).status).toBe(201);
    },
  );

  it('answers 400 (not a framework 500) when the upload breaks off', async () => {
    const broken = new Request('http://web/api/v1/conversations', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.error(new Error('client went away'));
        },
      }),
      duplex: 'half',
    } as RequestInit);
    await expectError(await create(broken), 400, 'invalid_request');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it.each(['text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data', ''])(
    'refuses a body declared as %j with 415 (a cross-site "simple" request must not create)',
    async (contentType) => {
      const res = await create(
        new Request('http://web/api/v1/conversations', {
          method: 'POST',
          headers: contentType ? { 'content-type': contentType } : {},
          body: '{"title":"x"}',
        }),
      );
      expect(res.status).toBe(415);
      expect((await res.json()).error).toBe('unsupported_media_type');
      expect(fetchSpy).not.toHaveBeenCalled();
    },
  );

  it.each(['application/json', 'application/json; charset=utf-8', 'Application/JSON'])(
    'accepts %j',
    async (contentType) => {
      upstreamJson(createdConversationFixture, 201);
      const res = await create(
        new Request('http://web/api/v1/conversations', {
          method: 'POST',
          headers: { 'content-type': contentType },
          body: '{"title":"x"}',
        }),
      );
      expect(res.status).toBe(201);
    },
  );

  it('needs no content type when there is no body at all', async () => {
    upstreamJson(createdConversationFixture, 201);
    const res = await create(new Request('http://web/api/v1/conversations', { method: 'POST' }));
    expect(res.status).toBe(201);
  });
});

describe('what the api sends back', () => {
  it('never follows a redirect', async () => {
    upstreamJson(conversationListFixture);
    await list(get('/api/v1/conversations'));
    expect(fetchSpy.mock.calls[0][1]?.redirect).toBe('error');
    fetchSpy.mockRejectedValueOnce(new TypeError('fetch failed: redirect mode is set to error'));
    expect((await list(get('/api/v1/conversations'))).status).toBe(502);
  });

  it('lets an api error through only in the api error shape', async () => {
    upstreamJson({ error: 'internal_error', trace_id: TRACE }, 500);
    const shaped = await list(get('/api/v1/conversations'));
    expect(shaped.status).toBe(500);
    await expect(shaped.json()).resolves.toEqual({ error: 'internal_error', trace_id: TRACE });

    // a framework default (FastAPI's {"detail": …}) or any other body must not reach the browser
    for (const [body, status] of [
      [{ detail: 'Not Found' }, 404],
      [{ detail: [{ loc: ['query', 'limit'], msg: 'x', input: 'secret' }] }, 422],
      [['not', 'an', 'object'], 500],
      [{ error: 7, trace_id: TRACE }, 500],
    ] as const) {
      fetchSpy.mockResolvedValueOnce(Response.json(body, { status }));
      const res = await list(get('/api/v1/conversations'));
      expect(res.status).toBe(502);
      const out = await res.json();
      expect(out.error).toBe('upstream_error');
      expect(JSON.stringify(out)).not.toContain('detail');
      expect(JSON.stringify(out)).not.toContain('secret');
    }
  });

  it('refuses to buffer an absurdly large reply and answers 502 instead', async () => {
    const chunk = new Uint8Array(1024 * 1024).fill(32);
    let sent = 0;
    fetchSpy.mockResolvedValueOnce(
      new Response(
        new ReadableStream<Uint8Array>({
          pull(controller) {
            if (sent >= 64) return controller.close(); // far more than the 8 MiB cap
            controller.enqueue(chunk);
            sent += 1;
          },
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    );
    await expectError(await list(get('/api/v1/conversations')), 502, 'upstream_error');
    // It gave up at the cap instead of draining the stream (a stream reads at most a chunk ahead).
    expect(sent).toBeLessThan(16);
  });

  it('passes a normal, large-ish thread through intact', async () => {
    const detail = findConversationFixture(ID);
    upstreamJson(detail);
    const res = await getOne(get('/x'), ctx(ID));
    await expect(res.json()).resolves.toEqual(detail);
  });
});

describe('mock mode behaves like the api', () => {
  beforeEach(() => {
    process.env.MOCK_UPSTREAM = 'true';
  });

  it('honours limit and reports the number returned', async () => {
    const res = await list(get('/api/v1/conversations?limit=2'));
    const body = await res.json();
    expect(body.items).toHaveLength(2);
    expect(body.count).toBe(2);
    expect(body.items).toEqual(conversationListFixture.items.slice(0, 2));
  });

  it('collapses inner whitespace in a title like the api does', async () => {
    const res = await create(post('/api/v1/conversations', { title: '  How   does\ttracing  ' }));
    expect((await res.json()).title).toBe('How does tracing');
  });

  it('refuses a mock in prod even with stray whitespace in APP_ENV', async () => {
    process.env.APP_ENV = ' PROD ';
    upstreamJson(conversationListFixture);
    const res = await list(get('/api/v1/conversations'));
    expect(res.headers.get(MOCK_HEADER)).toBeNull();
    expect(fetchSpy).toHaveBeenCalled();
    delete process.env.APP_ENV;
  });
});

// --- cross-cutting: trace id, logs, metrics ------------------------------------------------------

describe('trace id, logs and metrics', () => {
  it('replaces an invalid inbound trace id with one minted by the web origin', async () => {
    upstreamJson(conversationListFixture);
    const res = await list(get('/api/v1/conversations', { [TRACE_HEADER]: 'not-valid' }));
    const echoed = res.headers.get(TRACE_HEADER);
    expect(echoed).toMatch(/^0eb0[0-9a-f]{28}$/);
    expect(new Headers(fetchSpy.mock.calls[0][1]?.headers).get(TRACE_HEADER)).toBe(echoed);
  });

  it('logs only route patterns and statuses — never an id, a title or the upstream URL', async () => {
    upstreamJson(findConversationFixture(ID));
    await getOne(get(`/api/v1/conversations/${ID}`), ctx(ID));
    fetchSpy.mockRejectedValueOnce(new Error(`ECONNREFUSED ${API}/v1/conversations/${ID}`));
    await getOne(get(`/api/v1/conversations/${ID}`), ctx(ID));
    upstreamJson(createdConversationFixture, 201);
    await create(post('/api/v1/conversations', { title: SECRET_TITLE }));
    await create(post('/api/v1/conversations', { title: 7, secret: SECRET_TITLE }));

    const logged = JSON.stringify(vi.mocked(log).mock.calls);
    expect(vi.mocked(log).mock.calls.length).toBeGreaterThan(3);
    for (const forbidden of [ID, SECRET_TITLE, 'ECONNREFUSED', `${API}/v1`]) {
      expect(logged).not.toContain(forbidden);
    }
    expect(logged).toContain('/api/v1/conversations/[id]');
    expect(logged).toContain('bff rejected request'); // the 400 above was logged too, bounded
  });

  it('labels the duration metric with the route pattern, never the concrete id', async () => {
    upstreamJson(findConversationFixture(ID));
    await getOne(get(`/api/v1/conversations/${ID}`), ctx(ID));
    await list(get('/api/v1/conversations?limit=3'));
    const labels = vi.mocked(recordServerDuration).mock.calls.map(([arg]) => arg.routeClass);
    expect(labels).toEqual(['/api/v1/conversations/[id]', '/api/v1/conversations']);
    expect(labels.join()).not.toContain(ID);
  });
});
