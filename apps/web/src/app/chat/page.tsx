import { ChatWorkspace } from '@/components/chat/ChatWorkspace';

/**
 * The chat page: conversation list, open thread, input (`docs/design/wireframes/chat.md`). It
 * talks only to the same-origin BFF routes under `/api/v1/conversations`; with
 * `MOCK_UPSTREAM=true` those serve the fixtures in `src/mocks/conversations.ts`. The page stays
 * thin — state and fetches live in `ChatWorkspace`.
 */
export default function ChatPage() {
  return (
    <main className="px-4 py-6">
      <h1 className="mx-auto mb-4 max-w-6xl text-xl font-semibold">Team Assistant</h1>
      <ChatWorkspace />
    </main>
  );
}
