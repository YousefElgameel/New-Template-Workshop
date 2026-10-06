import { withBff } from '@/lib/trace';
import { log } from '@/lib/logger';
import { isMockUpstream, mockJson } from '@/lib/mocks';
import { badRequest, callApi, isUuid } from '@/lib/conversations-bff';
import { findConversationFixture } from '@/mocks/conversations';

export const dynamic = 'force-dynamic';

/** The route PATTERN — used for metrics and logs; the concrete id never is. */
const ROUTE = '/api/v1/conversations/[id]';

/**
 * GET /api/v1/conversations/[id]  → api GET /v1/conversations/{id}
 * The conversation with its messages (oldest first, at most 500, `truncated` when cut). `[id]`
 * is placed into the upstream URL, so it must be a UUID — anything else is a 400 from the BFF
 * and the api is not called. In Next 16 `params` is a promise.
 */
export async function GET(
  req: Request,
  ctx: { params: Promise<{ id: string }> },
): Promise<Response> {
  return withBff(
    req,
    async ({ traceId }) => {
      const { id } = await ctx.params;
      if (!isUuid(id)) return badRequest(ROUTE, traceId);
      log('info', 'bff inbound', { route: ROUTE, method: 'GET' });

      if (isMockUpstream()) {
        const fixture = findConversationFixture(id.toLowerCase());
        return fixture
          ? mockJson(fixture, traceId)
          : mockJson({ error: 'not_found', trace_id: traceId }, traceId, 404);
      }
      return callApi(`/v1/conversations/${id}`, traceId, ROUTE);
    },
    { routeClass: ROUTE },
  );
}
