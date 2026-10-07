import { Sources } from './Sources';
import type { ChatMessage } from './types';

const ROLE_LABEL: Record<ChatMessage['role'], string> = { user: 'You', assistant: 'Assistant' };

interface Props {
  messages: ChatMessage[];
  /** When given, a question that failed shows a **Retry** button that calls it with that message. */
  onRetry?: (message: ChatMessage) => void;
}

/**
 * The message list. Presentational only — no state, no fetches. Only the newest message is a live
 * region, and it is marked busy while it streams, so a screen reader hears the finished answer once
 * instead of every token.
 */
export function ChatThread({ messages, onRetry }: Readonly<Props>) {
  if (messages.length === 0) {
    return (
      <p className="text-sm text-neutral-500" data-testid="chat-empty">
        Ask a question to start.
      </p>
    );
  }
  return (
    <ol className="flex flex-col gap-3" aria-label="Conversation">
      {messages.map((m, index) => (
        <li
          key={m.id}
          data-role={m.role}
          {...(index === messages.length - 1
            ? { 'aria-live': 'polite' as const, 'aria-busy': m.pending === true }
            : {})}
          className={`max-w-[85%] rounded-xl px-4 py-3 text-sm leading-relaxed shadow-sm ${
            m.role === 'user'
              ? 'self-end bg-blue-600 text-white'
              : 'self-start border border-neutral-200 bg-white text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100'
          }`}
        >
          <span className="mb-1 block text-xs font-semibold uppercase tracking-wide opacity-70">
            {ROLE_LABEL[m.role]}
          </span>
          <span className="whitespace-pre-wrap">{m.text}</span>
          {m.pending && (
            <span className="ml-1 inline-block animate-pulse" aria-label="Streaming">
              …
            </span>
          )}
          {m.error && (
            <span role="alert" className="mt-2 block text-xs text-red-600 dark:text-red-400">
              Something went wrong ({m.error.kind}). Try again.
              {m.error.traceId && (
                <>
                  {' '}
                  Trace ID: <code className="break-all">{m.error.traceId}</code>
                </>
              )}
              {onRetry && m.role === 'user' && (
                <button
                  type="button"
                  onClick={() => onRetry(m)}
                  className="ml-2 rounded border border-current px-2 py-0.5 font-medium hover:bg-red-50 dark:hover:bg-red-950"
                >
                  Retry
                </button>
              )}
            </span>
          )}
          {m.sources && <Sources citations={m.sources} />}
        </li>
      ))}
    </ol>
  );
}
