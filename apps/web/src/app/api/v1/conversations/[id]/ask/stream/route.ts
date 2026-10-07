import { withBff } from '@/lib/trace';
import { log } from '@/lib/logger';
import { isMockUpstream, mockJson, mockSse } from '@/lib/mocks';
import { askSignal, guardStream, readAskRequest } from '@/lib/ask-bff';
import { apiUrl, relayJson, upstreamUnreachable } from '@/lib/conversations-bff';
import { proxyStream, streamUpstream } from '@/lib/stream';
import {
  conversationAskFailureFrames,
  conversationAskStreamFrames,
  findConversationFixture,
} from '@/mocks/conversations';

export const dynamic = 'force-dynamic';

const ROUTE = '/api/v1/conversations/[id]/ask/stream';

/**
 * POST /api/v1/conversations/[id]/ask/stream  → api POST /v1/conversations/{id}/ask/stream (SSE)
 * Body `{ "question": string }`. The api's frames (`sources`, `token`, `done` / `error`) reach the
 * browser unchanged. Errors the api reports before the first byte (`404`, `413`, `429 busy`, `422`)
 * pass through as JSON with their `trace_id`; anything else that is not an event stream is a `502`.
 * The hop timeout covers the api's ~4 minute worst case. Mock mode serves the 1.1 fixtures;
 * `?mock=failure` (mock mode only) serves the stream that fails after the answer started.
 */
export async function POST(
  req: Request,
  ctx: { params: Promise<{ id: string }> },
): Promise<Response> {
  return withBff(
    req,
    async ({ traceId }) => {
      const { id } = await ctx.params;
      const ask = await readAskRequest(req, id, traceId, ROUTE);
      if (!ask.ok) return ask.response;
      log('info', 'bff inbound', { route: ROUTE, method: 'POST' });

      if (isMockUpstream()) {
        if (!findConversationFixture(id.toLowerCase())) {
          return mockJson({ error: 'not_found', trace_id: traceId }, traceId, 404);
        }
        const failing = new URL(req.url).searchParams.get('mock') === 'failure';
        return mockSse(
          failing ? conversationAskFailureFrames : conversationAskStreamFrames,
          traceId,
        );
      }

      let upstream: Response;
      try {
        upstream = await streamUpstream(apiUrl(`/v1/conversations/${ask.id}/ask/stream`), traceId, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ question: ask.question }),
          signal: askSignal(req),
          redirect: 'error',
        });
      } catch {
        return upstreamUnreachable(ROUTE, traceId);
      }

      if (!upstream.ok) return relayJson(upstream, traceId, ROUTE);
      const type = upstream.headers.get('content-type') ?? '';
      if (!upstream.body || !/^text\/event-stream\s*(;|$)/i.test(type)) {
        void upstream.body?.cancel().catch(() => undefined);
        log('error', 'bff upstream stream was not an event stream', { route: ROUTE });
        return Response.json(
          { error: 'upstream_error', target: 'api', trace_id: traceId },
          { status: 502 },
        );
      }
      log('info', 'bff upstream stream', { route: ROUTE, status: upstream.status });
      return proxyStream(
        new Response(guardStream(upstream.body, traceId, ROUTE), {
          status: upstream.status,
          headers: upstream.headers,
        }),
        traceId,
      );
    },
    { routeClass: ROUTE },
  );
}
