import type { Citation } from '@/lib/conversation-types';

export type ChatRole = 'user' | 'assistant';

/** Why an answer failed: a bounded kind and, when the request got far enough, its trace id. */
export interface ChatError {
  /** A short snake_case word from the stream or the client (`timeout`, `busy`, `incomplete`, …). */
  kind: string;
  /** The request's trace id, so a person can quote it; absent when there was none. */
  traceId?: string;
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  /**
   * The documents an assistant message cites or, while it streams, every document that was
   * retrieved. Untrusted text (indexed titles and paths): show it as text, never as markup or a link.
   */
  sources?: Citation[];
  /** Still streaming. */
  pending?: boolean;
  /** Set when the answer failed. */
  error?: ChatError;
}
