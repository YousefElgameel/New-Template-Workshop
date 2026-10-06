// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ConversationListItem } from '@/lib/conversation-types';
import { conversationIds, conversationListFixture } from '@/mocks/conversations';
import { ConversationList } from './ConversationList';

// The fixtures' newest conversation is 2026-10-06T10:12:03.400Z; "now" is later that day, and the
// zone is pinned, so the rendered times never depend on the machine running the tests.
const NOW = new Date('2026-10-06T15:00:00.000Z');
const items = conversationListFixture.items;

function setup(overrides: Partial<React.ComponentProps<typeof ConversationList>> = {}) {
  const onSelect = vi.fn();
  const onNewChat = vi.fn();
  render(
    <ConversationList
      conversations={items}
      onSelect={onSelect}
      onNewChat={onNewChat}
      now={NOW}
      timeZone="UTC"
      {...overrides}
    />,
  );
  return { onSelect, onNewChat, user: userEvent.setup() };
}

describe('<ConversationList />', () => {
  afterEach(cleanup);

  it('renders one row per conversation, in the order given, with title and relative time', () => {
    setup();
    const rows = within(screen.getByRole('list')).getAllByRole('button');
    expect(rows).toHaveLength(3);
    expect(rows[0]).toHaveTextContent('How does trace_id propagation work?');
    expect(rows[0]).toHaveTextContent('today 10:12');
    expect(rows[1]).toHaveTextContent('Error contract');
    expect(rows[1]).toHaveTextContent('today 09:38');
    expect(rows[2]).toHaveTextContent('Onboarding');
    expect(rows[2]).toHaveTextContent('yesterday');
  });

  it('shows the + New chat button', () => {
    setup();
    expect(screen.getByRole('button', { name: '+ New chat' })).toBeInTheDocument();
  });

  it('marks the open conversation with aria-current and no other', () => {
    setup({ selectedId: conversationIds.errors });
    const current = screen
      .getAllByRole('button')
      .filter((b) => b.getAttribute('aria-current') === 'true');
    expect(current).toHaveLength(1);
    expect(current[0]).toHaveTextContent('Error contract');
    // announced as the current item
    expect(screen.getByRole('button', { name: /Error contract/, current: true })).toBe(current[0]);
  });

  it('has no selected row without a selection, or for an id that is not listed', () => {
    setup();
    cleanup();
    setup({ selectedId: '00000000-0000-4000-8000-000000000000' });
    expect(
      screen.getAllByRole('button').filter((b) => b.hasAttribute('aria-current')),
    ).toHaveLength(0);
    cleanup();
    setup({ selectedId: null });
    expect(
      screen.getAllByRole('button').filter((b) => b.hasAttribute('aria-current')),
    ).toHaveLength(0);
  });

  it('calls onSelect with the id when a row is clicked', async () => {
    const { onSelect, user } = setup();
    await user.click(screen.getByRole('button', { name: /Onboarding/ }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(conversationIds.onboarding);
  });

  it('calls onNewChat when + New chat is pressed, and nothing else', async () => {
    const { onSelect, onNewChat, user } = setup();
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    expect(onNewChat).toHaveBeenCalledTimes(1);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('is usable with the keyboard alone: Tab reaches New chat then each row; Enter and Space activate', async () => {
    const { onSelect, onNewChat, user } = setup();
    await user.tab();
    expect(screen.getByRole('button', { name: '+ New chat' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(onNewChat).toHaveBeenCalledTimes(1);

    await user.tab();
    expect(screen.getByRole('button', { name: /How does trace_id/ })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(onSelect).toHaveBeenLastCalledWith(conversationIds.tracing);

    await user.tab();
    expect(screen.getByRole('button', { name: /Error contract/ })).toHaveFocus();
    await user.keyboard(' ');
    expect(onSelect).toHaveBeenLastCalledWith(conversationIds.errors);
    expect(onSelect).toHaveBeenCalledTimes(2);
  });

  it('shows "No chats yet" and still offers + New chat when there are no conversations', async () => {
    const { onNewChat, user } = setup({ conversations: [] });
    expect(screen.getByTestId('conversations-empty')).toHaveTextContent('No chats yet');
    expect(screen.queryByRole('list')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    expect(onNewChat).toHaveBeenCalledTimes(1);
  });

  it('hides the empty line while the page is still loading, but keeps + New chat', () => {
    setup({ conversations: [], showEmpty: false });
    expect(screen.queryByTestId('conversations-empty')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '+ New chat' })).toBeInTheDocument();
  });

  it('truncates a long title without widening the column, and keeps the full text as a tooltip', () => {
    const long = 'A very long question about trace propagation across the whole stack '
      .repeat(8)
      .trim();
    const longItem: ConversationListItem = {
      id: 'ffffffff-ffff-4fff-8fff-ffffffffffff',
      title: long,
      updated_at: '2026-10-06T10:12:03.400Z',
    };
    setup({ conversations: [longItem] });
    const row = screen.getByRole('button', { name: new RegExp(long.slice(0, 20)) });
    const title = within(row).getByText(long);
    expect(title).toHaveClass('truncate', 'min-w-0');
    expect(row).toHaveClass('w-full', 'min-w-0');
    expect(row.closest('li')).toHaveClass('min-w-0');
    expect(screen.getByRole('navigation')).toHaveClass('min-w-0');
    // the tooltip is on the text, not the button: a title on the button would also be announced
    // as its description and repeat the name
    expect(title).toHaveAttribute('title', long);
    expect(row).not.toHaveAttribute('title');
    expect(row).not.toHaveAccessibleDescription();
  });

  it('shows the title alone when the timestamp cannot be read', () => {
    setup({ conversations: [{ id: 'x', title: 'Odd one', updated_at: 'not a date' }] });
    const row = screen.getByRole('button', { name: 'Odd one' });
    expect(row).toHaveTextContent(/^Odd one$/);
  });

  it('renders titles as text, never as markup', () => {
    setup({
      conversations: [
        { id: 'x', title: '<img src=x onerror=alert(1)>', updated_at: '2026-10-06T10:12:03.400Z' },
      ],
    });
    expect(screen.getByRole('button', { name: /<img src=x/ })).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
  });

  it('renders the time as a <time> element carrying the exact instant', () => {
    setup();
    const time = screen.getByText('today 10:12');
    expect(time.tagName).toBe('TIME');
    expect(time).toHaveAttribute('dateTime', '2026-10-06T10:12:03.400Z');
  });

  it('keeps the selected row tinted on hover and gives the others a neutral hover', () => {
    setup({ selectedId: conversationIds.tracing });
    const selected = screen.getByRole('button', { name: /How does trace_id/ });
    expect(selected).toHaveClass('bg-blue-50', 'hover:bg-blue-100', 'border-blue-600');
    expect(selected).not.toHaveClass('hover:bg-neutral-100');
    const other = screen.getByRole('button', { name: /Error contract/ });
    expect(other).toHaveClass('hover:bg-neutral-100', 'border-transparent');
    expect(other).not.toHaveClass('bg-blue-50');
  });

  it('leaves room around the rows so a scroll container does not clip the focus ring', () => {
    setup();
    const list = screen.getByRole('list');
    expect(list).toHaveClass('overflow-y-auto', 'p-1');
  });

  it('names the list once: a navigation landmark, not three overlapping labels', () => {
    setup();
    expect(screen.getAllByLabelText(/conversation/i, { selector: 'nav, ul' })).toHaveLength(1);
  });

  it('is a labelled navigation landmark', () => {
    setup();
    expect(screen.getByRole('navigation', { name: 'Conversations' })).toBeInTheDocument();
  });

  it('does not fetch: it only calls back', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const { user } = setup({ selectedId: conversationIds.tracing });
    await user.click(screen.getByRole('button', { name: '+ New chat' }));
    await user.click(screen.getByRole('button', { name: /Onboarding/ }));
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });

  it('updates when the list or the selection changes', () => {
    const onSelect = vi.fn();
    const { rerender } = render(
      <ConversationList
        conversations={items.slice(0, 1)}
        selectedId={conversationIds.tracing}
        onSelect={onSelect}
        onNewChat={() => undefined}
        now={NOW}
        timeZone="UTC"
      />,
    );
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    rerender(
      <ConversationList
        conversations={items}
        selectedId={conversationIds.onboarding}
        onSelect={onSelect}
        onNewChat={() => undefined}
        now={NOW}
        timeZone="UTC"
      />,
    );
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
    expect(screen.getByRole('button', { name: /Onboarding/ })).toHaveAttribute(
      'aria-current',
      'true',
    );
    expect(screen.getByRole('button', { name: /How does trace_id/ })).not.toHaveAttribute(
      'aria-current',
    );
  });
});
