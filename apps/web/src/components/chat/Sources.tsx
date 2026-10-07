import type { Citation } from '@/lib/conversation-types';

interface Props {
  /** The documents behind an answer. Nothing is rendered when there are none. */
  citations: readonly Citation[];
}

/**
 * The "Sources" block under an assistant message (`docs/design/wireframes/chat.md`): one row per
 * document, its title and, beside it, its path.
 *
 * Titles and paths come from indexed documents, so they are **untrusted text**. They are rendered
 * as plain text nodes — never as markup and never as a link target (there is no `<a>` here, so a
 * path like `javascript:alert(1)` is only characters). A long title wraps; a long path breaks
 * anywhere so it cannot widen the message.
 *
 * The rows are not numbered. The `[n]` markers in an answer are the numbers of the passages the
 * model was shown, and the list holds documents in citation order, so a "1." here would not mean the
 * `[1]` in the text.
 */
export function Sources({ citations }: Readonly<Props>) {
  if (citations.length === 0) return null;
  return (
    <ul aria-label="Sources" className="mt-2 flex min-w-0 flex-col gap-1">
      {citations.map((citation, index) => (
        <li
          key={`${index}:${citation.path}`}
          className="min-w-0 rounded-md bg-neutral-100 px-2 py-1 text-xs text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300"
        >
          <span className="font-medium break-words text-neutral-900 dark:text-neutral-100">
            {citation.title}
          </span>{' '}
          <span aria-hidden="true">·</span>{' '}
          <code className="break-all text-neutral-600 dark:text-neutral-400">{citation.path}</code>
        </li>
      ))}
    </ul>
  );
}
