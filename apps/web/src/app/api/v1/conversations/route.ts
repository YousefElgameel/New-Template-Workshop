import { withBff } from '@/lib/trace';
import { log } from '@/lib/logger';
import { isMockUpstream, mockJson } from '@/lib/mocks';
import { checkOrigin } from '@/lib/ask-bff';
import {
  MAX_CREATE_BODY_BYTES,
  badRequest,
  callApi,
  isJsonContentType,
  parseCreateBody,
  parseLimit,
  payloadTooLarge,
  readBodyCapped,
  unsupportedMediaType,
} from '@/lib/conversations-bff';
import { conversationListFixture, createdConversationFixture } from '@/mocks/conversations';

export const dynamic = 'force-dynamic';

const ROUTE = '/api/v1/conversations';

/**
 * GET /api/v1/conversations?limit=  → api GET /v1/conversations  (newest first, limit 1–100)
 * An invalid `limit` is a 400 from the BFF; the api is not called.
 */
export async function GET(req: Request): Promise<Response> {
  return withBff(
    req,
    async ({ traceId }) => {
      const limit = parseLimit(new URL(req.url).searchParams.get('limit'));
      if (limit === null) return badRequest(ROUTE, traceId);
      log('info', 'bff inbound', { route: ROUTE, method: 'GET' });

      if (isMockUpstream()) {
        // Like the api: at most `limit` items, and `count` is the number returned.
        const items = conversationListFixture.items.slice(0, limit);
        return mockJson({ items, count: items.length }, traceId);
      }
      return callApi(
        limit === undefined ? '/v1/conversations' : `/v1/conversations?limit=${limit}`,
        traceId,
        ROUTE,
      );
    },
    { routeClass: ROUTE },
  );
}

/**
 * POST /api/v1/conversations  → api POST /v1/conversations
 * Body is optional: `{ "title"?: string }` (≤ 200 characters), at most 4 KB, read within 5 s, and
 * declared as `application/json` when present. Only `title` is forwarded. The api reads a whole
 * body before validating it, so the size cap is enforced here, while streaming.
 */
export async function POST(req: Request): Promise<Response> {
  return withBff(
    req,
    async ({ traceId }) => {
      const foreign = checkOrigin(req, traceId, ROUTE);
      if (foreign) return foreign;
      const read = await readBodyCapped(req, MAX_CREATE_BODY_BYTES);
      if (read.kind === 'too_large') return payloadTooLarge(ROUTE, traceId);
      if (read.kind === 'unreadable') return badRequest(ROUTE, traceId);
      // A body that is not declared JSON could come from a cross-site "simple" request (no preflight).
      if (read.text.trim() !== '' && !isJsonContentType(req)) {
        return unsupportedMediaType(ROUTE, traceId);
      }
      const body = parseCreateBody(read.text);
      if (!body.ok) return badRequest(ROUTE, traceId);
      log('info', 'bff inbound', { route: ROUTE, method: 'POST' });

      if (isMockUpstream()) {
        // Like the api: surrounding whitespace is trimmed and inner runs collapsed.
        const title = body.title?.replace(/\s+/g, ' ').trim();
        return mockJson(
          { ...createdConversationFixture, ...(title ? { title } : {}) },
          traceId,
          201,
        );
      }
      return callApi('/v1/conversations', traceId, ROUTE, {
        method: 'POST',
        ...(body.title === undefined || body.title === null
          ? {}
          : {
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ title: body.title }),
            }),
      });
    },
    { routeClass: ROUTE },
  );
}
