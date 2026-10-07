import type { Attributes, Context, Span } from '@opentelemetry/api';

/**
 * Keep resource ids off the web's spans (web roadmap 1.3; the api does the same with
 * `scrub_ids_server_hook`).
 *
 * The instrumentations record the CONCRETE request target: the inbound request span of
 * `GET /api/v1/conversations/<uuid>` and the outbound span to the api both carry the conversation id
 * in `http.target` / `http.url` (and `url.path` / `url.full` under the stable conventions), which
 * would put it into Application Insights (`AppRequests.Url`, `AppDependencies.Data`) even though the
 * route class, the logs and every metric use the pattern. Conversation ids identify shared,
 * personal-data-capable content (ADR-0014), so every UUID in those attributes becomes `{id}`.
 *
 * Everything here is fail-safe: telemetry must never break a request, so any error is swallowed.
 * Pure functions + a span processor, no framework imports, so it is unit-tested in the node
 * environment.
 */

/**
 * The span attributes that hold a request target or URL. `next.span_name` is Next's own copy of the name of
 * its fetch span (`fetch GET http://api:8000/v1/conversations/<uuid>`), which the Azure exporter sends on as a
 * custom dimension.
 */
export const SPAN_URL_ATTRIBUTES = [
  'http.target',
  'http.url',
  'url.path',
  'url.full',
  'next.span_name',
] as const;

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;

/** `value` with every UUID replaced by `{id}`. */
export function scrubIds(value: string): string {
  return value.replace(UUID_RE, '{id}');
}

/**
 * The scrubbed URL attributes of an outbound request, for the undici instrumentation's
 * `startSpanHook`: they replace the concrete ones before the span is created, so the id is never on
 * the span at all.
 */
export function scrubbedRequestAttributes(request: { origin: string; path: string }): Attributes {
  try {
    const url = new URL(request.path, request.origin);
    return { 'url.full': scrubIds(url.toString()), 'url.path': scrubIds(url.pathname) };
  } catch {
    return {};
  }
}

/** The minimal shape of a span this module touches (a `Span`, or a finished `ReadableSpan`). */
interface SpanLike {
  name?: string;
  attributes?: Attributes;
  setAttribute?: (key: string, value: string) => unknown;
  updateName?: (name: string) => unknown;
  isRecording?: () => boolean;
}

/** Rewrite every URL attribute of `span` that holds an id. Never throws. */
export function scrubSpanAttributes(span: SpanLike): void {
  try {
    if (span.isRecording && !span.isRecording()) return; // a span that is not recorded exports nothing
    const attributes = span.attributes;
    if (!attributes) return;
    for (const key of SPAN_URL_ATTRIBUTES) {
      const value = attributes[key];
      if (typeof value !== 'string') continue;
      const scrubbed = scrubIds(value);
      if (scrubbed !== value) span.setAttribute?.(key, scrubbed);
    }
    // Next names its fetch span after the URL it fetches.
    if (typeof span.name === 'string') {
      const name = scrubIds(span.name);
      if (name !== span.name) span.updateName?.(name);
    }
  } catch {
    // telemetry must never fail a request
  }
}

/**
 * The same, for a span that has already ended: `setAttribute` is a no-op by then, but the attribute
 * map is still the one the exporter reads later (the batch processor only queues the span).
 */
function scrubEndedSpan(span: SpanLike): void {
  try {
    const attributes = span.attributes as Record<string, unknown> | undefined;
    if (!attributes) return;
    for (const key of SPAN_URL_ATTRIBUTES) {
      try {
        const value = attributes[key];
        if (typeof value !== 'string') continue;
        const scrubbed = scrubIds(value);
        if (scrubbed !== value) attributes[key] = scrubbed;
      } catch {
        // one attribute that cannot be written must not leave the others unscrubbed
      }
    }
    try {
      if (typeof span.name === 'string') {
        const name = scrubIds(span.name);
        if (name !== span.name) (span as { name: string }).name = name;
      }
    } catch {
      // see above
    }
  } catch {
    // telemetry must never fail a request
  }
}

/**
 * A span processor that scrubs ids from the URL attributes when a span starts (the instrumentations
 * set them at creation) and again when it ends (anything that wrote them in between). Registered
 * through the Azure distro's `spanProcessors`, ahead of its exporter, so it covers the inbound request
 * span and every outbound span alike. Structurally a `SpanProcessor` of `@opentelemetry/sdk-trace-base`.
 */
export class IdScrubSpanProcessor {
  onStart(span: Span, _parentContext: Context): void {
    scrubSpanAttributes(span as unknown as SpanLike);
  }

  /** Called by the SDK just before the span ends, while `setAttribute` still works. */
  onEnding(span: unknown): void {
    scrubSpanAttributes(span as SpanLike);
  }

  /** The last pass: the span has ended, so its maps are written directly (the exporter reads them later). */
  onEnd(span: unknown): void {
    scrubEndedSpan(span as SpanLike);
  }

  forceFlush(): Promise<void> {
    return Promise.resolve();
  }

  shutdown(): Promise<void> {
    return Promise.resolve();
  }
}
