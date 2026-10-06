'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { ConversationListItem, ConversationMessage } from '@/lib/conversation-types';
import { createConversation, getConversation, listConversations } from '@/lib/conversations-client';
import { ChatThread } from './ChatThread';
import { ConversationList } from './ConversationList';
import { MessageInput } from './MessageInput';
import type { ChatMessage } from './types';

/** A failed BFF call, as the page shows it: the bounded code and the id to quote. */
interface Failure {
  error: string;
  traceId: string | null;
}

type ListState = { status: 'loading' } | { status: 'ready' } | ({ status: 'error' } & Failure);

type ThreadState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; messages: ChatMessage[]; truncated: boolean }
  | ({ status: 'error' } & Failure);

function ErrorLine({ what, failure }: Readonly<{ what: string; failure: Failure }>) {
  return (
    <p role="alert" className="text-sm text-red-600 dark:text-red-400">
      {what} ({failure.error}).
      {failure.traceId && (
        <>
          {' '}
          Trace ID: <code className="break-all">{failure.traceId}</code>
        </>
      )}
    </p>
  );
}

/** A stored message as the thread renders it; citations become their unique `docs/…` paths. */
function toChatMessage(m: ConversationMessage): ChatMessage {
  const sources = [...new Set((m.citations ?? []).map((c) => c.path))];
  return { id: m.id, role: m.role, text: m.content, ...(sources.length ? { sources } : {}) };
}

/**
 * The body of the `/chat` page (`docs/design/wireframes/chat.md`): the conversation list on the
 * left, the open thread on the right, the input at the bottom. It owns the state and the
 * fetches; `ConversationList`, `ChatThread` and `MessageInput` stay presentational.
 *
 * - **Client-only data.** The list is fetched after mount, never server-rendered: its times are
 *   read in the browser's time zone (see `ConversationList`).
 * - **Latest click wins.** Opening a conversation while another is still loading ignores the
 *   slower answer, so the thread never shows a conversation that is not the selected one.
 * - **Narrow screens.** Below 768 px the list is a drawer opened by the *Conversations* button.
 * - **Sending** a message is added in web 3.3; until then the input only says so.
 */
export function ChatWorkspace() {
  const [items, setItems] = useState<ConversationListItem[]>([]);
  const [list, setList] = useState<ListState>({ status: 'loading' });
  const [reloadKey, setReloadKey] = useState(0);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [thread, setThread] = useState<ThreadState>({ status: 'idle' });
  const [creating, setCreating] = useState(false);
  const [createFailure, setCreateFailure] = useState<Failure | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const inputRef = useRef<HTMLTextAreaElement>(null);
  // Bumped by every open/new-chat; a response is applied only if it still holds the latest value.
  const threadRef = useRef<HTMLElement>(null);
  const openSeq = useRef(0);
  const openAbort = useRef<AbortController | null>(null);

  // Load (or retry) the list after mount.
  useEffect(() => {
    const controller = new AbortController();
    void listConversations({ signal: controller.signal }).then((result) => {
      if (controller.signal.aborted) return;
      if (!result.ok) {
        setList({ status: 'error', error: result.error, traceId: result.traceId });
        return;
      }
      // Keep a chat created while this was loading; the server's rows win for the rest.
      setItems((current) => {
        const known = new Set(result.data.items.map((i) => i.id));
        return [...current.filter((i) => !known.has(i.id)), ...result.data.items];
      });
      setList({ status: 'ready' });
    });
    return () => controller.abort();
  }, [reloadKey]);

  // Leaving the page cancels an open in flight.
  useEffect(() => () => openAbort.current?.abort(), []);

  const retryList = useCallback(() => {
    setList({ status: 'loading' });
    setReloadKey((k) => k + 1);
  }, []);

  const openConversation = useCallback((id: string) => {
    const seq = ++openSeq.current;
    openAbort.current?.abort();
    const controller = new AbortController();
    openAbort.current = controller;

    setSelectedId(id);
    setThread({ status: 'loading' });
    setNotice(null);
    setCreateFailure(null);
    setDrawerOpen(false);
    // The row that had focus may be inside the drawer that just closed; keep the keyboard user here.
    threadRef.current?.focus();
    void getConversation(id, { signal: controller.signal }).then((result) => {
      if (seq !== openSeq.current) return;
      if (!result.ok) {
        setThread({ status: 'error', error: result.error, traceId: result.traceId });
        return;
      }
      setThread({
        status: 'ready',
        messages: result.data.messages.map(toChatMessage),
        truncated: result.data.truncated,
      });
    });
  }, []);

  const newChat = useCallback(async () => {
    if (creating) return;
    setCreating(true);
    setCreateFailure(null);
    const startedAt = openSeq.current;
    const result = await createConversation();
    setCreating(false);
    if (!result.ok) {
      setCreateFailure({ error: result.error, traceId: result.traceId });
      return;
    }
    const { id, title, updated_at } = result.data;
    setItems((current) => [{ id, title, updated_at }, ...current.filter((i) => i.id !== id)]);
    // The person opened something else while this was in flight: the later click wins. The new
    // chat is in the list, just not selected.
    if (openSeq.current !== startedAt) return;
    openSeq.current++;
    openAbort.current?.abort();
    setSelectedId(id);
    setThread({ status: 'ready', messages: [], truncated: false });
    setNotice(null);
    setDrawerOpen(false);
    // The textarea is enabled in every state, so it can take focus now.
    inputRef.current?.focus();
  }, [creating]);

  const listPanel = (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      <ConversationList
        conversations={items}
        selectedId={selectedId}
        onSelect={openConversation}
        onNewChat={() => void newChat()}
        showEmpty={list.status === 'ready'}
      />
      {list.status === 'loading' && (
        <p role="status" className="text-sm text-neutral-600 dark:text-neutral-400">
          Loading conversations…
        </p>
      )}
      {list.status === 'error' && (
        <div className="flex flex-col items-start gap-2">
          <ErrorLine what="Could not load conversations" failure={list} />
          <button
            type="button"
            onClick={retryList}
            className="rounded-md border border-neutral-300 px-3 py-1 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
          >
            Retry
          </button>
        </div>
      )}
      {createFailure && <ErrorLine what="Could not start a new chat" failure={createFailure} />}
    </div>
  );

  return (
    <div className="relative mx-auto flex h-[calc(100dvh-7rem)] min-h-[24rem] w-full max-w-6xl gap-4">
      {/* The list: a column from 768 px up, a drawer over the thread below that. */}
      <aside
        id="conversation-drawer"
        className={`${
          drawerOpen ? 'flex' : 'hidden'
        } absolute inset-y-0 left-0 z-10 w-72 max-w-[85%] flex-col rounded-xl border border-neutral-200 bg-white p-3 shadow-lg md:static md:z-auto md:flex md:w-72 md:max-w-none md:shrink-0 md:shadow-none dark:border-neutral-800 dark:bg-neutral-900`}
      >
        {listPanel}
      </aside>

      <section
        ref={threadRef}
        tabIndex={-1}
        aria-label="Chat"
        className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 rounded-xl focus:outline-none border border-neutral-200 bg-neutral-50 p-4 dark:border-neutral-800 dark:bg-neutral-950"
      >
        <div className="md:hidden">
          <button
            type="button"
            aria-expanded={drawerOpen}
            aria-controls="conversation-drawer"
            onClick={() => setDrawerOpen((open) => !open)}
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
          >
            Show conversations
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {thread.status === 'idle' && (
            <p className="text-sm text-neutral-600 dark:text-neutral-400" data-testid="thread-idle">
              Pick a conversation, or start a new chat.
            </p>
          )}
          {thread.status === 'loading' && (
            <p role="status" className="text-sm text-neutral-600 dark:text-neutral-400">
              Loading conversation…
            </p>
          )}
          {thread.status === 'error' && (
            <ErrorLine what="Could not load this conversation" failure={thread} />
          )}
          {thread.status === 'ready' && (
            <div className="flex flex-col gap-3">
              {thread.truncated && (
                <p className="text-xs text-neutral-600 dark:text-neutral-400">
                  Showing the most recent 500 messages.
                </p>
              )}
              <ChatThread messages={thread.messages} />
            </div>
          )}
        </div>

        {notice && (
          <p role="status" className="text-sm text-neutral-700 dark:text-neutral-300">
            {notice}
          </p>
        )}
        <MessageInput
          textareaRef={inputRef}
          keepText
          onSubmit={() => setNotice('Sending messages is not available yet.')}
        />
      </section>
    </div>
  );
}
