import { describe, expect, it } from 'vitest';
import {
  MAX_CREATE_BODY_BYTES,
  isJsonContentType,
  isUuid,
  parseCreateBody,
  parseLimit,
  readBodyCapped,
} from '@/lib/conversations-bff';

describe('isUuid', () => {
  it('accepts a UUID in either case and nothing that could alter a URL path', () => {
    expect(isUuid('5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e01')).toBe(true);
    expect(isUuid('5B1F3A52-8C1E-4D3A-9D0B-1A2B3C4D5E01')).toBe(true);
    for (const bad of [
      '',
      'abc',
      '../x',
      '5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e01/x',
      '5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e01\n',
    ]) {
      expect(isUuid(bad)).toBe(false);
    }
  });
});

describe('parseLimit', () => {
  it('absent → undefined; 1–100 → the number; everything else → null', () => {
    expect(parseLimit(null)).toBeUndefined();
    expect(parseLimit('1')).toBe(1);
    expect(parseLimit('100')).toBe(100);
    for (const bad of ['0', '101', '-1', '1.5', 'x', '', ' 5', '5 ', '1e1', '0010x', '1000']) {
      expect(parseLimit(bad)).toBeNull();
    }
  });
});

describe('parseCreateBody', () => {
  it('accepts no body, null, {} and a title', () => {
    expect(parseCreateBody('')).toEqual({ ok: true, title: undefined });
    expect(parseCreateBody('null')).toEqual({ ok: true, title: undefined });
    expect(parseCreateBody('{}')).toEqual({ ok: true, title: undefined });
    expect(parseCreateBody('{"title":null}')).toEqual({ ok: true, title: null });
    expect(parseCreateBody('{"title":"Hello"}')).toEqual({ ok: true, title: 'Hello' });
  });

  it('measures the title in UTF-16 units, like the database column', () => {
    expect(parseCreateBody(JSON.stringify({ title: '\u{1F600}'.repeat(100) })).ok).toBe(true);
    expect(parseCreateBody(JSON.stringify({ title: '\u{1F600}'.repeat(101) })).ok).toBe(false);
  });

  it('rejects anything else', () => {
    for (const bad of ['{', '[]', '1', '"x"', '{"title":1}', '{"title":"a","b":2}', '{"b":2}']) {
      expect(parseCreateBody(bad).ok, bad).toBe(false);
    }
  });
});

describe('readBodyCapped', () => {
  const request = (body: BodyInit | null, headers: Record<string, string> = {}) =>
    new Request('http://web/x', { method: 'POST', body, headers });
  const streamOf = (chunks: string[], extra: Partial<UnderlyingDefaultSource<Uint8Array>> = {}) =>
    new Request('http://web/x', {
      method: 'POST',
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk));
          controller.close();
        },
        ...extra,
      }),
      duplex: 'half',
    } as RequestInit);

  it('reads a small body and treats no body as empty', async () => {
    await expect(readBodyCapped(request('{"a":1}'), 100)).resolves.toEqual({
      kind: 'ok',
      text: '{"a":1}',
    });
    await expect(readBodyCapped(request(null), 100)).resolves.toEqual({ kind: 'ok', text: '' });
  });

  it('is too_large for a body counted over the cap', async () => {
    await expect(readBodyCapped(request('x'.repeat(101)), 100)).resolves.toEqual({
      kind: 'too_large',
    });
    await expect(readBodyCapped(streamOf(['x'.repeat(60), 'x'.repeat(60)]), 100)).resolves.toEqual({
      kind: 'too_large',
    });
  });

  it('is too_large on the declared length alone, and then reads nothing', async () => {
    let pulled = false;
    const req = new Request('http://web/x', {
      method: 'POST',
      headers: { 'content-length': '500' },
      body: new ReadableStream<Uint8Array>(
        {
          pull() {
            pulled = true;
          },
        },
        { highWaterMark: 0 },
      ),
      duplex: 'half',
    } as RequestInit);
    await expect(readBodyCapped(req, 100)).resolves.toEqual({ kind: 'too_large' });
    expect(pulled).toBe(false);
  });

  it('ignores a garbage declared length and counts the bytes instead', async () => {
    for (const declared of ['abc', '-1', 'NaN', '1, 2']) {
      await expect(
        readBodyCapped(request('{}', { 'content-length': declared }), 100),
      ).resolves.toEqual({ kind: 'ok', text: '{}' });
    }
  });

  it('counts bytes, not characters', async () => {
    const emoji = '\u{1F600}'.repeat(30); // 120 bytes, 60 UTF-16 units
    await expect(readBodyCapped(request(emoji), 100)).resolves.toEqual({ kind: 'too_large' });
    await expect(readBodyCapped(request(emoji), 120)).resolves.toEqual({ kind: 'ok', text: emoji });
  });

  it('is unreadable when the stream errors', async () => {
    const req = new Request('http://web/x', {
      method: 'POST',
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.error(new Error('boom'));
        },
      }),
      duplex: 'half',
    } as RequestInit);
    await expect(readBodyCapped(req, 100)).resolves.toEqual({ kind: 'unreadable' });
  });

  it('is unreadable when the client stalls past the timeout, and the reader is cancelled', async () => {
    let cancelled = false;
    const req = new Request('http://web/x', {
      method: 'POST',
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('{"ti'));
          // never sends more, never closes
        },
        cancel() {
          cancelled = true;
        },
      }),
      duplex: 'half',
    } as RequestInit);
    await expect(readBodyCapped(req, 100, 30)).resolves.toEqual({ kind: 'unreadable' });
    expect(cancelled).toBe(true);
  });

  it('the cap itself is small enough for a title and no more', () => {
    expect(MAX_CREATE_BODY_BYTES).toBeLessThanOrEqual(4096);
    expect(MAX_CREATE_BODY_BYTES).toBeGreaterThan(
      JSON.stringify({ title: 'x'.repeat(200) }).length,
    );
  });
});

describe('isJsonContentType', () => {
  const withType = (type: string | null) =>
    new Request('http://web/x', {
      method: 'POST',
      headers: type === null ? {} : { 'content-type': type },
    });
  it('accepts application/json, with or without parameters, in any case', () => {
    for (const ok of [
      'application/json',
      'application/json; charset=utf-8',
      'APPLICATION/JSON',
      'application/json ;x=1',
    ]) {
      expect(isJsonContentType(withType(ok)), ok).toBe(true);
    }
  });
  it('rejects everything else', () => {
    for (const bad of [
      null,
      '',
      'text/plain',
      'application/jsonp',
      'application/json-seq',
      'multipart/form-data',
    ]) {
      expect(isJsonContentType(withType(bad)), String(bad)).toBe(false);
    }
  });
});
