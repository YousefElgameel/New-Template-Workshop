import type { ConversationListItem } from '@/lib/conversation-types';
import { formatRelativeTime } from '@/lib/relative-time';

interface Props {
  /** Newest first, exactly as `GET /api/v1/conversations` returns them. */
  conversations: ConversationListItem[];
  /** The open conversation, if any. An id that is not in the list selects nothing. */
  selectedId?: string | null;
  /** A row was chosen (click, Enter or Space). The component never fetches. */
  onSelect: (id: string) => void;
  /** **+ New chat** was pressed. The page creates the conversation. */
  onNewChat: () => void;
  /** The clock for "today/yesterday". Defaults to now; tests and stories pin it. */
  now?: Date;
  /** IANA zone for the times. Defaults to the browser's — see the hydration note below. */
  timeZone?: string;
}

/**
 * The conversation sidebar of the chat page (`docs/design/wireframes/chat.md`): a **+ New chat**
 * button and one row per conversation — title, and when it was last active. Presentational only:
 * no state, no fetches (the page owns both).
 *
 * Accessibility: a labelled `<nav>`; every row is a real `<button>`, so it is reachable with Tab
 * and activated with Enter or Space; the open row carries `aria-current="true"`, which screen
 * readers announce as "current". A long title is cut with an ellipsis (the full text is a tooltip
 * on the title) and never widens the column.
 *
 * For the page that uses it:
 * - **Layout:** the column's width and height come from the parent. `truncate` needs the parent to
 *   bound the width, and the list only scrolls (`overflow-y-auto`) inside a parent with a bounded
 *   height.
 * - **Hydration:** the times are read in the browser's time zone, which the server does not know,
 *   so rows must not be in server-rendered markup. Fetch the list after mount and the first render
 *   has none.
 */
export function ConversationList({
  conversations,
  selectedId = null,
  onSelect,
  onNewChat,
  now,
  timeZone,
}: Readonly<Props>) {
  const clock = now ?? new Date();
  return (
    <nav aria-label="Conversations" className="flex min-h-0 w-full min-w-0 flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-neutral-700 dark:text-neutral-200">
          Conversations
        </h2>
        <button
          type="button"
          onClick={onNewChat}
          className="shrink-0 rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600"
        >
          + New chat
        </button>
      </div>

      {conversations.length === 0 ? (
        <p
          className="text-sm text-neutral-600 dark:text-neutral-400"
          data-testid="conversations-empty"
        >
          No chats yet
        </p>
      ) : (
        // `p-1`: a scroll container clips on both axes, so without room around the rows the 2px
        // focus ring (plus its 2px offset) would be cut off at the edges.
        <ul className="flex min-h-0 flex-col gap-1 overflow-y-auto p-1">
          {conversations.map((conversation) => {
            const selected = conversation.id === selectedId;
            const when = formatRelativeTime(conversation.updated_at, clock, timeZone);
            return (
              <li key={conversation.id} className="min-w-0">
                <button
                  type="button"
                  onClick={() => onSelect(conversation.id)}
                  aria-current={selected ? 'true' : undefined}
                  className={`flex w-full min-w-0 flex-col rounded-md border-l-4 px-3 py-2 text-left text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-600 ${
                    selected
                      ? 'border-blue-600 bg-blue-50 font-medium hover:bg-blue-100 dark:bg-blue-950 dark:hover:bg-blue-900'
                      : 'border-transparent hover:bg-neutral-100 dark:hover:bg-neutral-800'
                  }`}
                >
                  {/* The tooltip sits on the text, not the button: a title on the button would
                      also be announced as its description, repeating the name. */}
                  <span
                    title={conversation.title}
                    className="min-w-0 truncate text-neutral-900 dark:text-neutral-100"
                  >
                    {conversation.title}
                  </span>
                  {when && (
                    <time
                      dateTime={conversation.updated_at}
                      className="text-xs text-neutral-600 dark:text-neutral-400"
                    >
                      {when}
                    </time>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </nav>
  );
}
