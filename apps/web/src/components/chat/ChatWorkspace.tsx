'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { askStream } from '@/lib/chat-client';
import type { Citation, ConversationListItem, ConversationMessage } from '@/lib/conversation-types';
import { createConversation, getConversation, listConversations } from '@/lib/conversations-client';
import { ChatThread } from './ChatThread';
import { ConversationList } from './ConversationList';
import { MessageInput } from './MessageInput';
import type { ChatError, ChatMessage } from './types';

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

/** The api's limit on a question, in characters (`docs/reference/http-api.md`). */
const QUESTION_MAX_CHARS = 4000;
/** What the api calls a conversation until its first question names it. */
const UNTITLED = 'New conversation';
/** The longest conversation title, in UTF-16 units (the api's column). */
const TITLE_MAX_UNITS = 200;

let localCounter = 0;
const localId = () => `local-${++localCounter}`;

/** A citation list with one entry per path, in order. */
function uniqueByPath(citations: readonly Citation[]): Citation[] {
  const byPath = new Map<string, Citation>();
  for (const c of citations) if (!byPath.has(c.path)) byPath.set(c.path, c);
  return [...byPath.values()];
}

/** The title the api gives an untitled conversation: its first question, flattened, at most 200 units. */
function titleFrom(question: string): string {
  let title = '';
  for (const ch of question.replace(/\s+/g, ' ').trim()) {
    if (title.length + ch.length > TITLE_MAX_UNITS) break;
    title += ch;
  }
  return title;
}

/** A stored message as the thread renders it; its citations keep their titles, one per path. */
function toChatMessage(m: ConversationMessage): ChatMessage {
  const sources = uniqueByPath(m.citations ?? []);
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
 * - **Sending.** The question joins the thread at once and streams its answer in
 *   (`/api/v1/conversations/{id}/ask/stream`); with no conversation open, one is created first.
 *   While it streams the input is disabled. On `done` the conversation moves to the top of the list
 *   (an untitled one takes the question as its title). On a failure the answer placeholder goes
 *   and the question shows the error code and trace id with a **Retry** button (the api keeps the
 *   question it was asked, so a retry shows it twice when the conversation is reopened). Opening
 *   another conversation, or starting a new chat, ends the stream — nothing it sends is applied.
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
  const [streaming, setStreaming] = useState(false);

  const inputRef = useRef<HTMLTextAreaElement>(null);
  // Bumped by every open/new-chat; a response is applied only if it still holds the latest value.
  const threadRef = useRef<HTMLElement>(null);
  const openSeq = useRef(0);
  const openAbort = useRef<AbortController | null>(null);
  // Same idea for an answer in flight: bumped to ignore it, aborted to stop it.
  const sendSeq = useRef(0);
  const streamAbort = useRef<AbortController | null>(null);
  // Set when an answer ends, so the input takes focus once it is enabled again (not before).
  const refocusInput = useRef(false);

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

  // Leaving the page cancels an open and an answer in flight.
  useEffect(
    () => () => {
      openAbort.current?.abort();
      streamAbort.current?.abort();
    },
    [],
  );

  useEffect(() => {
    if (!streaming && refocusInput.current) {
      refocusInput.current = false;
      inputRef.current?.focus();
    }
  }, [streaming]);

  /** End the answer in flight, if any: its late frames are ignored and the api call is cancelled. */
  const stopStream = useCallback(() => {
    sendSeq.current++;
    streamAbort.current?.abort();
    streamAbort.current = null;
    setStreaming(false);
  }, []);

  const retryList = useCallback(() => {
    setList({ status: 'loading' });
    setReloadKey((k) => k + 1);
  }, []);

  const openConversation = useCallback(
    (id: string) => {
      const seq = ++openSeq.current;
      stopStream();
      openAbort.current?.abort();
      const controller = new AbortController();
      openAbort.current = controller;

      setSelectedId(id);
      setThread({ status: 'loading' });
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
    },
    [stopStream],
  );

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
    stopStream();
    openAbort.current?.abort();
    setSelectedId(id);
    setThread({ status: 'ready', messages: [], truncated: false });
    setDrawerOpen(false);
    // The textarea is enabled in every state, so it can take focus now.
    inputRef.current?.focus();
  }, [creating, stopStream]);

  const send = useCallback(
    async (question: string) => {
      // The ref, not the state: two submits in one tick must not both get through.
      if (streamAbort.current) return;
      const seq = ++sendSeq.current;
      const controller = new AbortController();
      streamAbort.current = controller;
      setStreaming(true);

      const userMessage: ChatMessage = { id: localId(), role: 'user', text: question };
      const assistantId = localId();
      const current = () => sendSeq.current === seq;
      const patch = (change: (messages: ChatMessage[]) => ChatMessage[]) => {
        if (!current()) return;
        setThread((prev) =>
          prev.status === 'ready' ? { ...prev, messages: change(prev.messages) } : prev,
        );
      };
      const patchAssistant = (change: (m: ChatMessage) => ChatMessage) =>
        patch((messages) => messages.map((m) => (m.id === assistantId ? change(m) : m)));
      /** The answer failed: drop its placeholder and put the error under the question. */
      const fail = (error: ChatError) =>
        patch((messages) =>
          messages
            .filter((m) => m.id !== assistantId)
            .map((m) => (m.id === userMessage.id ? { ...m, error } : m)),
        );
      const finish = () => {
        if (!current()) return;
        streamAbort.current = null;
        setStreaming(false);
        refocusInput.current = true;
      };

      // The question shows at once, with an answer on its way.
      const pending: ChatMessage = { id: assistantId, role: 'assistant', text: '', pending: true };
      // With no conversation open the thread belongs to none: what it shows (a question that failed
      // before a chat could be created) is not carried into the new one.
      const keep = (prev: ThreadState): prev is Extract<ThreadState, { status: 'ready' }> =>
        prev.status === 'ready' && selectedId !== null;
      setThread((prev) => ({
        status: 'ready',
        messages: [...(keep(prev) ? prev.messages : []), userMessage, pending],
        truncated: keep(prev) ? prev.truncated : false,
      }));

      let id = selectedId;
      if (id === null) {
        const created = await createConversation(undefined, { signal: controller.signal });
        // A chat that was created is kept in the list even if the person went elsewhere meanwhile.
        if (created.ok) {
          const chat = created.data;
          setItems((cur) => [
            { id: chat.id, title: chat.title, updated_at: chat.updated_at },
            ...cur.filter((i) => i.id !== chat.id),
          ]);
        }
        if (!current()) return;
        if (!created.ok) {
          fail({ kind: created.error, ...(created.traceId ? { traceId: created.traceId } : {}) });
          finish();
          return;
        }
        const chat = created.data;
        id = chat.id;
        openSeq.current++; // an open still in flight must not replace this thread
        openAbort.current?.abort();
        setSelectedId(chat.id);
      }

      const conversationId = id;
      await askStream(
        `/api/v1/conversations/${encodeURIComponent(conversationId)}/ask/stream`,
        question,
        {
          onSources: (sources) => patchAssistant((m) => ({ ...m, sources })),
          onToken: (text) => patchAssistant((m) => ({ ...m, text: m.text + text })),
          // The early list is every retrieved document; `done` says which ones the answer cites.
          onDone: ({ citations }) => {
            const cited = uniqueByPath(citations);
            patchAssistant(({ sources: _early, ...rest }) => ({
              ...rest,
              pending: false,
              ...(cited.length ? { sources: cited } : {}),
            }));
            if (!current()) return;
            const updatedAt = new Date().toISOString();
            setItems((cur) => {
              const known = cur.find((i) => i.id === conversationId);
              const title = known && known.title !== UNTITLED ? known.title : titleFrom(question);
              const moved = { id: conversationId, title, updated_at: updatedAt };
              return [moved, ...cur.filter((i) => i.id !== conversationId)];
            });
          },
          onError: ({ kind, traceId }) => fail({ kind, ...(traceId ? { traceId } : {}) }),
        },
        { signal: controller.signal },
      );
      finish();
    },
    [selectedId],
  );

  const retry = useCallback(
    (message: ChatMessage) => {
      if (streamAbort.current) return;
      setThread((prev) =>
        prev.status === 'ready'
          ? { ...prev, messages: prev.messages.filter((m) => m.id !== message.id) }
          : prev,
      );
      void send(message.text);
    },
    [send],
  );

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
              <ChatThread messages={thread.messages} onRetry={retry} />
            </div>
          )}
        </div>

        <MessageInput
          textareaRef={inputRef}
          maxChars={QUESTION_MAX_CHARS}
          // Not while an answer streams, nor while the thread it would join is loading or failed.
          disabled={streaming || thread.status === 'loading' || thread.status === 'error'}
          onSubmit={(question) => void send(question)}
        />
      </section>
    </div>
  );
}
