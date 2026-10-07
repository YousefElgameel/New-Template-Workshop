import type {
  AskStreamEvent,
  Citation,
  Conversation,
  ConversationAskResponse,
  ConversationDetail,
  ConversationList,
} from '@/lib/conversation-types';
import { sseFrame } from '@/lib/sse';

/**
 * Fixtures for the conversation surface (`/api/v1/conversations…`) — web roadmap 1.1.
 *
 * Typed from `@/lib/conversation-types` (generated for create/list/open, hand-declared for the
 * ask response and stream frames until api 4.2 / 4.3). The content mirrors the wireframe
 * (`docs/design/wireframes/chat.md`) so the pages look right against the mocks. Streaming
 * fixtures are plain `string[]` frames: hand them to `mockSse(frames, traceId, delayMs)` to pace
 * them like the real stream.
 */

/** Fixed ids so tests and pages can refer to a fixture by name. */
export const conversationIds = {
  tracing: '5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e01',
  errors: '5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e02',
  onboarding: '5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e03',
  created: '5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e99',
} as const;

// The assistant indexes `docs/**/*.md` only, so real citations are `docs/…` paths.
const CITE_TRACE: Citation = {
  title: 'Observability architecture',
  path: 'docs/architecture/observability.md',
};
const CITE_OVERVIEW: Citation = {
  title: 'Architecture overview',
  path: 'docs/architecture/overview.md',
};
const CITE_ERRORS: Citation = { title: 'HTTP API reference', path: 'docs/reference/http-api.md' };

/** No conversations yet — the "No chats yet" state. */
export const emptyConversationList: ConversationList = { items: [], count: 0 };

/**
 * Three conversations, newest first by `updated_at` (as the api returns them). The api bumps
 * `updated_at` to the time of the last message appended, so for a conversation with messages
 * it equals that message's `created_at`; with none it equals `created_at`.
 */
export const conversationListFixture: ConversationList = {
  items: [
    {
      id: conversationIds.tracing,
      title: 'How does trace_id propagation work?',
      updated_at: '2026-10-06T10:12:03.400Z',
    },
    {
      id: conversationIds.errors,
      title: 'Error contract',
      updated_at: '2026-10-06T09:38:03.000Z',
    },
    { id: conversationIds.onboarding, title: 'Onboarding', updated_at: '2026-10-05T16:30:00.000Z' },
  ],
  count: 3,
};

/** `POST /api/v1/conversations` → 201: a fresh, untitled conversation. */
export const createdConversationFixture: Conversation = {
  id: conversationIds.created,
  title: 'New conversation',
  created_at: '2026-10-06T10:20:00.000Z',
  updated_at: '2026-10-06T10:20:00.000Z',
};

/** `GET /api/v1/conversations/{id}` for each listed conversation (messages oldest first). */
export const conversationDetails: Record<string, ConversationDetail> = {
  [conversationIds.tracing]: {
    id: conversationIds.tracing,
    title: 'How does trace_id propagation work?',
    created_at: '2026-10-06T10:10:00.000Z',
    updated_at: '2026-10-06T10:12:03.400Z',
    truncated: false,
    messages: [
      {
        id: 'a0000000-0000-4000-8000-000000000001',
        role: 'user',
        content: 'How does trace_id propagation work?',
        citations: null,
        created_at: '2026-10-06T10:10:00.100Z',
      },
      {
        id: 'a0000000-0000-4000-8000-000000000002',
        role: 'assistant',
        content:
          'The trace id is minted by the BFF and forwarded unchanged to the api on every hop. It is echoed in the x-trace-id response header and present on every log line.',
        citations: [CITE_TRACE, CITE_OVERVIEW],
        created_at: '2026-10-06T10:10:04.200Z',
      },
      {
        id: 'a0000000-0000-4000-8000-000000000003',
        role: 'user',
        content: 'And on the api side?',
        citations: null,
        created_at: '2026-10-06T10:12:00.300Z',
      },
      {
        id: 'a0000000-0000-4000-8000-000000000004',
        role: 'assistant',
        content:
          'The api adopts a valid inbound x-trace-id, otherwise it generates one with its own origin, and keeps it in a context variable for the request.',
        citations: [CITE_OVERVIEW],
        created_at: '2026-10-06T10:12:03.400Z',
      },
    ],
  },
  [conversationIds.errors]: {
    id: conversationIds.errors,
    title: 'Error contract',
    created_at: '2026-10-06T09:38:00.000Z',
    updated_at: '2026-10-06T09:38:03.000Z',
    truncated: false,
    messages: [
      {
        id: 'b0000000-0000-4000-8000-000000000001',
        role: 'user',
        content: 'Error contract',
        citations: null,
        created_at: '2026-10-06T09:38:00.100Z',
      },
      {
        id: 'b0000000-0000-4000-8000-000000000002',
        role: 'assistant',
        content: 'Every non-2xx response body is { error, trace_id }; details are never echoed.',
        citations: [CITE_ERRORS],
        created_at: '2026-10-06T09:38:03.000Z',
      },
    ],
  },
  // A conversation that was started but never asked anything.
  [conversationIds.onboarding]: {
    id: conversationIds.onboarding,
    title: 'Onboarding',
    created_at: '2026-10-05T16:30:00.000Z',
    updated_at: '2026-10-05T16:30:00.000Z',
    truncated: false,
    messages: [],
  },
};

/**
 * Whether a mocked ask may target this conversation: a listed one, or the one `POST` creates —
 * so "new chat, then ask" works against the mocks.
 */
export function mockConversationExists(id: string): boolean {
  return Object.hasOwn(conversationDetails, id) || id === conversationIds.created;
}

/** The fixture a mocked `GET /api/v1/conversations/{id}` serves, or undefined (→ 404). */
export function findConversationFixture(id: string): ConversationDetail | undefined {
  return Object.hasOwn(conversationDetails, id) ? conversationDetails[id] : undefined;
}

/** `POST /api/v1/conversations/{id}/ask` → 200. */
export const conversationAskResponse: ConversationAskResponse = {
  message_id: 'c0000000-0000-4000-8000-000000000001',
  answer:
    'The api listens on port 8000 and the web app on port 3000; the browser only talks to the web app.',
  citations: [
    { title: 'Getting started', path: 'docs/getting-started.md' },
    { title: 'Architecture', path: 'docs/architecture/ARCHITECTURE.md' },
  ],
};

/** Typed frame builder: the event name decides which payload shape the compiler accepts. */
function frame<E extends AskStreamEvent['event']>(
  event: E,
  data: Extract<AskStreamEvent, { event: E }>['data'],
): string {
  return sseFrame(event, data);
}

/** A document that was retrieved but that the answer does not cite: only the `sources` frame lists it. */
const retrievedButNotCited: Citation = {
  title: 'HTTP API reference',
  path: 'docs/reference/http-api.md',
};

/** `POST …/ask/stream` — `sources`, then one `token` per word, then `done`. */
export const conversationAskStreamFrames: string[] = [
  frame('sources', {
    sources: [...conversationAskResponse.citations, retrievedButNotCited],
    count: conversationAskResponse.citations.length + 1,
  }),
  ...conversationAskResponse.answer
    .split(' ')
    .map((word, index, words) =>
      frame('token', { text: index < words.length - 1 ? `${word} ` : word }),
    ),
  frame('done', {
    message_id: conversationAskResponse.message_id,
    input_tokens: 42,
    output_tokens: 24,
    citations: conversationAskResponse.citations,
  }),
];

/** A trace id the failure frame carries (api origin `0c70`, env nibble `0`). */
export const failureTraceId = `0c700${'d'.repeat(27)}`;

/**
 * `POST …/ask/stream` that fails after the answer started: `sources`, a few `token`s, then a
 * terminal `error` (no `done`). The UI shows the code and the trace id.
 */
export const conversationAskFailureFrames: string[] = [
  frame('sources', {
    sources: conversationAskResponse.citations,
    count: conversationAskResponse.citations.length,
  }),
  frame('token', { text: 'The api listens ' }),
  frame('token', { text: 'on port ' }),
  frame('error', { error_kind: 'timeout', error: 'ai_unavailable', trace_id: failureTraceId }),
];
