import { describe, expect, it } from 'vitest';
import {
  MAX_SSE_EVENT_CHARS,
  SseEventTooLargeError,
  parseSseBlock,
  readSseStream,
  sseFrame,
  sseStreamFromFrames,
} from '@/lib/sse';

async function collect(stream: ReadableStream<Uint8Array>) {
  const out: { event: string; data: string }[] = [];
  for await (const ev of readSseStream(stream)) out.push(ev);
  return out;
}

function streamOf(...chunks: (string | Uint8Array)[]) {
  const encoder = new TextEncoder();
  return new ReadableStream<Uint8Array>({
    start(c) {
      for (const chunk of chunks)
        c.enqueue(typeof chunk === 'string' ? encoder.encode(chunk) : chunk);
      c.close();
    },
  });
}

const LS = String.fromCharCode(0x2028);
const PS = String.fromCharCode(0x2029);
const NEL = String.fromCharCode(0x85);

describe('sse helpers', () => {
  it('sseFrame produces the api wire format', () => {
    expect(sseFrame('token', { text: 'hi' })).toBe('event: token\ndata: {"text":"hi"}\n\n');
  });

  it('parseSseBlock handles event/data, comments, multi-line data and defaults', () => {
    expect(parseSseBlock('event: done\ndata: {"a":1}')).toEqual({ event: 'done', data: '{"a":1}' });
    expect(parseSseBlock(': keep-alive\ndata: x\ndata: y')).toEqual({
      event: 'message',
      data: 'x\ny',
    });
    expect(parseSseBlock('event: only')).toBeNull();
    expect(parseSseBlock('data:no-space')).toEqual({ event: 'message', data: 'no-space' });
  });

  it('readSseStream yields events across chunk boundaries and a trailing block', async () => {
    const encoder = new TextEncoder();
    const text =
      sseFrame('sources', { sources: ['a'] }) +
      sseFrame('token', { text: 'x' }) +
      'event: done\ndata: {}';
    const chunks = [text.slice(0, 13), text.slice(13, 40), text.slice(40)];
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        for (const chunk of chunks) c.enqueue(encoder.encode(chunk));
        c.close();
      },
    });
    const events = await collect(stream);
    expect(events.map((e) => e.event)).toEqual(['sources', 'token', 'done']);
    expect(JSON.parse(events[1].data)).toEqual({ text: 'x' });
  });

  it('sseStreamFromFrames replays frames in order', async () => {
    const events = await collect(sseStreamFromFrames([sseFrame('a', 1), sseFrame('b', 2)]));
    expect(events).toEqual([
      { event: 'a', data: '1' },
      { event: 'b', data: '2' },
    ]);
  });
});

describe('line breaks are CR, LF and CRLF — and nothing else', () => {
  const frame = (event: string, data: string, eol: string) =>
    `event: ${event}${eol}data: ${data}${eol}${eol}`;

  it.each([
    ['LF', '\n'],
    ['CRLF', '\r\n'],
    ['CR', '\r'],
  ])('parses frames that use %s', async (_name, eol) => {
    const events = await collect(streamOf(frame('a', '1', eol) + frame('b', '2', eol)));
    expect(events).toEqual([
      { event: 'a', data: '1' },
      { event: 'b', data: '2' },
    ]);
  });

  it('parses a block that mixes the three', () => {
    expect(parseSseBlock('event: a\r\ndata: x\rdata: y\nevent: b')).toEqual({
      event: 'b',
      data: 'x\ny',
    });
  });

  it('keeps a CRLF together when the chunks split it between the CR and the LF', async () => {
    const whole = frame('a', '1', '\r\n') + frame('b', '2', '\r\n');
    for (let cut = 1; cut < whole.length; cut += 1) {
      const events = await collect(streamOf(whole.slice(0, cut), whole.slice(cut)));
      expect(events, `cut at ${cut}`).toEqual([
        { event: 'a', data: '1' },
        { event: 'b', data: '2' },
      ]);
    }
  });

  it('a CR that ends the stream still ends its line', async () => {
    const events = await collect(streamOf('event: a\rdata: 1\r\r'));
    expect(events).toEqual([{ event: 'a', data: '1' }]);
  });

  it('does not split on U+2028, U+2029 or U+0085, in either helper', async () => {
    const data = `{"text":"a${LS}event: done${LS}data: {}${PS}b${NEL}c"}`;
    const block = `event: token\ndata: ${data}`;
    expect(parseSseBlock(block)).toEqual({ event: 'token', data });
    const events = await collect(streamOf(block + '\n\n'));
    expect(events).toEqual([{ event: 'token', data }]);
  });

  it('keeps trailing spaces in a value and strips only one leading space', () => {
    expect(parseSseBlock('data:  two  ')).toEqual({ event: 'message', data: ' two  ' });
  });

  it('decodes a multi-byte character split across chunks', async () => {
    const bytes = new TextEncoder().encode('event: t\ndata: "é😀"\n\n');
    const events = await collect(
      streamOf(bytes.slice(0, 15), bytes.slice(15, 19), bytes.slice(19)),
    );
    expect(JSON.parse(events[0].data)).toBe('é😀');
  });
});

describe('an event that never ends', () => {
  it('is refused once it passes the size limit', async () => {
    const chunk = 'x'.repeat(10_000);
    const stream = streamOf(
      ...Array.from({ length: MAX_SSE_EVENT_CHARS / 10_000 + 2 }, () => chunk),
    );
    await expect(collect(stream)).rejects.toBeInstanceOf(SseEventTooLargeError);
  });

  it('allows a large but finished event, and counts per event, not per stream', async () => {
    const big = 'y'.repeat(40_000);
    const events = await collect(
      streamOf(`data: ${big}\n\n`, `data: ${big}\n\n`, `data: ${big}\n\n`, `data: ${big}\n\n`),
    );
    expect(events).toHaveLength(4);
  });
});
