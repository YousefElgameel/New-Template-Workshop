// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  conversationDetails,
  conversationIds,
  conversationListFixture,
  createdConversationFixture,
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
    expect(sources[0]).toHaveTextContent('[1] docs/architecture/observability.md');
    expect(sources[0]).toHaveTextContent('[2] docs/architecture/overview.md');
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
    await screen.findByText(evil);
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

  it('says sending is not available yet, rather than dropping the message silently', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [CREATE]: ok(createdConversationFixture, 201),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    await user.type(await screen.findByRole('textbox', { name: 'Message' }), 'hello{Enter}');
    expect(await screen.findByText('Sending messages is not available yet.')).toBeInTheDocument();
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

  it('keeps what was typed when sending is not available', async () => {
    stubFetch({
      [LIST]: ok(conversationListFixture),
      [CREATE]: ok(createdConversationFixture, 201),
    });
    const user = userEvent.setup();
    render(<ChatWorkspace />);
    await screen.findByRole('button', { name: /Onboarding/ });
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    const box = await screen.findByRole('textbox', { name: 'Message' });
    await user.type(box, 'hello{Enter}');
    expect(box).toHaveValue('hello');
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
