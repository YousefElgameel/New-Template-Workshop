'use client';

import { useId, useState, type FormEvent, type KeyboardEvent, type Ref } from 'react';

interface Props {
  onSubmit: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  /** Lets the page move focus here (e.g. after **+ New chat**). */
  textareaRef?: Ref<HTMLTextAreaElement>;
  /**
   * Show a "n / max" counter and refuse to send more than `max` characters. Characters are
   * counted as code points, like the api does (an emoji is one), on the text as it will be sent
   * (trimmed).
   */
  maxChars?: number;
}

/** Characters (code points) in `text`. */
function charCount(text: string): number {
  let count = 0;
  for (const _ of text) count += 1;
  return count;
}

/** Textarea + send button. Enter sends, Shift+Enter inserts a newline. */
export function MessageInput({
  onSubmit,
  disabled = false,
  placeholder = 'Ask a question…',
  textareaRef,
  maxChars,
}: Props) {
  const [text, setText] = useState('');
  const countId = useId();
  const trimmed = text.trim();
  const count = charCount(trimmed);
  const tooLong = maxChars !== undefined && count > maxChars;
  const canSend = !disabled && trimmed.length > 0 && !tooLong;

  function submit(e?: FormEvent) {
    e?.preventDefault();
    if (!canSend) return;
    onSubmit(trimmed);
    setText('');
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <form onSubmit={submit} className="flex flex-col gap-1" aria-label="Message form">
      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          rows={2}
          placeholder={placeholder}
          aria-label="Message"
          aria-invalid={tooLong || undefined}
          aria-describedby={maxChars === undefined ? undefined : countId}
          className="flex-1 resize-none rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm text-neutral-900 focus:border-blue-500 focus:outline-none disabled:opacity-60 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100"
        />
        <button
          type="submit"
          disabled={!canSend}
          className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Send
        </button>
      </div>
      {maxChars !== undefined && (
        <p
          id={countId}
          className={`text-xs ${
            tooLong ? 'text-red-600 dark:text-red-400' : 'text-neutral-600 dark:text-neutral-400'
          }`}
        >
          {count.toLocaleString('en-US')} / {maxChars.toLocaleString('en-US')}
          {tooLong && ' — too long to send'}
        </p>
      )}
    </form>
  );
}
