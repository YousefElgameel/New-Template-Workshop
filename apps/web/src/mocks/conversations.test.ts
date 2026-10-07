import { describe, expect, it } from 'vitest';
import type { AskStreamEvent } from '@/lib/conversation-types';
import { mockSse } from '@/lib/mocks';
import { readSseStream, sseStreamFromFrames } from '@/lib/sse';
import {
  conversationAskFailureFrames,
  conversationAskResponse,
  conversationAskStreamFrames,
  conversationDetails,
  conversationIds,
  conversationListFixture,
  createdConversationFixture,
  emptyConversationList,
  failureTraceId,
  findConversationFixture,
} from '@/mocks/conversations';

/**
 * The fixtures are what the web pages are built and tested against until the api is switched on
 * (web roadmap 4.2), so they must keep telling the truth about the contract: same shapes, same
 * ordering rules, same limits as `apps/api`. The type-check covers the shapes; these tests cover
 * the rules a type cannot.
 */

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const UTC_ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/;
const TRACE_ID = /^[0-9a-f]{32}$/;

/** SQL Server's NVARCHAR(200) counts UTF-16 code units — the same measure as `String.length`. */
const TITLE_MAX = 200;

/** The most messages one read of a conversation returns (apps/api `MAX_THREAD_MESSAGES`). */
const MAX_THREAD_MESSAGES = 500;

async function parse(frames: string[]): Promise<AskStreamEvent[]> {
  const events: AskStreamEvent[] = [];
  for await (const raw of readSseStream(sseStreamFromFrames(frames))) {
    events.push({ event: raw.event, data: JSON.parse(raw.data) } as AskStreamEvent);
  }
  return events;
}

describe('conversation list fixtures', () => {
  it('the empty list is the "No chats yet" state', () => {
    expect(emptyConversationList).toEqual({ items: [], count: 0 });
  });

  it('is newest first by updated_at, with a count that matches and unique UUID ids', () => {
    const { items, count } = conversationListFixture;
    expect(count).toBe(items.length);
    expect(items.length).toBeGreaterThanOrEqual(3);
    const times = items.map((item) => Date.parse(item.updated_at));
    expect(times).toEqual([...times].sort((a, b) => b - a));
    expect(new Set(items.map((item) => item.id)).size).toBe(items.length);
    for (const item of items) {
      expect(item.id).toMatch(UUID);
      expect(item.updated_at).toMatch(UTC_ISO);
      expect(item.title.length).toBeGreaterThan(0);
      expect(item.title.length).toBeLessThanOrEqual(TITLE_MAX);
    }
  });

  it('a created conversation starts untitled, with equal timestamps', () => {
    expect(createdConversationFixture.title).toBe('New conversation');
    expect(createdConversationFixture.id).toMatch(UUID);
    expect(createdConversationFixture.created_at).toMatch(UTC_ISO);
    expect(createdConversationFixture.updated_at).toBe(createdConversationFixture.created_at);
    expect(conversationListFixture.items.map((i) => i.id)).not.toContain(
      createdConversationFixture.id,
    );
  });
});

describe('conversation detail fixtures', () => {
  it('has a detail for every listed conversation, agreeing with the list', () => {
    for (const item of conversationListFixture.items) {
      const detail = findConversationFixture(item.id);
      expect(detail, item.title).toBeDefined();
      expect(detail?.title).toBe(item.title);
      expect(detail?.updated_at).toBe(item.updated_at);
    }
  });

  it('an unknown id has no fixture (the route turns that into a 404)', () => {
    expect(findConversationFixture('00000000-0000-4000-8000-000000000000')).toBeUndefined();
    expect(findConversationFixture('not-a-uuid')).toBeUndefined();
  });

  it('messages are oldest first, valid, and only assistant messages carry citations', () => {
    for (const detail of Object.values(conversationDetails)) {
      // truncated is only true for a thread cut at the api's cap of 500 messages
      expect(typeof detail.truncated).toBe('boolean');
      expect(detail.messages.length).toBeLessThanOrEqual(MAX_THREAD_MESSAGES);
      if (detail.truncated) expect(detail.messages.length).toBe(MAX_THREAD_MESSAGES);
      expect(detail.created_at).toMatch(UTC_ISO);
      const times = detail.messages.map((m) => Date.parse(m.created_at));
      expect(times).toEqual([...times].sort((a, b) => a - b));
      for (const message of detail.messages) {
        expect(message.id).toMatch(UUID);
        expect(message.created_at).toMatch(UTC_ISO);
        expect(['user', 'assistant']).toContain(message.role);
        expect(message.content.length).toBeGreaterThan(0);
        if (message.role === 'user') {
          expect(message.citations).toBeNull();
        } else {
          expect(message.citations?.length).toBeGreaterThan(0);
          for (const citation of message.citations ?? []) {
            expect(citation.title.length).toBeGreaterThan(0);
            // the assistant indexes docs/**/*.md only, so a real citation is a docs/… path
            expect(citation.path).toMatch(/^docs\/.+\.md$/);
          }
        }
      }
    }
  });

  it('updated_at is the time of the last message (the api bumps it on every append)', () => {
    for (const detail of Object.values(conversationDetails)) {
      const last = detail.messages.at(-1);
      expect(detail.created_at <= detail.updated_at).toBe(true);
      if (last) {
        expect(Date.parse(detail.updated_at)).toBe(Date.parse(last.created_at));
        expect(Date.parse(detail.messages[0]?.created_at ?? '')).toBeGreaterThanOrEqual(
          Date.parse(detail.created_at),
        );
      } else {
        expect(detail.updated_at).toBe(detail.created_at);
      }
    }
  });

  it('covers the interesting cases: a long thread, a short one and an empty one', () => {
    const lengths = Object.values(conversationDetails).map((d) => d.messages.length);
    expect(Math.max(...lengths)).toBeGreaterThanOrEqual(4);
    expect(lengths).toContain(0);
    expect(conversationDetails[conversationIds.onboarding]?.messages).toEqual([]);
  });

  it('message ids are unique across every fixture', () => {
    const ids = Object.values(conversationDetails).flatMap((d) => d.messages.map((m) => m.id));
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe('ask fixtures', () => {
  it('the JSON answer has an id, text and citations', () => {
    expect(conversationAskResponse.message_id).toMatch(UUID);
    expect(conversationAskResponse.answer.length).toBeGreaterThan(0);
    expect(conversationAskResponse.citations.length).toBeGreaterThan(0);
    for (const citation of conversationAskResponse.citations) {
      expect(citation.path).toMatch(/^docs\/.+\.md$/);
    }
  });

  it('the stream is sources → tokens → done, and the tokens spell the answer', async () => {
    const events = await parse(conversationAskStreamFrames);
    const names = events.map((e) => e.event);
    expect(names[0]).toBe('sources');
    expect(names.at(-1)).toBe('done');
    expect(names.slice(1, -1).every((n) => n === 'token')).toBe(true);
    expect(names.filter((n) => n === 'error')).toEqual([]);

    // `sources` lists every retrieved document — the cited ones and one the answer does not cite
    const sources = events[0];
    const listed = sources?.event === 'sources' ? sources.data : null;
    expect(listed?.sources.slice(0, conversationAskResponse.citations.length)).toEqual(
      conversationAskResponse.citations,
    );
    expect(listed?.sources.length).toBe(conversationAskResponse.citations.length + 1);
    expect(listed?.count).toBe(listed?.sources.length);
    const text = events.flatMap((e) => (e.event === 'token' ? [e.data.text] : [])).join('');
    expect(text).toBe(conversationAskResponse.answer);

    // `done` says which documents the answer cites, and the token counts are numbers or null
    const done = events.at(-1);
    if (done?.event !== 'done') throw new Error('last frame is not done');
    expect(done.data.message_id).toBe(conversationAskResponse.message_id);
    expect(done.data.citations).toEqual(conversationAskResponse.citations);
    expect(done.data.input_tokens === null || Number.isInteger(done.data.input_tokens)).toBe(true);
  });

  it('the failure stream ends with a terminal error frame and never says done', async () => {
    const events = await parse(conversationAskFailureFrames);
    const names = events.map((e) => e.event);
    expect(names.at(-1)).toBe('error');
    expect(names).not.toContain('done');
    expect(names.filter((n) => n === 'error')).toHaveLength(1);
    expect(names).toContain('token'); // it failed AFTER the answer started

    const error = events.at(-1);
    if (error?.event !== 'error') throw new Error('last frame is not an error');
    expect(error.data.trace_id).toBe(failureTraceId);
    expect(error.data.trace_id).toMatch(TRACE_ID);
    expect(error.data.trace_id.startsWith('0c70')).toBe(true); // the api's trace origin
    expect(error.data.error).toBe('ai_unavailable');
    // a bounded kind, never exception text
    expect(error.data.error_kind).toMatch(/^[a-z_]+$/);
  });

  it('plays through mockSse like the existing mocks do, in order, as an event stream', async () => {
    const res = mockSse(conversationAskStreamFrames, 'a'.repeat(32), 0);
    expect(res.headers.get('content-type')).toContain('text/event-stream');
    expect(res.headers.get('x-mock-upstream')).toBe('true');
    expect(await res.text()).toBe(conversationAskStreamFrames.join(''));

    const failing = mockSse(conversationAskFailureFrames, 'a'.repeat(32), 0);
    expect(await failing.text()).toBe(conversationAskFailureFrames.join(''));
  });
});
