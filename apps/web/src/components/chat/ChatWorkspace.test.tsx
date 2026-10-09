// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sseFrame, sseStreamFromFrames } from '@/lib/sse';
import {
  conversationDetails,
  conversationIds,
  conversationListFixture,
  conversationAskFailureFrames,
  conversationAskResponse,
  conversationAskStreamFrames,
  createdConversationFixture,
  failureTraceId,
  emptyConversationList,
} from '@/mocks/conversations';
import { ChatWorkspace } from './ChatWorkspace';

const TRACE = '0eb01bbbbbbbbbbbbbbbbbbbbbbbbbbb';

type Handler = () => Response | Promise<Response>;

/** Stub the same-origin BFF: keys are `METHOD /path`. Anything else is a test bug. */
function stubFetch(routes: Record<string, Handler>) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const key = `${init?.method ?? 'GET'} ${String(input)}`;
    const handler = routes[key];
    if (!handler) throw new Error(`unexpected request: ${key}`);
    return handler();
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

const ok =
  (body: unknown, status = 200): Handler =>
  () =>
    Response.json(body, { status });
const fail =
  (error: string, status = 502): Handler =>
  () =>
    Response.json({ error, trace_id: TRACE }, { status });

const LIST = 'GET /api/v1/conversations';
const CREATE = 'POST /api/v1/conversations';
const open = (id: string) => `GET /api/v1/conversations/${id}`;

function deferred() {
  let resolve!: (r: Response) => void;
  const promise = new Promise<Response>((r) => (resolve = r));
  return { promise, resolve };
}

const NOTICE = 'Chats are shared with the team. Do not enter personal data.';

const rowsOf = () =>
  within(screen.getByRole('navigation', { name: 'Conversations' })).getAllByRole('listitem');

describe('<ChatWorkspace />', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('shows a loading state, then the conversations', async () => {
    const gate = deferred();
    stubFetch({ [LIST]: () => gate.promise });
    render(<ChatWorkspace />);
    expect(screen.getByText('Loading conversations…')).toBeInTheDocument();
    expect(screen.queryByTestId('conversations-empty')).not.toBeInTheDocument();
    gate.resolve(Response.json(conversationListFixture));
    expect(await screen.findByRole('button', { name: /Error contract/ })).toBeInTheDocument();
    expect(screen.queryByText('Loading conversations…')).not.toBeInTheDocument();
    expect(screen.getByTestId('thread-idle')).toBeInTheDocument();
  });

  it('shows "No chats yet" when there are none', async () => {
    stubFetch({ [LIST]: ok(emptyConversationList) });
    render(<ChatWorkspace />);
    expect(await screen.findByTestId('conversations-empty')).toHaveTextContent('No chats yet');
  });

  it('shows the error code and trace_id when the list fails, and Retry loads it', async () => {
    let healthy = false;
    stubFetch({
      [LIST]: () => (healthy ? Response.json(conversationListFixture) : fail('upstream_error')()),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load conversations (upstream_error)');
    expect(screen.queryByTestId('conversations-empty')).not.toBeInTheDocument();
    expect(alert).toHaveTextContent(TRACE);

    healthy = true;
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('button', { name: /Onboarding/ })).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('omits the trace id when the network is down', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => Promise.reject(new TypeError('offline'))),
    );
    render(<ChatWorkspace />);
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('network_error');
    expect(alert).not.toHaveTextContent('Trace ID');
  });

  it('opens a conversation: loading, then its messages with their citations', async () => {
    const gate = deferred();
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.tracing)]: () => gate.promise,
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /How does trace_id/ }));
    expect(screen.getByText('Loading conversation…')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /How does trace_id/ })).toHaveAttribute(
      'aria-current',
      'true',
    );

    gate.resolve(Response.json(conversationDetails[conversationIds.tracing]));
    const thread = await screen.findByRole('list', { name: 'Conversation' });
    expect(within(thread).getByText(/minted by the BFF/)).toBeInTheDocument();
    const sources = within(thread).getAllByRole('list', { name: 'Sources' });
    // each cited document shows its title and, beside it, its path (the web 3.2 Sources block)
    expect(sources[0]).toHaveTextContent('Observability architecture');
    expect(sources[0]).toHaveTextContent('docs/architecture/observability.md');
    expect(sources[0]).toHaveTextContent('Architecture overview');
    expect(sources[0]).toHaveTextContent('docs/architecture/overview.md');
  });

  it('shows the error line with the trace_id when opening fails, and recovers on the next open', async () => {
    let broken = true;
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.errors)]: () =>
        broken
          ? fail('not_found', 404)()
          : Response.json(conversationDetails[conversationIds.errors]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Error contract/ }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not load this conversation (not_found)');
    expect(alert).toHaveTextContent(TRACE);

    broken = false;
    await user.click(screen.getByRole('button', { name: /Error contract/ }));
    expect(await screen.findByText(/Every non-2xx response body/)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('latest click wins: a slow earlier answer never replaces the selected conversation', async () => {
    const slow = deferred();
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.tracing)]: () => slow.promise,
      [open(conversationIds.errors)]: ok(conversationDetails[conversationIds.errors]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /How does trace_id/ }));
    await user.click(screen.getByRole('button', { name: /Error contract/ }));
    expect(await screen.findByText(/Every non-2xx response body/)).toBeInTheDocument();

    slow.resolve(Response.json(conversationDetails[conversationIds.tracing]));
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByText(/minted by the BFF/)).not.toBeInTheDocument();
    expect(screen.getByText(/Every non-2xx response body/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Error contract/ })).toHaveAttribute(
      'aria-current',
      'true',
    );
  });

  it('+ New chat creates a conversation, puts it first, selects it and focuses the input', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [CREATE]: ok(createdConversationFixture, 201),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.click(screen.getByRole('button', { name: '+ New chat' }));

    await screen.findByTestId('chat-empty');
    const rows = rowsOf();
    expect(rows).toHaveLength(4);
    expect(rows[0]).toHaveTextContent('New conversation');
    expect(within(rows[0]).getByRole('button')).toHaveAttribute('aria-current', 'true');
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveFocus();
  });

  it('+ New chat shows the error and trace_id when creation fails, and changes nothing else', async () => {
    stubFetch({ [LIST]: ok(conversationListFixture), [CREATE]: fail('upstream_unreachable') });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Could not start a new chat (upstream_unreachable)');
    expect(alert).toHaveTextContent(TRACE);
    expect(rowsOf()).toHaveLength(3);
    expect(screen.getByTestId('thread-idle')).toBeInTheDocument();
  });

  it('works from an empty list: New chat creates the first conversation', async () => {
    stubFetch({
      [LIST]: ok(emptyConversationList),
      [CREATE]: ok(createdConversationFixture, 201),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByTestId('conversations-empty');
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    expect(await screen.findByRole('button', { name: /New conversation/ })).toBeInTheDocument();
    expect(screen.queryByTestId('conversations-empty')).not.toBeInTheDocument();
  });

  it('a chat created while the list is still loading survives the list arriving', async () => {
    const gate = deferred();
    stubFetch({ [LIST]: () => gate.promise, [CREATE]: ok(createdConversationFixture, 201) });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    await screen.findByRole('button', { name: /New conversation/ });
    gate.resolve(Response.json(conversationListFixture));
    await screen.findByRole('button', { name: /Onboarding/ });
    expect(rowsOf()).toHaveLength(4);
  });

  it('a double click on + New chat creates one conversation', async () => {
    const gate = deferred();
    const fetchMock = stubFetch({
      [LIST]: ok(emptyConversationList),
      [CREATE]: () => gate.promise,
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByTestId('conversations-empty');
    const button = screen.getByRole('button', { name: '+ New chat' });
    await user.click(button);
    await user.click(button);
    gate.resolve(Response.json(createdConversationFixture, { status: 201 }));
    await screen.findByRole('button', { name: /New conversation/ });
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  });

  it('opening a conversation while a new chat is selected replaces the thread', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [CREATE]: ok(createdConversationFixture, 201),
      [open(conversationIds.errors)]: ok(conversationDetails[conversationIds.errors]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    await screen.findByTestId('chat-empty');
    await user.click(screen.getByRole('button', { name: /Error contract/ }));
    expect(await screen.findByText(/Every non-2xx response body/)).toBeInTheDocument();
    expect(screen.queryByTestId('chat-empty')).not.toBeInTheDocument();
  });

  it('shows a conversation with no messages as an invitation to ask', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.onboarding)]: ok(conversationDetails[conversationIds.onboarding]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Onboarding/ }));
    expect(await screen.findByTestId('chat-empty')).toBeInTheDocument();
  });

  it('says when a thread was cut to the most recent 500 messages', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.errors)]: ok({
        ...conversationDetails[conversationIds.errors],
        truncated: true,
      }),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Error contract/ }));
    expect(await screen.findByText('Showing the most recent 500 messages.')).toBeInTheDocument();
  });

  it('renders message text and citation paths as text, never as markup', async () => {
    const evil = '<img src=x onerror=alert(1)>';
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.errors)]: ok({
        ...conversationDetails[conversationIds.errors],
        messages: [
          {
            id: 'c0000000-0000-4000-8000-000000000001',
            role: 'assistant',
            content: evil,
            citations: [{ title: evil, path: `docs/${evil}.md` }],
            created_at: '2026-10-06T09:38:03.000Z',
          },
        ],
      }),
    });
    const user = userEvent.setup();
    const { container } = render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Error contract/ }));
    // the hostile string is the message text and, in the Sources block, a title: text both times
    expect((await screen.findAllByText(evil)).length).toBeGreaterThanOrEqual(2);
    expect(container.querySelector('img')).toBeNull();
  });

  it('lists each cited path once, even when two citations share it', async () => {
    const cite = { title: 'A', path: 'docs/a.md' };
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.errors)]: ok({
        ...conversationDetails[conversationIds.errors],
        messages: [
          {
            id: 'c0000000-0000-4000-8000-000000000002',
            role: 'assistant',
            content: 'answer',
            citations: [cite, { ...cite, title: 'A again' }],
            created_at: '2026-10-06T09:38:03.000Z',
          },
        ],
      }),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Error contract/ }));
    const sources = await screen.findByRole('list', { name: 'Sources' });
    expect(within(sources).getAllByRole('listitem')).toHaveLength(1);
  });

  it('the narrow-screen button opens and closes the drawer, and choosing a chat closes it', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.errors)]: ok(conversationDetails[conversationIds.errors]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Error contract/ });
    const toggle = screen.getByRole('button', { name: 'Show conversations' });
    const drawer = document.getElementById('conversation-drawer')!;
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', 'conversation-drawer');
    expect(drawer).toHaveClass('hidden', 'md:flex');

    await user.click(toggle);
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(drawer).toHaveClass('flex');
    expect(drawer).not.toHaveClass('hidden');

    await user.click(screen.getByRole('button', { name: /Error contract/ }));
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await waitFor(() => expect(drawer).toHaveClass('hidden'));
  });

  it('keeps the columns bounded so the list and the thread scroll inside the page', async () => {
    stubFetch({ [LIST]: ok(conversationListFixture) });
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Error contract/ });
    expect(document.getElementById('conversation-drawer')).toHaveClass('md:w-72', 'flex-col');
    expect(screen.getByRole('region', { name: 'Chat' })).toHaveClass('min-h-0', 'min-w-0');
  });

  it('only talks to the same-origin BFF routes', async () => {
    const fetchMock = stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.errors)]: ok(conversationDetails[conversationIds.errors]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Error contract/ }));
    await screen.findByText(/Every non-2xx response body/);
    for (const [url] of fetchMock.mock.calls) {
      expect(String(url)).toMatch(/^\/api\/v1\/conversations/);
    }
  });

  it.each([[{ items: 'nope' }], [['x']]])(
    'a 200 list reply of the wrong shape (%j) is an error, not an endless spinner',
    async (body) => {
      stubFetch({ [LIST]: ok(body) });
      render(<ChatWorkspace />);
      expect(await screen.findByRole('alert')).toHaveTextContent('invalid_response');
      expect(screen.queryByText('Loading conversations…')).not.toBeInTheDocument();
    },
  );

  it('a 200 thread reply of the wrong shape is an error, not an endless spinner', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.errors)]: ok({ id: 'x' }),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Error contract/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('invalid_response');
    expect(screen.queryByText('Loading conversation…')).not.toBeInTheDocument();
  });

  it('a click on a row made while New chat is in flight wins over the new chat', async () => {
    const gate = deferred();
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [CREATE]: () => gate.promise,
      [open(conversationIds.errors)]: ok(conversationDetails[conversationIds.errors]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    await user.click(screen.getByRole('button', { name: /Error contract/ }));
    await screen.findByText(/Every non-2xx response body/);
    gate.resolve(Response.json(createdConversationFixture, { status: 201 }));
    await waitFor(() => expect(rowsOf()).toHaveLength(4));
    expect(screen.getByRole('button', { name: /Error contract/ })).toHaveAttribute(
      'aria-current',
      'true',
    );
    expect(screen.getByText(/Every non-2xx response body/)).toBeInTheDocument();
  });

  it('clears the "could not start a new chat" line once another conversation is opened', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [CREATE]: fail('upstream_error'),
      [open(conversationIds.errors)]: ok(conversationDetails[conversationIds.errors]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    await screen.findByText(/Could not start a new chat/);
    await user.click(screen.getByRole('button', { name: /Error contract/ }));
    await screen.findByText(/Every non-2xx response body/);
    expect(screen.queryByText(/Could not start a new chat/)).not.toBeInTheDocument();
  });

  it('moves focus to the chat region when a row is chosen, so it is not lost with the drawer', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.errors)]: ok(conversationDetails[conversationIds.errors]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Error contract/ }));
    expect(screen.getByRole('region', { name: 'Chat' })).toHaveFocus();
  });
});

const stream = (id: string) => `POST /api/v1/conversations/${id}/ask/stream`;

/** A finished event stream, as the BFF would send it. */
const sse =
  (frames: string[]): Handler =>
  () =>
    new Response(sseStreamFromFrames(frames), {
      status: 200,
      headers: { 'content-type': 'text/event-stream' },
    });

/** An event stream the test pushes frames into, one at a time. */
function liveStream() {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    handler: (): Response =>
      new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    push: (frame: string) => controller.enqueue(encoder.encode(frame)),
    end: () => controller.close(),
  };
}

const NEW_ID = createdConversationFixture.id;
const SOURCES = [
  { title: 'Architecture overview', path: 'docs/architecture/overview.md' },
  { title: 'HTTP API reference', path: 'docs/reference/http-api.md' },
];
const DONE = (citations = SOURCES.slice(0, 1)) =>
  sseFrame('done', {
    message_id: 'c0000000-0000-4000-8000-000000000009',
    input_tokens: 3,
    output_tokens: 2,
    citations,
  });

describe('<ChatWorkspace /> — sending a question', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  async function openTracing(routes: Record<string, Handler>) {
    const fetchMock = stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.tracing)]: ok(conversationDetails[conversationIds.tracing]),
      ...routes,
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /How does trace_id/ }));
    await screen.findByText(/The trace id is minted/);
    return { user, fetchMock, box: screen.getByRole('textbox', { name: 'Message' }) };
  }

  it('shows the question at once, streams the answer and the sources, and ends with the cited ones', async () => {
    const live = liveStream();
    const { user, box, fetchMock } = await openTracing({
      [stream(conversationIds.tracing)]: live.handler,
    });

    await user.type(box, 'Which port?{Enter}');
    // Browsers drop the focus of a textarea that becomes disabled; put it somewhere else to say so.
    screen.getByRole('region', { name: 'Chat' }).focus();
    expect(screen.getByRole('textbox', { name: 'Message' })).not.toHaveFocus();
    // The question is in the thread before any frame arrives; Send waits.
    expect(await screen.findByText('Which port?')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    expect(screen.getByLabelText('Streaming')).toBeInTheDocument();

    const [url, init] = fetchMock.mock.calls.at(-1) as unknown as [string, RequestInit];
    expect(url).toBe(`/api/v1/conversations/${conversationIds.tracing}/ask/stream`);
    expect(JSON.parse(String(init.body))).toEqual({ question: 'Which port?' });

    // Sources show as soon as their frame arrives — before any token — every retrieved document.
    live.push(sseFrame('sources', { sources: SOURCES, count: 2 }));
    await waitFor(() => {
      const lists = screen.getAllByRole('list', { name: 'Sources' });
      expect(within(lists.at(-1)!).getAllByRole('listitem')).toHaveLength(2);
    });

    live.push(sseFrame('token', { text: 'Port ' }));
    live.push(sseFrame('token', { text: '8000 [1].' }));
    expect(await screen.findByText('Port 8000 [1].')).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeDisabled();

    live.push(DONE());
    live.end();
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled());
    expect(screen.queryByLabelText('Streaming')).not.toBeInTheDocument();
    // `done` replaces the early list with the documents the answer cites.
    const final = screen.getAllByRole('list', { name: 'Sources' }).at(-1)!;
    expect(within(final).getAllByRole('listitem')).toHaveLength(1);
    expect(final).toHaveTextContent('Architecture overview');
    expect(final).not.toHaveTextContent('HTTP API reference');
    // Focus goes back to the input, so the next question can be typed straight away.
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toHaveFocus());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('keeps the shared-chats notice in view while an answer streams', async () => {
    const live = liveStream();
    const { user, box } = await openTracing({ [stream(conversationIds.tracing)]: live.handler });
    await user.type(box, 'Which port?{Enter}');
    expect(await screen.findByLabelText('Streaming')).toBeInTheDocument();
    expect(screen.getByTestId('shared-chats-notice')).toHaveTextContent(NOTICE);
    live.push(DONE());
    live.end();
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled());
    expect(screen.getByTestId('shared-chats-notice')).toHaveTextContent(NOTICE);
  });

  it('moves a titled conversation to the top of the list when its answer is done, keeping its title', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.errors)]: ok(conversationDetails[conversationIds.errors]),
      [stream(conversationIds.errors)]: sse([sseFrame('token', { text: 'a' }), DONE([])]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Error contract/ }));
    await screen.findByText(/Every non-2xx response body/);
    expect(rowsOf()[0]).toHaveTextContent('How does trace_id');

    await user.type(
      screen.getByRole('textbox', { name: 'Message' }),
      'something else entirely{Enter}',
    );
    await waitFor(() => expect(rowsOf()[0]).toHaveTextContent('Error contract'));
    expect(rowsOf()[1]).toHaveTextContent('How does trace_id');
    expect(rowsOf()).toHaveLength(3);
  });

  it('takes the first question as the title of an untitled conversation', async () => {
    stubFetch({
      [LIST]: ok({
        items: [
          {
            id: createdConversationFixture.id,
            title: 'New conversation',
            updated_at: '2026-10-06T10:20:00.000Z',
          },
          ...conversationListFixture.items,
        ],
        count: 4,
      }),
      [open(NEW_ID)]: ok({
        ...createdConversationFixture,
        truncated: false,
        messages: [],
      }),
      [stream(NEW_ID)]: sse([sseFrame('token', { text: 'Hi' }), DONE([])]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /New conversation/ }));
    await user.type(
      await screen.findByRole('textbox', { name: 'Message' }),
      '  What   is\nthe port?{Enter}',
    );
    await waitFor(() => expect(rowsOf()[0]).toHaveTextContent('What is the port?'));
    expect(rowsOf()).toHaveLength(4);
    // A conversation that already has a title keeps it.
    expect(
      within(screen.getByRole('navigation', { name: 'Conversations' })).queryByText(
        'New conversation',
      ),
    ).toBeNull();
  });

  it('creates a conversation first when none is open, then asks in it', async () => {
    const fetchMock = stubFetch({
      [LIST]: ok(conversationListFixture),
      [CREATE]: ok(createdConversationFixture, 201),
      [stream(NEW_ID)]: sse([
        sseFrame('sources', { sources: SOURCES, count: 2 }),
        sseFrame('token', { text: 'Hello' }),
        DONE(),
      ]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'First question{Enter}');

    expect(await screen.findByText('Hello')).toBeInTheDocument();
    const calls = fetchMock.mock.calls.map(
      ([u, i]) => `${(i as RequestInit | undefined)?.method ?? 'GET'} ${String(u)}`,
    );
    expect(calls.indexOf(CREATE)).toBeGreaterThan(-1);
    expect(calls.indexOf(CREATE)).toBeLessThan(calls.indexOf(stream(NEW_ID)));
    await waitFor(() => expect(rowsOf()[0]).toHaveTextContent('First question'));
    expect(
      within(screen.getByRole('region', { name: 'Chat' })).getByText('First question'),
    ).toBeInTheDocument();
  });

  it('shows the error code and trace id under the question when the stream fails, and lets it be retried', async () => {
    let attempt = 0;
    const { user, box } = await openTracing({
      [stream(conversationIds.tracing)]: () =>
        new Response(
          sseStreamFromFrames(
            attempt++ === 0 ? conversationAskFailureFrames : conversationAskStreamFrames,
          ),
          { status: 200, headers: { 'content-type': 'text/event-stream' } },
        ),
    });

    await user.type(box, 'Which port?{Enter}');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('timeout');
    expect(alert).toHaveTextContent(failureTraceId);
    // The half answer is not kept as if it were one; the input works again.
    expect(screen.queryByText(/The api listens on port/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Streaming')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled();

    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText(conversationAskResponse.answer)).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    // One question in the thread, not two.
    expect(screen.getAllByText('Which port?')).toHaveLength(1);
  });

  it.each([
    ['429 busy', 'busy', 429],
    ['503 ai_unavailable', 'ai_unavailable', 503],
    ['404 not_found', 'not_found', 404],
  ])('shows what a refused request says: %s', async (_name, error, status) => {
    const { user, box } = await openTracing({
      [stream(conversationIds.tracing)]: fail(error, status),
    });
    await user.type(box, 'q{Enter}');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(error);
    expect(alert).toHaveTextContent(TRACE);
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled();
  });

  it('puts the error under the question when the conversation cannot be created, and retry creates it', async () => {
    let creates = 0;
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [CREATE]: () =>
        creates++ === 0
          ? Response.json({ error: 'upstream_unreachable', trace_id: TRACE }, { status: 502 })
          : Response.json(createdConversationFixture, { status: 201 }),
      [stream(NEW_ID)]: sse([sseFrame('token', { text: 'Fine' }), DONE([])]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'hello{Enter}');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('upstream_unreachable');
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Fine')).toBeInTheDocument();
    expect(creates).toBe(2);
  });

  it('shows a counter, refuses an empty question and one over 4,000 characters', async () => {
    const { user, box } = await openTracing({});
    expect(screen.getByText('0 / 4,000')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    await user.type(box, '   ');
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();

    await user.clear(box);
    await user.click(box);
    await user.paste('x'.repeat(4000));
    expect(screen.getByText('4,000 / 4,000')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();

    await user.paste('x');
    expect(screen.getByText(/4,001 \/ 4,000/)).toHaveTextContent('too long');
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    expect(box).toHaveAttribute('aria-invalid', 'true');
    await user.keyboard('{Enter}');
    // Nothing was sent.
    expect(screen.queryByLabelText('Streaming')).not.toBeInTheDocument();
  });

  it('counts characters like the api does: an emoji is one', async () => {
    const { user, box } = await openTracing({});
    await user.click(box);
    await user.paste(String.fromCodePoint(0x1f600).repeat(4000));
    expect(screen.getByText('4,000 / 4,000')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
  });

  it('ends the answer when another conversation is opened: late frames are not applied', async () => {
    const rowsBefore = conversationListFixture.items.map((i) => i.title);
    const live = liveStream();
    let signal: AbortSignal | undefined;
    const { user, box } = await openTracing({
      [stream(conversationIds.tracing)]: live.handler,
      [open(conversationIds.errors)]: ok(conversationDetails[conversationIds.errors]),
    });
    const baseFetch = globalThis.fetch;
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/ask/stream')) signal = init?.signal ?? undefined;
      return baseFetch(input, init);
    });

    await user.type(box, 'q{Enter}');
    await screen.findByLabelText('Streaming');
    live.push(sseFrame('token', { text: 'EARLY' }));
    await screen.findByText('EARLY');

    await user.click(screen.getByRole('button', { name: /Error contract/ }));
    await screen.findByText(/Every non-2xx response body/);
    expect(signal?.aborted).toBe(true);
    live.push(sseFrame('token', { text: 'LATE-TOKEN' }));
    live.push(DONE());
    live.end();
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText(/LATE-TOKEN/)).not.toBeInTheDocument();
    expect(screen.queryByText('EARLY')).not.toBeInTheDocument();
    // A late `done` must not move or retitle a row either.
    expect(rowsOf().map((r) => r.textContent ?? '')).toEqual(
      rowsBefore.map((title) => expect.stringContaining(title)),
    );
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('ends the answer when the page is left', async () => {
    const live = liveStream();
    const { user, box } = await openTracing({ [stream(conversationIds.tracing)]: live.handler });
    const baseFetch = globalThis.fetch;
    let signal: AbortSignal | undefined;
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/ask/stream')) signal = init?.signal ?? undefined;
      return baseFetch(input, init);
    });
    await user.type(box, 'q{Enter}');
    await screen.findByLabelText('Streaming');
    cleanup();
    expect(signal?.aborted).toBe(true);
  });

  it('does not send a second question while one is streaming', async () => {
    const live = liveStream();
    const { user, box, fetchMock } = await openTracing({
      [stream(conversationIds.tracing)]: live.handler,
    });
    await user.type(box, 'one{Enter}');
    await screen.findByLabelText('Streaming');
    await user.click(screen.getByRole('button', { name: 'Send' }));
    await user.keyboard('two{Enter}');
    const asks = fetchMock.mock.calls.filter(([u]) => String(u).endsWith('/ask/stream'));
    expect(asks).toHaveLength(1);
  });

  it('shows the hostile text of a question and an answer as text', async () => {
    const hostile = '<img src=x onerror=alert(1)>';
    const { user, box } = await openTracing({
      [stream(conversationIds.tracing)]: sse([
        sseFrame('sources', {
          sources: [{ title: hostile, path: 'javascript:alert(1)' }],
          count: 1,
        }),
        sseFrame('token', { text: hostile }),
        DONE([{ title: hostile, path: 'javascript:alert(1)' }]),
      ]),
    });
    await user.type(box, `${hostile}{Enter}`);
    await waitFor(() => expect(screen.getAllByText(hostile).length).toBeGreaterThanOrEqual(3));
    expect(document.querySelector('img')).toBeNull();
    expect(document.querySelector('a')).toBeNull();
  });

  it('shows no Sources block when the answer cites nothing, whatever was retrieved while it streamed', async () => {
    const { user, box } = await openTracing({
      [stream(conversationIds.tracing)]: sse([
        sseFrame('sources', { sources: SOURCES, count: 2 }),
        sseFrame('token', { text: 'I do not know.' }),
        DONE([]),
      ]),
    });
    const before = screen.getAllByRole('list', { name: 'Sources' }).length;
    await user.type(box, 'q{Enter}');
    await screen.findByText('I do not know.');
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled());
    expect(screen.getAllByRole('list', { name: 'Sources' })).toHaveLength(before);
  });

  it('does not start a second answer from Retry while one is streaming', async () => {
    const live = liveStream();
    let attempt = 0;
    const { user, box, fetchMock } = await openTracing({
      [stream(conversationIds.tracing)]: () =>
        attempt++ === 0
          ? new Response(sseStreamFromFrames(conversationAskFailureFrames), {
              status: 200,
              headers: { 'content-type': 'text/event-stream' },
            })
          : live.handler(),
    });
    await user.type(box, 'first{Enter}');
    const failedAlert = await screen.findByRole('alert');
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled());
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'second{Enter}');
    await screen.findByLabelText('Streaming');
    // The first question's Retry button is still on screen while the second answer streams.
    await user.click(within(failedAlert).getByRole('button', { name: 'Retry' }));
    const asks = fetchMock.mock.calls.filter(([u]) => String(u).endsWith('/ask/stream'));
    expect(asks).toHaveLength(2);
    // ...and the failed question is still there to retry once the answer is done.
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getAllByText('first')).toHaveLength(1);
  });

  it('ends the answer when a new chat is started', async () => {
    const live = liveStream();
    let signal: AbortSignal | undefined;
    const { user, box } = await openTracing({
      [stream(conversationIds.tracing)]: live.handler,
      [CREATE]: ok(createdConversationFixture, 201),
    });
    const baseFetch = globalThis.fetch;
    vi.stubGlobal('fetch', (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/ask/stream')) signal = init?.signal ?? undefined;
      return baseFetch(input, init);
    });
    await user.type(box, 'q{Enter}');
    await screen.findByLabelText('Streaming');
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    await waitFor(() => expect(signal?.aborted).toBe(true));
    live.push(sseFrame('token', { text: 'LATE-TOKEN' }));
    await new Promise((r) => setTimeout(r, 30));
    expect(screen.queryByText(/LATE-TOKEN/)).not.toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled();
  });

  it('does not carry a question that failed before a chat existed into the next chat', async () => {
    let creates = 0;
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [CREATE]: () =>
        creates++ === 0
          ? Response.json({ error: 'upstream_unreachable', trace_id: TRACE }, { status: 502 })
          : Response.json(createdConversationFixture, { status: 201 }),
      [stream(NEW_ID)]: sse([sseFrame('token', { text: 'ok' }), DONE([])]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'first{Enter}');
    await screen.findByRole('alert');
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'second{Enter}');
    await screen.findByText('ok');
    expect(screen.queryByText('first')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
  });

  it('names an untitled conversation after the question: at most 200 units, never half an emoji', async () => {
    stubFetch({
      [LIST]: ok({
        items: [{ id: NEW_ID, title: 'New conversation', updated_at: '2026-10-06T10:20:00.000Z' }],
        count: 1,
      }),
      [open(NEW_ID)]: ok({ ...createdConversationFixture, truncated: false, messages: [] }),
      [stream(NEW_ID)]: sse([sseFrame('token', { text: 'ok' }), DONE([])]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /New conversation/ }));
    const box = await screen.findByRole('textbox', { name: 'Message' });
    await user.click(box);
    await user.paste('a'.repeat(199) + String.fromCodePoint(0x1f600) + 'tail');
    await user.keyboard('{Enter}');
    await waitFor(() => expect(rowsOf()[0]).toHaveTextContent('a'.repeat(199)));
    const title = rowsOf()[0].querySelector('[title]')?.getAttribute('title') ?? '';
    expect(title).toBe('a'.repeat(199)); // the emoji (2 units) would be unit 200-201: left out whole
  });

  it('a late done from an answer that was left does not move or retitle a row', async () => {
    const live = liveStream();
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.errors)]: ok(conversationDetails[conversationIds.errors]),
      [open(conversationIds.onboarding)]: ok(conversationDetails[conversationIds.onboarding]),
      [stream(conversationIds.errors)]: live.handler,
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Error contract/ }));
    await screen.findByText(/Every non-2xx response body/);
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'q{Enter}');
    await screen.findByLabelText('Streaming');

    await user.click(screen.getByRole('button', { name: /Onboarding/ }));
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Message' })).toBeEnabled());
    live.push(sseFrame('token', { text: 'late' }));
    live.push(DONE([]));
    live.end();
    await new Promise((r) => setTimeout(r, 30));
    expect(rowsOf().map((r) => r.textContent ?? '')).toEqual([
      expect.stringContaining('How does trace_id'),
      expect.stringContaining('Error contract'),
      expect.stringContaining('Onboarding'),
    ]);
  });

  it('puts the conversation id into the ask URL encoded, whatever the api sent', async () => {
    const odd = 'x/../y?z=1';
    const fetchMock = stubFetch({
      [LIST]: ok({
        items: [{ id: odd, title: 'Odd', updated_at: '2026-10-06T10:20:00.000Z' }],
        count: 1,
      }),
      [open(encodeURIComponent(odd))]: ok({
        ...createdConversationFixture,
        id: odd,
        truncated: false,
        messages: [],
      }),
      [stream(encodeURIComponent(odd))]: sse([sseFrame('token', { text: 'ok' }), DONE([])]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /Odd/ }));
    await screen.findByTestId('chat-empty');
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'q{Enter}');
    await screen.findByText('ok');
    const asked = fetchMock.mock.calls
      .map(([u]) => String(u))
      .filter((u) => u.endsWith('/ask/stream'));
    expect(asked).toEqual([`/api/v1/conversations/${encodeURIComponent(odd)}/ask/stream`]);
  });

  it('keeps a chat that was created for a question in the list even if the person went elsewhere meanwhile', async () => {
    const gate = deferred();
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [CREATE]: () => gate.promise,
      [open(conversationIds.onboarding)]: ok(conversationDetails[conversationIds.onboarding]),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.type(screen.getByRole('textbox', { name: 'Message' }), 'q{Enter}');
    await user.click(screen.getByRole('button', { name: /Onboarding/ }));
    gate.resolve(Response.json(createdConversationFixture, { status: 201 }));
    await waitFor(() => expect(rowsOf()).toHaveLength(4));
    expect(rowsOf()[0]).toHaveTextContent('New conversation');
    // ...and nothing was asked in it.
    expect(screen.queryByLabelText('Streaming')).not.toBeInTheDocument();
  });
});

describe('<ChatWorkspace /> — the shared-chats notice', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  const notice = () => screen.getByTestId('shared-chats-notice');

  it('is plain text next to the input, in the same panel', async () => {
    stubFetch({ [LIST]: ok(conversationListFixture) });
    render(<ChatWorkspace />);
    expect(notice()).toHaveTextContent(NOTICE);
    expect(notice().children).toHaveLength(0);
    const panel = screen.getByRole('region', { name: 'Chat' });
    expect(panel).toContainElement(notice());
    expect(panel).toContainElement(screen.getByRole('textbox', { name: 'Message' }));
    await screen.findByRole('button', { name: /Error contract/ });
  });

  it('is there while the list loads, when it is empty and when it failed', async () => {
    const gate = deferred();
    stubFetch({ [LIST]: () => gate.promise });
    const first = render(<ChatWorkspace />);
    expect(notice()).toHaveTextContent(NOTICE);
    gate.resolve(Response.json(emptyConversationList));
    await screen.findByTestId('conversations-empty');
    expect(notice()).toHaveTextContent(NOTICE);
    first.unmount();

    stubFetch({ [LIST]: fail('upstream_error') });
    render(<ChatWorkspace />);
    await screen.findByRole('alert');
    expect(notice()).toHaveTextContent(NOTICE);
  });

  it('is there while a conversation loads, when it is ready and when it failed to load', async () => {
    const gate = deferred();
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [open(conversationIds.tracing)]: () => gate.promise,
      [open(conversationIds.errors)]: fail('upstream_error'),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await user.click(await screen.findByRole('button', { name: /How does trace_id/ }));
    expect(await screen.findByText('Loading conversation…')).toBeInTheDocument();
    expect(notice()).toHaveTextContent(NOTICE);

    gate.resolve(Response.json(conversationDetails[conversationIds.tracing]));
    await screen.findByText(/The trace id is minted/);
    expect(notice()).toHaveTextContent(NOTICE);

    await user.click(screen.getByRole('button', { name: /Error contract/ }));
    await screen.findByText(/Could not load this conversation/);
    expect(notice()).toHaveTextContent(NOTICE);
  });
});
