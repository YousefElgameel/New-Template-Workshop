import { sseFrame } from '@/lib/sse';

/**
 * Fixtures for the example assistant surface (`/api/v1/assistant/ask[/stream]`).
 * The api ships no `/v1/assistant` route — a project adds one on `app/ai/graph.py`
 * (`feature-scaffold` target `ai`); until then these fixtures make the chat page usable.
 *
 * TODO(contract): replace these shapes with `paths['/v1/assistant/ask']` from `@/lib/api-types`
 * once the route exists and `make openapi` has been run.
 */
export interface AskResponse {
  answer: string;
  sources: string[];
}

export const askFixture: AskResponse = {
  answer:
    'This is a mocked answer from src/mocks/assistant.ts. The api listens on port 8000 [1] and the web app on port 3000 [2].',
  sources: ['docs/api.md', 'docs/web.md'],
};

const citations = askFixture.sources.map((path) => ({ title: path, path }));

/** The frames the api's conversation stream emits: sources → token* → done (api roadmap 4.3). */
export const askStreamFrames: string[] = [
  sseFrame('sources', { sources: citations, count: 2 }),
  ...askFixture.answer.split(' ').map((word) => sseFrame('token', { text: `${word} ` })),
  sseFrame('done', {
    message_id: 'mock-message',
    input_tokens: 42,
    output_tokens: 24,
    citations,
  }),
];
