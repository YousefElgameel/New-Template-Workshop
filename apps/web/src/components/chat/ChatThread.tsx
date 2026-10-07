import { Sources } from './Sources';
import type { ChatMessage } from './types';

const ROLE_LABEL: Record<ChatMessage['role'], string> = { user: 'You', assistant: 'Assistant' };

/** The message list. Presentational only — no state, no fetches. */
export function ChatThread({ messages }: Readonly<{ messages: ChatMessage[] }>) {
  if (messages.length === 0) {
    return (
      <p className="text-sm text-neutral-500" data-testid="chat-empty">
        Ask a question to start.
      </p>
    );
  }
  return (
    <ol className="flex flex-col gap-3" aria-live="polite" aria-label="Conversation">
      {messages.map((m) => (
        <li
          key={m.id}
          data-role={m.role}
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
            </span>
          )}
          {m.sources && <Sources citations={m.sources} />}
        </li>
      ))}
    </ol>
  );
}
