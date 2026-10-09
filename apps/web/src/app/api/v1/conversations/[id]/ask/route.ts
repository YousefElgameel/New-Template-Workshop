import { withBff } from '@/lib/trace';
import { log } from '@/lib/logger';
import { isMockUpstream, mockJson } from '@/lib/mocks';
import { acquireAsk, askSignal, checkOrigin, readAskRequest } from '@/lib/ask-bff';
import { callApi } from '@/lib/conversations-bff';
import { conversationAskResponse, mockConversationExists } from '@/mocks/conversations';

export const dynamic = 'force-dynamic';

/** The route PATTERN — used for metrics and logs; the concrete id and the question never are. */
const ROUTE = '/api/v1/conversations/[id]/ask';

/**
 * POST /api/v1/conversations/[id]/ask  → api POST /v1/conversations/{id}/ask
 * Body `{ "question": string }`. The answer comes back whole, with its citations. The api's own
 * `404`, `413 payload_too_large`, `429 busy` and `503 ai_unavailable` pass through with their body
 * and `trace_id`. A model-backed call takes up to ~4 minutes, so the hop timeout is set explicitly
 * (`ASK_HOP_TIMEOUT_MS`) instead of `fetchUpstream`'s 10 s default.
 */
export async function POST(
  req: Request,
  ctx: { params: Promise<{ id: string }> },
): Promise<Response> {
  return withBff(
    req,
    async ({ traceId }) => {
      const foreign = checkOrigin(req, traceId, ROUTE);
      if (foreign) return foreign;
      const { id } = await ctx.params;
      const ask = await readAskRequest(req, id, traceId, ROUTE);
      if (!ask.ok) return ask.response;
      log('info', 'bff inbound', { route: ROUTE, method: 'POST' });
      const slot = acquireAsk(req, traceId, ROUTE);
      if (!slot.ok) return slot.response;

      try {
        if (isMockUpstream()) {
          return mockConversationExists(id.toLowerCase())
            ? mockJson(conversationAskResponse, traceId)
            : mockJson({ error: 'not_found', trace_id: traceId }, traceId, 404);
        }
        return await callApi(`/v1/conversations/${ask.id}/ask`, traceId, ROUTE, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ question: ask.question }),
          signal: askSignal(req),
        });
      } finally {
        slot.release();
      }
    },
    { routeClass: ROUTE },
  );
}
