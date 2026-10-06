import { describe, expect, it, vi } from 'vitest';
import { createConversation, getConversation, listConversations } from '@/lib/conversations-client';
import {
  conversationDetails,
  conversationIds,
  conversationListFixture,
  createdConversationFixture,
} from '@/mocks/conversations';

const TRACE = '0eb01aaaaaaaaaaaaaaaaaaaaaaaaaaa';

type Spy = ReturnType<typeof vi.fn>;

/** A fetch that answers every call with `body`; `spy` is the same function, typed for `.mock`. */
function fetchReturning(body: unknown, init: ResponseInit = {}) {
  const spy: Spy = vi.fn(async () => Response.json(body, init));
  return { fetchImpl: spy as unknown as typeof fetch, spy };
}

describe('conversations client', () => {
  it('lists conversations from the same-origin BFF route', async () => {
    const { fetchImpl, spy } = fetchReturning(conversationListFixture);
    expect(await listConversations({ fetchImpl })).toEqual({
      ok: true,
      data: conversationListFixture,
    });
    const [url, init] = spy.mock.calls[0];
    expect(url).toBe('/api/v1/conversations');
    expect(init.method).toBe('GET');
    expect(init.cache).toBe('no-store');
  });

  it('creates a conversation: no body without a title, a JSON body with one', async () => {
    const plain = fetchReturning(createdConversationFixture, { status: 201 });
    expect(await createConversation(undefined, { fetchImpl: plain.fetchImpl })).toEqual({
      ok: true,
      data: createdConversationFixture,
    });
    expect(plain.spy.mock.calls[0][1].method).toBe('POST');
    expect(plain.spy.mock.calls[0][1].body).toBeUndefined();

    const titled = fetchReturning(createdConversationFixture, { status: 201 });
    await createConversation('Hello', { fetchImpl: titled.fetchImpl });
    expect(titled.spy.mock.calls[0][1].headers).toEqual({ 'content-type': 'application/json' });
    expect(titled.spy.mock.calls[0][1].body).toBe('{"title":"Hello"}');
  });

  it('opens a conversation by id, encoding the id into the path', async () => {
    const detail = conversationDetails[conversationIds.tracing];
    const first = fetchReturning(detail);
    expect(await getConversation(conversationIds.tracing, { fetchImpl: first.fetchImpl })).toEqual({
      ok: true,
      data: detail,
    });
    expect(first.spy.mock.calls[0][0]).toBe(`/api/v1/conversations/${conversationIds.tracing}`);

    const odd = fetchReturning(detail);
    await getConversation('a/b?c', { fetchImpl: odd.fetchImpl });
    expect(odd.spy.mock.calls[0][0]).toBe('/api/v1/conversations/a%2Fb%3Fc');
  });

  it('reports the error code and trace_id from an error body', async () => {
    const { fetchImpl } = fetchReturning({ error: 'not_found', trace_id: TRACE }, { status: 404 });
    expect(await getConversation(conversationIds.tracing, { fetchImpl })).toEqual({
      ok: false,
      error: 'not_found',
      traceId: TRACE,
    });
  });

  it('falls back to the x-trace-id header when the body has no trace_id', async () => {
    const { fetchImpl } = fetchReturning(
      { error: 'upstream_error' },
      { status: 502, headers: { 'x-trace-id': TRACE } },
    );
    expect(await listConversations({ fetchImpl })).toEqual({
      ok: false,
      error: 'upstream_error',
      traceId: TRACE,
    });
  });

  it('treats an error body of the wrong shape as upstream_error', async () => {
    const array = fetchReturning(['nope'], { status: 500, headers: { 'x-trace-id': TRACE } });
    expect(await listConversations({ fetchImpl: array.fetchImpl })).toEqual({
      ok: false,
      error: 'upstream_error',
      traceId: TRACE,
    });
    const nul = fetchReturning(null, { status: 500 });
    expect(await listConversations({ fetchImpl: nul.fetchImpl })).toMatchObject({
      ok: false,
      error: 'upstream_error',
    });
  });

  it('handles a reply that is not JSON, whatever the status', async () => {
    const bad = vi.fn(
      async () => new Response('<html>', { status: 502, headers: { 'x-trace-id': TRACE } }),
    ) as unknown as typeof fetch;
    expect(await listConversations({ fetchImpl: bad })).toEqual({
      ok: false,
      error: 'upstream_error',
      traceId: TRACE,
    });
    const okButBad = vi.fn(
      async () => new Response('<html>', { status: 200 }),
    ) as unknown as typeof fetch;
    expect(await listConversations({ fetchImpl: okButBad })).toEqual({
      ok: false,
      error: 'invalid_response',
      traceId: null,
    });
  });

  it('rejects a 200 whose body lacks what the page needs', async () => {
    const list = fetchReturning({ count: 0 });
    expect(await listConversations({ fetchImpl: list.fetchImpl })).toMatchObject({
      ok: false,
      error: 'invalid_response',
    });
    const thread = fetchReturning({ id: 'x' });
    expect(await getConversation('x', { fetchImpl: thread.fetchImpl })).toMatchObject({
      ok: false,
      error: 'invalid_response',
    });
    const created = fetchReturning({ nope: true }, { status: 201 });
    expect(await createConversation(undefined, { fetchImpl: created.fetchImpl })).toMatchObject({
      ok: false,
      error: 'invalid_response',
    });
  });

  it('resolves, never throws, when the network fails or the call is aborted', async () => {
    const down = vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    }) as unknown as typeof fetch;
    expect(await listConversations({ fetchImpl: down })).toEqual({
      ok: false,
      error: 'network_error',
      traceId: null,
    });
  });

  it('passes the abort signal to fetch', async () => {
    const controller = new AbortController();
    const { fetchImpl, spy } = fetchReturning(conversationListFixture);
    await listConversations({ fetchImpl, signal: controller.signal });
    expect(spy.mock.calls[0][1].signal).toBe(controller.signal);
  });
});
