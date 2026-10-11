import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it, vi } from 'vitest';

import type {
  AskStreamEvent,
  DoneFrame,
  ErrorFrame,
  SourcesFrame,
  TokenFrame,
} from '@/lib/conversation-types';
import { parseSseBlock } from '@/lib/sse';
import {
  conversationAskFailureFrames,
  conversationAskStreamFrames,
  failureTraceId,
} from '@/mocks/conversations';

/**
 * The stream frames are the one part of the conversation contract OpenAPI cannot describe (the
 * route is a single `text/event-stream` string), so they are pinned here instead: the frame table
 * in `docs/reference/http-api.md`, the declared types, the fixtures and the client must all name
 * the same field **names**, and the client turns them into the right handler calls. A field added,
 * removed or renamed on one side only fails this file; value types and the `error_kind` words are
 * not compared, and the compile-time half (the `declared` keys) runs in `pnpm typecheck` (CI), not in Vitest.
 */

const repo = (path: string) => fileURLToPath(new URL(`../../../../${path}`, import.meta.url));
const read = (path: string) => readFileSync(repo(path), 'utf8');

const EVENTS = ['sources', 'token', 'done', 'error'] as const;
type EventName = (typeof EVENTS)[number];

/** Top-level keys of a `{ "a", "b": …, "c": [{ "x" }] }` cell: `a`, `b`, `c` (not `x`). */
function topLevelKeys(cell: string): string[] {
  const keys: string[] = [];
  let depth = 0;
  let previous = ''; // the last non-space character outside a string
  for (let i = 0; i < cell.length; i++) {
    const ch = cell[i];
    if (ch === '"') {
      const end = cell.indexOf('"', i + 1);
      // a key follows `{` or `,`; a string after `:` is a value (`{ "text": "partial " }`)
      if (depth === 1 && previous !== ':') keys.push(cell.slice(i + 1, end));
      i = end;
      previous = '"';
      continue;
    }
    if (ch === '{' || ch === '[') depth++;
    else if (ch === '}' || ch === ']') depth--;
    if (ch !== ' ') previous = ch;
  }
  return keys;
}

/** The keys the frame table documents for each event (`| \`done\` | \`{ "message_id", … }\` | … |`). */
function documentedKeys(): Record<EventName, string[]> {
  const doc = read('docs/reference/http-api.md');
  const section = doc.slice(doc.indexOf('### `POST /v1/conversations/{id}/ask/stream`'));
  const out = {} as Record<EventName, string[]>;
  for (const name of EVENTS) {
    const row = section.split('\n').find((line) => line.startsWith(`| \`${name}\``));
    if (!row) throw new Error(`no row for the ${name} frame in docs/reference/http-api.md`);
    const cell = row.split('|')[2];
    out[name] = topLevelKeys(cell);
  }
  return out;
}

/**
 * One object per frame type whose keys the compiler forces to be exactly the type's keys: a
 * missing one is an error, and so is an extra one (excess property check).
 */
const declared: {
  [E in EventName]: Record<keyof Extract<AskStreamEvent, { event: E }>['data'], true>;
} = {
  sources: { sources: true, count: true } satisfies Record<keyof SourcesFrame, true>,
  token: { text: true } satisfies Record<keyof TokenFrame, true>,
  done: {
    message_id: true,
    input_tokens: true,
    output_tokens: true,
    citations: true,
  } satisfies Record<keyof DoneFrame, true>,
  error: { error_kind: true, error: true, trace_id: true } satisfies Record<keyof ErrorFrame, true>,
};

const sorted = (keys: string[]) => [...keys].sort();

describe('the stream frame contract', () => {
  const documented = documentedKeys();

  it.each(EVENTS)('the %s frame: the declared type names the documented fields', (name) => {
    expect(sorted(Object.keys(declared[name]))).toEqual(sorted(documented[name]));
  });

  it('documents all four events and nothing else', () => {
    expect(Object.keys(documented).sort()).toEqual([...EVENTS].sort());
    for (const name of EVENTS) expect(documented[name].length).toBeGreaterThan(0);
  });

  it('the fixtures send exactly the documented fields, in the documented order of events', () => {
    for (const frames of [conversationAskStreamFrames, conversationAskFailureFrames]) {
      const events = frames.map((raw) => parseSseBlock(raw.trim()));
      expect(events.every((e) => e !== null)).toBe(true);
      for (const event of events) {
        const name = event!.event as EventName;
        expect(EVENTS).toContain(name);
        const keys = Object.keys(JSON.parse(event!.data) as object);
        expect(sorted(keys)).toEqual(sorted(documented[name]));
      }
      const names = events.map((e) => e!.event);
      expect(names[0]).toBe('sources'); // retrieval first
      expect(['done', 'error']).toContain(names.at(-1)); // exactly one terminal frame, last
      expect(names.filter((n) => n === 'done' || n === 'error')).toHaveLength(1);
    }
  });

  it('the client turns the documented frames into the right handler calls', async () => {
    const { askStream } = await import('@/lib/chat-client');
    const { sseStreamFromFrames } = await import('@/lib/sse');
    const { conversationAskResponse } = await import('@/mocks/conversations');

    const calls = {
      sources: [] as unknown[],
      tokens: [] as string[],
      done: [] as unknown[],
      error: [] as unknown[],
    };
    const stream = (frames: string[]) =>
      vi.fn(
        async () =>
          new Response(sseStreamFromFrames(frames), {
            status: 200,
            headers: { 'content-type': 'text/event-stream' },
          }),
      );
    const handlers = {
      onSources: (c: unknown) => calls.sources.push(c),
      onToken: (t: string) => calls.tokens.push(t),
      onDone: (d: unknown) => calls.done.push(d),
      onError: (e: unknown) => calls.error.push(e),
    };

    // the success fixture: every documented field of sources, token and done is picked up
    await askStream('/api/v1/conversations/x/ask/stream', 'q', handlers, {
      fetchImpl: stream(conversationAskStreamFrames),
    });
    expect(calls.sources[0]).toHaveLength(conversationAskResponse.citations.length + 1);
    expect(calls.tokens.join('')).toBe(conversationAskResponse.answer);
    expect(calls.done).toEqual([
      {
        messageId: conversationAskResponse.message_id,
        citations: conversationAskResponse.citations,
        inputTokens: 42,
        outputTokens: 24,
      },
    ]);
    expect(calls.error).toEqual([]);

    // the failure fixture: error_kind, error and trace_id reach the failure; no done follows
    calls.done.length = 0;
    await askStream('/api/v1/conversations/x/ask/stream', 'q', handlers, {
      fetchImpl: stream(conversationAskFailureFrames),
    });
    expect(calls.done).toEqual([]);
    expect(calls.error).toHaveLength(1);
    expect(calls.error[0]).toMatchObject({ traceId: failureTraceId });
    const failureJson = JSON.parse(
      parseSseBlock(conversationAskFailureFrames.at(-1)!.trim())!.data,
    ) as ErrorFrame;
    expect(calls.error[0]).toMatchObject({ kind: failureJson.error_kind, code: failureJson.error });
  });
});
