// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChatThread } from './ChatThread';
import type { ChatMessage } from './types';

const TRACE = '0c700' + 'd'.repeat(27);

describe('<ChatThread />', () => {
  afterEach(cleanup);

  it('shows the empty state', () => {
    render(<ChatThread messages={[]} />);
    expect(screen.getByTestId('chat-empty')).toHaveTextContent('Ask a question');
  });

  it('renders user and assistant turns with sources, pending and error states', () => {
    render(
      <ChatThread
        messages={[
          { id: '1', role: 'user', text: 'Which port?' },
          {
            id: '2',
            role: 'assistant',
            text: 'Port 8000 [1].',
            sources: [{ title: 'The api', path: 'docs/api.md' }],
            pending: true,
          },
          { id: '3', role: 'assistant', text: '', error: { kind: 'timeout' } },
        ]}
      />,
    );
    const items = screen.getAllByRole('listitem').filter((li) => li.hasAttribute('data-role'));
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveAttribute('data-role', 'user');
    expect(screen.getByText('Port 8000 [1].')).toBeInTheDocument();
    expect(screen.getByLabelText('Sources')).toHaveTextContent('The api');
    expect(screen.getByLabelText('Sources')).toHaveTextContent('docs/api.md');
    expect(screen.getByLabelText('Streaming')).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('timeout');
  });

  it('shows the trace id with an error when there is one, and not otherwise', () => {
    render(
      <ChatThread
        messages={[
          { id: '1', role: 'assistant', text: '', error: { kind: 'busy', traceId: TRACE } },
          { id: '2', role: 'assistant', text: '', error: { kind: 'incomplete' } },
        ]}
      />,
    );
    const [withTrace, without] = screen.getAllByRole('alert');
    expect(withTrace).toHaveTextContent('Something went wrong (busy)');
    expect(withTrace).toHaveTextContent(`Trace ID: ${TRACE}`);
    expect(within(withTrace).getByText(TRACE).tagName).toBe('CODE');
    expect(without).toHaveTextContent('Something went wrong (incomplete)');
    expect(without).not.toHaveTextContent('Trace ID');
  });

  it('draws no Sources block for a message without citations', () => {
    render(
      <ChatThread
        messages={[
          { id: '1', role: 'assistant', text: 'I do not know.', sources: [] },
          { id: '2', role: 'assistant', text: 'No list at all.' },
        ]}
      />,
    );
    expect(screen.queryByLabelText('Sources')).not.toBeInTheDocument();
  });

  it('keeps each message its own sources', () => {
    render(
      <ChatThread
        messages={[
          { id: '1', role: 'assistant', text: 'a', sources: [{ title: 'One', path: 'docs/1.md' }] },
          { id: '2', role: 'assistant', text: 'b', sources: [{ title: 'Two', path: 'docs/2.md' }] },
        ]}
      />,
    );
    const lists = screen.getAllByRole('list', { name: 'Sources' });
    expect(lists).toHaveLength(2);
    expect(lists[0]).toHaveTextContent('One');
    expect(lists[0]).not.toHaveTextContent('Two');
    expect(lists[1]).toHaveTextContent('Two');
  });

  it('renders message text and an error kind as text, never as markup', () => {
    const { container } = render(
      <ChatThread
        messages={[
          {
            id: '1',
            role: 'assistant',
            text: '<img src=x onerror=alert(1)>',
            error: { kind: '<b>x</b>' },
          },
        ]}
      />,
    );
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('b')).toBeNull();
  });

  describe('retry and the live region', () => {
    const failed: ChatMessage = {
      id: 'u1',
      role: 'user',
      text: 'Which port?',
      error: { kind: 'timeout', traceId: '0c700' + 'd'.repeat(27) },
    };

    it('offers Retry under a failed question when a handler is given, and calls it with that message', async () => {
      const onRetry = vi.fn();
      render(<ChatThread messages={[failed]} onRetry={onRetry} />);
      await userEvent.setup().click(screen.getByRole('button', { name: 'Retry' }));
      expect(onRetry).toHaveBeenCalledWith(failed);
    });

    it('shows no Retry without a handler, nor under an assistant message', () => {
      const { rerender } = render(<ChatThread messages={[failed]} />);
      expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
      rerender(
        <ChatThread
          messages={[{ id: 'a1', role: 'assistant', text: '', error: { kind: 'timeout' } }]}
          onRetry={vi.fn()}
        />,
      );
      expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    });

    it('makes only the newest message a live region, busy while it streams', () => {
      const messages: ChatMessage[] = [
        { id: 'u1', role: 'user', text: 'q' },
        { id: 'a1', role: 'assistant', text: 'partial', pending: true },
      ];
      const { rerender } = render(<ChatThread messages={messages} />);
      const items = screen.getAllByRole('listitem');
      expect(screen.getByRole('list', { name: 'Conversation' })).not.toHaveAttribute('aria-live');
      expect(items[0]).not.toHaveAttribute('aria-live');
      expect(items[1]).toHaveAttribute('aria-live', 'polite');
      expect(items[1]).toHaveAttribute('aria-busy', 'true');
      rerender(<ChatThread messages={[messages[0], { ...messages[1], pending: false }]} />);
      expect(screen.getAllByRole('listitem')[1]).toHaveAttribute('aria-busy', 'false');
    });
  });
});
