import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  AskLimiter,
  DEFAULT_MAX_INFLIGHT,
  DEFAULT_MAX_INFLIGHT_PER_CLIENT,
  SHARED_CLIENT,
  askLimiter,
  MAX_TRUSTED_PROXY_HOPS,
  clientKeyOf,
  normaliseAddress,
  readProxyHops,
  isSameOriginRequest,
  readAskLimits,
  readLimit,
  releaseWhenDone,
  __resetAskLimiterForTests,
} from '@/lib/ask-limits';

const LIMITS = { maxInflight: 3, maxPerClient: 2 };

function req(headers: Record<string, string> = {}, url = 'https://app.example.com/api/x'): Request {
  return new Request(url, { method: 'POST', headers });
}

function streamOf(...chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(c) {
      for (const chunk of chunks) c.enqueue(encoder.encode(chunk));
      c.close();
    },
  });
}

afterEach(() => {
  __resetAskLimiterForTests();
  vi.unstubAllEnvs();
});

describe('readLimit / readAskLimits', () => {
  it.each([
    [undefined, 8],
    ['', 8],
    ['  ', 8],
    ['5', 5],
    [' 5 ', 5],
    ['1000', 1000],
    ['1001', 8],
    ['0', 8],
    ['-3', 8],
    ['2.5', 8],
    ['many', 8],
    ['1e3', 8],
    ['99999999', 8],
  ])('reads %j as %i (a mistake is the default, never "no limit")', (raw, expected) => {
    expect(readLimit(raw as string | undefined, 8)).toBe(expected);
  });

  it('has the documented defaults and reads the environment', () => {
    expect([DEFAULT_MAX_INFLIGHT, DEFAULT_MAX_INFLIGHT_PER_CLIENT]).toEqual([8, 2]);
    expect(readAskLimits({})).toEqual({ maxInflight: 8, maxPerClient: 2 });
    expect(readAskLimits({ ASK_MAX_INFLIGHT: '3', ASK_MAX_INFLIGHT_PER_CLIENT: '1' })).toEqual({
      maxInflight: 3,
      maxPerClient: 1,
    });
  });
});

describe('clientKeyOf', () => {
  it('is the rightmost X-Forwarded-For address, never one the client could have supplied', () => {
    expect(clientKeyOf(req({ 'x-forwarded-for': '10.0.0.1' }))).toBe('10.0.0.1');
    expect(clientKeyOf(req({ 'x-forwarded-for': '1.2.3.4, 5.6.7.8, 10.0.0.9' }))).toBe('10.0.0.9');
    expect(clientKeyOf(req({ 'x-forwarded-for': '  6.6.6.6 ,   10.0.0.9  ' }))).toBe('10.0.0.9');
  });

  it('handles IPv6: case does not matter and a client owns its whole /64', () => {
    expect(clientKeyOf(req({ 'x-forwarded-for': '2001:DB8::1' }))).toBe('2001:db8:0:0::/64');
    expect(clientKeyOf(req({ 'x-forwarded-for': '[2001:db8::1]' }))).toBe('2001:db8:0:0::/64');
    // two addresses in one /64 are one client: rotating inside it buys nothing
    expect(clientKeyOf(req({ 'x-forwarded-for': '2001:db8:0:0:aaaa::1' }))).toBe(
      clientKeyOf(req({ 'x-forwarded-for': '2001:db8:0:0:bbbb::2' })),
    );
    expect(clientKeyOf(req({ 'x-forwarded-for': '2001:db8:0:1::1' }))).not.toBe(
      clientKeyOf(req({ 'x-forwarded-for': '2001:db8:0:0::1' })),
    );
  });

  it.each([
    ['no header', {}],
    ['an empty header', { 'x-forwarded-for': '' }],
    ['a trailing comma', { 'x-forwarded-for': '1.2.3.4,' }],
    ['not an address', { 'x-forwarded-for': 'unknown' }],
    ['a name', { 'x-forwarded-for': '1.2.3.4, evil.example' }],
    ['markup', { 'x-forwarded-for': '<script>' }],
    ['a very long value', { 'x-forwarded-for': '1'.repeat(200) }],
  ])('falls back to the one shared bucket for %s', (_name, headers) => {
    expect(clientKeyOf(req(headers))).toBe(SHARED_CLIENT);
  });

  it('does not fall back to an address further left when the rightmost is unusable', () => {
    expect(clientKeyOf(req({ 'x-forwarded-for': '1.2.3.4, unknown' }))).toBe(SHARED_CLIENT);
  });
});

describe('AskLimiter', () => {
  it('lets an instance hold as many as its limit and then refuses with the reason', () => {
    const limiter = new AskLimiter();
    const held = ['a', 'b', 'c'].map((c) => limiter.tryAcquire(c, LIMITS));
    expect(held.every((h) => h.ok)).toBe(true);
    expect(limiter.tryAcquire('d', LIMITS)).toEqual({ ok: false, why: 'instance' });
    expect(limiter.snapshot()).toEqual({ inflight: 3, clients: 3 });
  });

  it('caps one client below the instance limit', () => {
    const limiter = new AskLimiter();
    expect(limiter.tryAcquire('a', LIMITS).ok).toBe(true);
    expect(limiter.tryAcquire('a', LIMITS).ok).toBe(true);
    expect(limiter.tryAcquire('a', LIMITS)).toEqual({ ok: false, why: 'client' });
    expect(limiter.tryAcquire('b', LIMITS).ok).toBe(true); // another client still gets one
  });

  it('checks the instance cap first', () => {
    const limiter = new AskLimiter();
    limiter.tryAcquire('a', { maxInflight: 1, maxPerClient: 1 });
    expect(limiter.tryAcquire('a', { maxInflight: 1, maxPerClient: 1 })).toEqual({
      ok: false,
      why: 'instance',
    });
  });

  it('gives a slot back on release, exactly once', () => {
    const limiter = new AskLimiter();
    const first = limiter.tryAcquire('a', LIMITS);
    const second = limiter.tryAcquire('a', LIMITS);
    if (!first.ok || !second.ok) throw new Error('expected both');
    first.release();
    first.release(); // a second release changes nothing
    first.release();
    expect(limiter.snapshot()).toEqual({ inflight: 1, clients: 1 });
    expect(limiter.tryAcquire('a', LIMITS).ok).toBe(true);
    second.release();
    expect(limiter.snapshot().inflight).toBe(1);
  });

  it('forgets a client that holds nothing (the map cannot grow without bound)', () => {
    const limiter = new AskLimiter();
    for (let i = 0; i < 500; i += 1) {
      const taken = limiter.tryAcquire(`10.0.${i >> 8}.${i & 255}`, LIMITS);
      if (taken.ok) taken.release();
    }
    expect(limiter.snapshot()).toEqual({ inflight: 0, clients: 0 });
  });

  it('is one limiter for the whole process', () => {
    expect(askLimiter()).toBe(askLimiter());
    const a = askLimiter();
    __resetAskLimiterForTests();
    expect(askLimiter()).not.toBe(a);
  });

  it('uses the environment limits by default', () => {
    vi.stubEnv('ASK_MAX_INFLIGHT', '1');
    const limiter = new AskLimiter();
    expect(limiter.tryAcquire('a').ok).toBe(true);
    expect(limiter.tryAcquire('b')).toEqual({ ok: false, why: 'instance' });
  });
});

describe('releaseWhenDone', () => {
  it('passes the bytes through unchanged and releases when the stream ends', async () => {
    const release = vi.fn();
    const out = releaseWhenDone(streamOf('event: a\n', 'data: 1\n\n'), release);
    expect(release).not.toHaveBeenCalled();
    await expect(new Response(out).text()).resolves.toBe('event: a\ndata: 1\n\n');
    expect(release).toHaveBeenCalledTimes(1);
  });

  it('releases when the source fails, and passes the failure on', async () => {
    const release = vi.fn();
    const broken = new ReadableStream<Uint8Array>({
      pull() {
        throw new Error('reset');
      },
    });
    await expect(new Response(releaseWhenDone(broken, release)).text()).rejects.toThrow();
    expect(release).toHaveBeenCalledTimes(1);
  });

  it('releases when the reader goes away, and cancels the source', async () => {
    const release = vi.fn();
    const cancelled = vi.fn();
    const source = new ReadableStream<Uint8Array>({
      pull(c) {
        c.enqueue(new TextEncoder().encode('x'));
      },
      cancel: cancelled,
    });
    const reader = releaseWhenDone(source, release)!.getReader();
    await reader.read();
    expect(release).not.toHaveBeenCalled();
    await reader.cancel();
    expect(release).toHaveBeenCalledTimes(1);
    expect(cancelled).toHaveBeenCalled();
  });

  it('releases at once when there is no body', () => {
    const release = vi.fn();
    expect(releaseWhenDone(null, release)).toBeNull();
    expect(release).toHaveBeenCalledTimes(1);
  });
});

describe('isSameOriginRequest', () => {
  it('passes a request with neither header (curl, a test)', () => {
    expect(isSameOriginRequest(req())).toBe(true);
  });

  it("passes the app's own origin, whatever the scheme the ingress forwards", () => {
    expect(
      isSameOriginRequest(req({ origin: 'https://app.example.com', host: 'app.example.com' })),
    ).toBe(true);
    expect(
      isSameOriginRequest(req({ origin: 'http://app.example.com', host: 'app.example.com' })),
    ).toBe(true);
    expect(
      isSameOriginRequest(req({ origin: 'http://localhost:3000', host: 'localhost:3000' })),
    ).toBe(true);
  });

  it('is case-insensitive on the host', () => {
    expect(
      isSameOriginRequest(req({ origin: 'https://App.Example.com', host: 'app.example.COM' })),
    ).toBe(true);
  });

  it.each([
    ['another site', { origin: 'https://evil.example', host: 'app.example.com' }],
    ['another port', { origin: 'https://app.example.com:8443', host: 'app.example.com' }],
    ['a sibling subdomain', { origin: 'https://other.example.com', host: 'app.example.com' }],
    ['a suffix trick', { origin: 'https://app.example.com.evil.example', host: 'app.example.com' }],
    ['the null origin', { origin: 'null', host: 'app.example.com' }],
    ['garbage', { origin: 'not an origin', host: 'app.example.com' }],
    ['an empty origin', { origin: '', host: 'app.example.com' }],
  ])('refuses %s', (_name, headers) => {
    expect(isSameOriginRequest(req(headers))).toBe(false);
  });

  it('refuses Sec-Fetch-Site: cross-site even with a matching Origin, and allows the others', () => {
    const host = { host: 'app.example.com' };
    expect(isSameOriginRequest(req({ ...host, 'sec-fetch-site': 'cross-site' }))).toBe(false);
    expect(isSameOriginRequest(req({ ...host, 'sec-fetch-site': ' Cross-Site ' }))).toBe(false);
    expect(
      isSameOriginRequest(
        req({ ...host, origin: 'https://app.example.com', 'sec-fetch-site': 'cross-site' }),
      ),
    ).toBe(false);
    for (const value of ['same-origin', 'none', 'same-site']) {
      expect(isSameOriginRequest(req({ ...host, 'sec-fetch-site': value }))).toBe(true);
    }
  });

  it('falls back to the URL host when there is no Host header', () => {
    const request = new Request('https://app.example.com/x', {
      method: 'POST',
      headers: { origin: 'https://app.example.com' },
    });
    request.headers.delete('host');
    expect(isSameOriginRequest(request)).toBe(true);
  });
});

describe('normaliseAddress — one client, one key, whatever the form', () => {
  it.each([
    ['1.2.3.4', '1.2.3.4'],
    ['1.2.3.4:51234', '1.2.3.4'],
    ['  1.2.3.4  ', '1.2.3.4'],
    ['::ffff:1.2.3.4', '1.2.3.4'],
    ['::FFFF:0102:0304', '1.2.3.4'],
    ['[::ffff:1.2.3.4]:8080', '1.2.3.4'],
    ['::1', '0:0:0:0::/64'],
    ['0:0:0:0:0:0:0:1', '0:0:0:0::/64'],
    ['[::1]:5000', '0:0:0:0::/64'],
    ['fe80::1%eth0', 'fe80:0:0:0::/64'],
    ['2001:db8:85a3::8a2e:370:7334', '2001:db8:85a3:0::/64'],
  ])('%s is %s', (raw, key) => {
    expect(normaliseAddress(raw)).toBe(key);
  });

  it('turns equivalent forms into one key (a port or a mapped form cannot make a new client)', () => {
    const forms = ['1.2.3.4', '1.2.3.4:1', '1.2.3.4:65535', '::ffff:1.2.3.4', '[::ffff:1.2.3.4]:9'];
    expect(new Set(forms.map(normaliseAddress)).size).toBe(1);
  });

  it.each([
    'unknown',
    '',
    '1.2.3',
    '1.2.3.4.5',
    '999.1.1.1',
    '1.2.3.4:',
    '[::1',
    '::1::2',
    '1:2:3:4:5:6:7:8:9',
    'g::1',
    '<script>',
    '1.2.3.4:99999999',
  ])('%j is not an address', (raw) => {
    expect(normaliseAddress(raw)).toBeNull();
  });
});

describe('proxies in front of the ingress (ASK_TRUSTED_PROXY_HOPS)', () => {
  const chain = req({ 'x-forwarded-for': '6.6.6.6, 10.1.1.1, 10.2.2.2' });

  it('uses the rightmost address by default, and the N-th from the right with hops', () => {
    expect(clientKeyOf(chain)).toBe('10.2.2.2');
    expect(clientKeyOf(chain, 0)).toBe('10.2.2.2');
    expect(clientKeyOf(chain, 1)).toBe('10.1.1.1');
    expect(clientKeyOf(chain, 2)).toBe('6.6.6.6');
  });

  it('is the shared bucket when there are fewer entries than hops (never the leftmost guess)', () => {
    expect(clientKeyOf(req({ 'x-forwarded-for': '10.2.2.2' }), 1)).toBe(SHARED_CLIENT);
    expect(clientKeyOf(chain, 3)).toBe(SHARED_CLIENT);
  });

  it('reads the setting from the environment', () => {
    vi.stubEnv('ASK_TRUSTED_PROXY_HOPS', '1');
    expect(clientKeyOf(chain)).toBe('10.1.1.1');
  });

  it.each([
    [undefined, 0],
    ['', 0],
    ['0', 0],
    ['1', 1],
    [' 2 ', 2],
    ['5', 5],
    ['6', 0],
    ['-1', 0],
    ['two', 0],
    ['1.5', 0],
    ['10', 0],
  ])('reads %j as %i hops', (raw, expected) => {
    expect(readProxyHops(raw as string | undefined)).toBe(expected);
    expect(MAX_TRUSTED_PROXY_HOPS).toBe(5);
  });
});

describe('releaseWhenDone — the backstop for a reader that stalls', () => {
  it('frees the slot and cancels the source once the hold time has passed, whatever the reader does', async () => {
    vi.useFakeTimers();
    try {
      const release = vi.fn();
      const cancelled = vi.fn();
      const source = new ReadableStream<Uint8Array>({
        pull(c) {
          c.enqueue(new TextEncoder().encode('x'));
        },
        cancel: cancelled,
      });
      const out = releaseWhenDone(source, release, 1000)!;
      const reader = out.getReader();
      await reader.read(); // the reader reads once and then stalls
      vi.advanceTimersByTime(999);
      expect(release).not.toHaveBeenCalled();
      vi.advanceTimersByTime(2);
      await vi.runAllTimersAsync();
      expect(release).toHaveBeenCalledTimes(1);
      expect(cancelled).toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not fire the backstop after a normal end (the timer is cleared), and releases once', async () => {
    vi.useFakeTimers();
    try {
      const release = vi.fn();
      const out = releaseWhenDone(streamOf('a'), release, 1000);
      const reading = new Response(out).text();
      await vi.runAllTimersAsync();
      await reading;
      expect(release).toHaveBeenCalledTimes(1);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it('a release by the reader leaving also clears the timer', async () => {
    vi.useFakeTimers();
    try {
      const release = vi.fn();
      const source = new ReadableStream<Uint8Array>({ pull() {} });
      const reader = releaseWhenDone(source, release, 1000)!.getReader();
      await reader.cancel();
      expect(release).toHaveBeenCalledTimes(1);
      expect(vi.getTimerCount()).toBe(0);
    } finally {
      vi.useRealTimers();
    }
  });
});
