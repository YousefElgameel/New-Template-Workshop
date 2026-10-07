// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { sseFrame, sseStreamFromFrames } from '@/lib/sse';
import { ChatPanel } from './ChatPanel';

const TRACE = '0c700' + 'd'.repeat(27);
const API = { title: 'The api', path: 'docs/api.md' };
const WEB = { title: 'The web app', path: 'docs/web.md' };
const OTHER = { title: 'Something else', path: 'docs/other.md' };

const sseHeaders = { 'content-type': 'text/event-stream' };

describe('<ChatPanel />', () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('streams an answer into the thread and shows the documents it cites', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        sseStreamFromFrames([
          sseFrame('sources', { sources: [API, OTHER], count: 2 }),
          sseFrame('token', { text: 'Port ' }),
          sseFrame('token', { text: '8000' }),
          sseFrame('done', {
            message_id: 'm-1',
            citations: [API],
            input_tokens: 5,
            output_tokens: 3,
          }),
        ]),
        { status: 200, headers: sseHeaders },
      ),
    );
    const user = userEvent.setup();
    render(<ChatPanel endpoint="/api/v1/conversations/x/ask/stream" />);

    await user.type(screen.getByLabelText('Message'), 'which port?{Enter}');

    await waitFor(() => expect(screen.getByText('Port 8000')).toBeInTheDocument());
    expect(screen.getByText('which port?')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByLabelText('Streaming')).not.toBeInTheDocument());
    const sources = screen.getByLabelText('Sources');
    expect(sources).toHaveTextContent('The api');
    expect(sources).toHaveTextContent('docs/api.md');
    expect(sources).not.toHaveTextContent('Something else'); // retrieved, not cited: gone at `done`
    const [url] = (globalThis.fetch as unknown as { mock: { calls: [string][] } }).mock.calls[0];
    expect(url).toBe('/api/v1/conversations/x/ask/stream'); // same-origin only
  });

  it('shows every retrieved document while streaming, then replaces them with the cited ones', async () => {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        controller = c;
      },
    });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(body, { status: 200, headers: sseHeaders }),
    );
    const user = userEvent.setup();
    render(<ChatPanel endpoint="/api/v1/conversations/x/ask/stream" />);
    await user.type(screen.getByLabelText('Message'), 'q{Enter}');

    controller.enqueue(
      encoder.encode(sseFrame('sources', { sources: [API, WEB, OTHER], count: 3 })),
    );
    controller.enqueue(encoder.encode(sseFrame('token', { text: 'so far' })));
    await waitFor(() => expect(screen.getByText('so far')).toBeInTheDocument());
    const early = screen.getByLabelText('Sources');
    expect(within(early).getAllByRole('listitem')).toHaveLength(3); // before the answer is done
    expect(screen.getByLabelText('Streaming')).toBeInTheDocument();
    expect(screen.getByLabelText('Message')).toBeDisabled();

    controller.enqueue(encoder.encode(sseFrame('done', { message_id: 'm', citations: [WEB] })));
    controller.close();
    await waitFor(() => expect(screen.queryByLabelText('Streaming')).not.toBeInTheDocument());
    const final = screen.getByLabelText('Sources');
    expect(within(final).getAllByRole('listitem')).toHaveLength(1);
    expect(final).toHaveTextContent('The web app');
    expect(screen.getByLabelText('Message')).toBeEnabled();
  });

  it('shows no sources when the answer cites none', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        sseStreamFromFrames([
          sseFrame('sources', { sources: [API], count: 1 }),
          sseFrame('token', { text: 'I do not know.' }),
          sseFrame('done', { message_id: 'm', citations: [] }),
        ]),
        { status: 200, headers: sseHeaders },
      ),
    );
    const user = userEvent.setup();
    render(<ChatPanel endpoint="/e" />);
    await user.type(screen.getByLabelText('Message'), 'q{Enter}');
    await waitFor(() => expect(screen.getByText('I do not know.')).toBeInTheDocument());
    await waitFor(() => expect(screen.queryByLabelText('Streaming')).not.toBeInTheDocument());
    expect(screen.queryByLabelText('Sources')).not.toBeInTheDocument();
  });

  it('surfaces the error kind and trace id of an error frame, keeping what was streamed', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        sseStreamFromFrames([
          sseFrame('sources', { sources: [API], count: 1 }),
          sseFrame('token', { text: 'partial ' }),
          sseFrame('error', { error_kind: 'timeout', error: 'ai_unavailable', trace_id: TRACE }),
        ]),
        { status: 200, headers: sseHeaders },
      ),
    );
    const user = userEvent.setup();
    render(<ChatPanel endpoint="/e" />);
    await user.type(screen.getByLabelText('Message'), 'q{Enter}');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('timeout');
    expect(alert).toHaveTextContent(`Trace ID: ${TRACE}`);
    expect(screen.getByText('partial')).toBeInTheDocument();
    expect(screen.queryByLabelText('Streaming')).not.toBeInTheDocument();
  });

  it('shows what a refused request says: the api code and the trace id', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      Response.json({ error: 'busy', trace_id: TRACE }, { status: 429 }),
    );
    const user = userEvent.setup();
    render(<ChatPanel endpoint="/e" />);
    await user.type(screen.getByLabelText('Message'), 'q{Enter}');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('busy');
    expect(alert).toHaveTextContent(TRACE);
    expect(screen.getByLabelText('Message')).toBeEnabled(); // the person can try again
  });

  it('surfaces a bounded error when the upstream fails without a body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('x', { status: 502 }));
    const user = userEvent.setup();
    render(<ChatPanel endpoint="/e" />);
    await user.type(screen.getByLabelText('Message'), 'q{Enter}');
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('upstream_error'));
  });

  it('treats a stream that just stops as a failure, not a finished answer', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        sseStreamFromFrames([
          sseFrame('sources', { sources: [API] }),
          sseFrame('token', { text: 'half' }),
        ]),
        { status: 200, headers: sseHeaders },
      ),
    );
    const user = userEvent.setup();
    render(<ChatPanel endpoint="/e" />);
    await user.type(screen.getByLabelText('Message'), 'q{Enter}');
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('incomplete'));
    expect(screen.getByText('half')).toBeInTheDocument();
  });

  it('aborts the stream when the panel is unmounted', async () => {
    let signal: AbortSignal | undefined;
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (_url, init) => {
      signal = init?.signal ?? undefined;
      return new Response(new ReadableStream<Uint8Array>({ start() {} }), {
        status: 200,
        headers: sseHeaders,
      });
    });
    const user = userEvent.setup();
    const { unmount } = render(<ChatPanel endpoint="/e" />);
    await user.type(screen.getByLabelText('Message'), 'q{Enter}');
    await waitFor(() => expect(signal).toBeDefined());
    expect(signal?.aborted).toBe(false);
    unmount();
    expect(signal?.aborted).toBe(true);
  });
});
