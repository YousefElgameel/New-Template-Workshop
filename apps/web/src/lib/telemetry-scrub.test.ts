import { createServer, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { SpanKind, trace } from '@opentelemetry/api';
import { registerInstrumentations } from '@opentelemetry/instrumentation';
import { UndiciInstrumentation } from '@opentelemetry/instrumentation-undici';
import {
  InMemorySpanExporter,
  SimpleSpanProcessor,
  type ReadableSpan,
  type SpanProcessor,
} from '@opentelemetry/sdk-trace-base';
import { NodeTracerProvider } from '@opentelemetry/sdk-trace-node';
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest';
import {
  IdScrubSpanProcessor,
  scrubIds,
  scrubSpanAttributes,
  scrubbedRequestAttributes,
} from './telemetry-scrub';

const ID = '5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e01';
const OTHER_ID = 'C0000000-0000-4000-8000-00000000000A';

/** Every attribute value of every exported span, joined — "does the id appear anywhere?". */
function everything(spans: ReadableSpan[]): string {
  return JSON.stringify(spans.map((s) => ({ name: s.name, attributes: s.attributes })));
}

describe('scrubIds', () => {
  it('replaces every UUID, in any case, and nothing else', () => {
    expect(scrubIds(`/api/v1/conversations/${ID}/ask/stream`)).toBe(
      '/api/v1/conversations/{id}/ask/stream',
    );
    expect(scrubIds(`/a/${ID}/b/${OTHER_ID}?limit=5`)).toBe('/a/{id}/b/{id}?limit=5');
    expect(scrubIds('/api/v1/conversations?limit=5')).toBe('/api/v1/conversations?limit=5');
    expect(scrubIds('/health')).toBe('/health');
    expect(scrubIds('5b1f3a52-8c1e-4d3a-9d0b')).toBe('5b1f3a52-8c1e-4d3a-9d0b'); // not a UUID
  });

  it('is repeatable (the pattern is global, so a stateful test would alternate)', () => {
    for (let i = 0; i < 4; i += 1) expect(scrubIds(`/x/${ID}`)).toBe('/x/{id}');
  });
});

describe('scrubbedRequestAttributes (the undici startSpanHook)', () => {
  it('returns the URL attributes without the id', () => {
    expect(
      scrubbedRequestAttributes({
        origin: 'http://api:8000',
        path: `/v1/conversations/${ID}/ask/stream?x=1`,
      }),
    ).toEqual({
      'url.full': 'http://api:8000/v1/conversations/{id}/ask/stream?x=1',
      'url.path': '/v1/conversations/{id}/ask/stream',
    });
  });

  it('never throws: an unparseable request gives no attributes', () => {
    expect(scrubbedRequestAttributes({ origin: 'not a url', path: '::' })).toEqual({});
    expect(scrubbedRequestAttributes({ origin: '', path: '' })).toEqual({});
  });
});

describe('the span processor, with an in-memory exporter', () => {
  const exporter = new InMemorySpanExporter();
  afterEach(() => exporter.reset());

  function providerWith(...processors: SpanProcessor[]): NodeTracerProvider {
    return new NodeTracerProvider({ spanProcessors: processors });
  }

  const attributesWithId = {
    'http.target': `/api/v1/conversations/${ID}`,
    'http.url': `http://web:3000/api/v1/conversations/${ID}?limit=2`,
    'url.path': `/api/v1/conversations/${ID}`,
    'url.full': `http://web:3000/api/v1/conversations/${ID}`,
    'http.route': '/api/v1/conversations/[id]',
    'http.method': 'GET',
  };

  it('CONTROL: without the processor the id is on the exported span', () => {
    const provider = providerWith(new SimpleSpanProcessor(exporter));
    provider
      .getTracer('t')
      .startSpan('GET', { kind: SpanKind.SERVER, attributes: attributesWithId })
      .end();
    const [span] = exporter.getFinishedSpans();
    for (const key of ['http.target', 'http.url', 'url.path', 'url.full']) {
      expect(String(span.attributes[key])).toContain(ID);
    }
    expect(everything([span])).toContain(ID);
  });

  it('with the processor, no exported attribute holds the id, and the rest is untouched', () => {
    const provider = providerWith(
      new IdScrubSpanProcessor() as unknown as SpanProcessor,
      new SimpleSpanProcessor(exporter),
    );
    provider
      .getTracer('t')
      .startSpan('GET', { kind: SpanKind.SERVER, attributes: attributesWithId })
      .end();
    const [span] = exporter.getFinishedSpans();
    expect(everything([span])).not.toContain(ID);
    expect(span.attributes['http.target']).toBe('/api/v1/conversations/{id}');
    expect(span.attributes['url.path']).toBe('/api/v1/conversations/{id}');
    expect(span.attributes['http.url']).toBe('http://web:3000/api/v1/conversations/{id}?limit=2');
    expect(span.attributes['url.full']).toBe('http://web:3000/api/v1/conversations/{id}');
    expect(span.attributes['http.route']).toBe('/api/v1/conversations/[id]');
    expect(span.attributes['http.method']).toBe('GET');
  });

  it("scrubs Next's own copy of the fetch span name, and the span name itself", () => {
    const provider = providerWith(
      new IdScrubSpanProcessor() as unknown as SpanProcessor,
      new SimpleSpanProcessor(exporter),
    );
    const fetchSpan = `fetch GET http://api:8000/v1/conversations/${ID}`;
    provider
      .getTracer('t')
      .startSpan(fetchSpan, {
        attributes: {
          'next.span_name': fetchSpan,
          'http.url': `http://api:8000/v1/conversations/${ID}`,
        },
      })
      .end();
    const [span] = exporter.getFinishedSpans();
    expect(everything([span])).not.toContain(ID);
    expect(span.name).toBe('fetch GET http://api:8000/v1/conversations/{id}');
    expect(span.attributes['next.span_name']).toBe(
      'fetch GET http://api:8000/v1/conversations/{id}',
    );
  });

  it("CONTROL: without the processor Next's span name carries the id", () => {
    const provider = providerWith(new SimpleSpanProcessor(exporter));
    const fetchSpan = `fetch GET http://api:8000/v1/conversations/${ID}`;
    provider
      .getTracer('t')
      .startSpan(fetchSpan, { attributes: { 'next.span_name': fetchSpan } })
      .end();
    expect(everything(exporter.getFinishedSpans())).toContain(ID);
    expect(exporter.getFinishedSpans()[0].name).toContain(ID);
  });

  it('also covers an attribute written after the span started', () => {
    const provider = providerWith(
      new IdScrubSpanProcessor() as unknown as SpanProcessor,
      new SimpleSpanProcessor(exporter),
    );
    const span = provider.getTracer('t').startSpan('GET', { kind: SpanKind.SERVER });
    span.setAttribute('http.target', `/api/v1/conversations/${ID}/ask`);
    span.end();
    expect(everything(exporter.getFinishedSpans())).not.toContain(ID);
    expect(exporter.getFinishedSpans()[0].attributes['http.target']).toBe(
      '/api/v1/conversations/{id}/ask',
    );
  });

  it('even when the exporter runs first (a batch exporter reads the attributes later)', () => {
    const queued: ReadableSpan[] = [];
    const provider = providerWith(
      {
        onStart() {},
        onEnd: (s) => queued.push(s),
        forceFlush: async () => {},
        shutdown: async () => {},
      },
      new IdScrubSpanProcessor() as unknown as SpanProcessor,
    );
    const span = provider.getTracer('t').startSpan('GET', { kind: SpanKind.SERVER });
    span.setAttribute('http.target', `/api/v1/conversations/${ID}`);
    span.end();
    // The queue holds the span the exporter will read later: the id must be gone by then.
    expect(queued).toHaveLength(1);
    expect(everything(queued)).not.toContain(ID);
  });
});

describe('fail-safe: telemetry can never break a request', () => {
  it('survives a span that throws on every access', () => {
    const hostile = {
      get attributes(): never {
        throw new Error('boom');
      },
      setAttribute: () => {
        throw new Error('boom');
      },
      isRecording: () => {
        throw new Error('boom');
      },
    };
    expect(() => scrubSpanAttributes(hostile)).not.toThrow();
    const processor = new IdScrubSpanProcessor();
    expect(() => processor.onStart(hostile as never, {} as never)).not.toThrow();
    expect(() => processor.onEnd(hostile)).not.toThrow();
    expect(() => processor.onEnd(undefined)).not.toThrow();
    expect(() => processor.onEnd(null)).not.toThrow();
    expect(() =>
      processor.onEnd({ attributes: Object.freeze({ 'http.target': `/x/${ID}` }) }),
    ).not.toThrow();
  });

  it('leaves a span that is not recording alone, and one without attributes', () => {
    let touched = false;
    scrubSpanAttributes({
      attributes: { 'http.target': `/x/${ID}` },
      isRecording: () => false,
      setAttribute: () => {
        touched = true;
      },
    });
    expect(touched).toBe(false);
    expect(() => scrubSpanAttributes({})).not.toThrow();
  });

  it('onStart rewrites the attributes of a span that has just been created', () => {
    const written: Record<string, string> = {};
    new IdScrubSpanProcessor().onStart(
      {
        attributes: { 'http.target': `/x/${ID}`, 'url.path': '/health' },
        isRecording: () => true,
        setAttribute: (key: string, value: string) => {
          written[key] = value;
        },
      } as never,
      {} as never,
    );
    expect(written).toEqual({ 'http.target': '/x/{id}' });
  });

  it('is a real SpanProcessor of the installed SDK (a compile-time check of the cast)', () => {
    const processor: SpanProcessor = new IdScrubSpanProcessor();
    expect(processor).toBeInstanceOf(IdScrubSpanProcessor);
  });

  it('one attribute that cannot be written does not leave the others unscrubbed', () => {
    const attributes: Record<string, string> = {
      'http.target': `/x/${ID}`,
      'http.url': `http://web/x/${ID}`,
    };
    Object.defineProperty(attributes, 'http.target', {
      value: `/x/${ID}`,
      writable: false,
      enumerable: true,
    });
    const processor = new IdScrubSpanProcessor();
    processor.onEnd({ attributes });
    expect(attributes['http.url']).toBe('http://web/x/{id}');
  });

  it('flush and shutdown resolve', async () => {
    const processor = new IdScrubSpanProcessor();
    await expect(processor.forceFlush()).resolves.toBeUndefined();
    await expect(processor.shutdown()).resolves.toBeUndefined();
  });
});

describe('the outbound span of a real fetch (undici instrumentation)', () => {
  let server: Server;
  let base: string;

  beforeAll(async () => {
    server = createServer((_req, res) => {
      res.setHeader('content-type', 'application/json');
      res.end('{}');
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });

  afterAll(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  async function exportedSpans(options: ConstructorParameters<typeof UndiciInstrumentation>[0]) {
    const exporter = new InMemorySpanExporter();
    const provider = new NodeTracerProvider({
      spanProcessors: [new SimpleSpanProcessor(exporter)],
    });
    provider.register();
    const unregister = registerInstrumentations({
      instrumentations: [new UndiciInstrumentation(options)],
      tracerProvider: provider,
    });
    try {
      const res = await fetch(`${base}/v1/conversations/${ID}?limit=3`);
      await res.text();
      await provider.forceFlush();
      return exporter.getFinishedSpans();
    } finally {
      unregister();
      await provider.shutdown();
      trace.disable();
    }
  }

  it('CONTROL: without the hook the outbound span carries the id', async () => {
    const spans = await exportedSpans({});
    expect(spans.length).toBeGreaterThan(0);
    expect(everything(spans)).toContain(ID);
  });

  it('with the hook the span never carries it, and keeps the rest of the URL', async () => {
    const spans = await exportedSpans({ startSpanHook: scrubbedRequestAttributes });
    expect(spans.length).toBeGreaterThan(0);
    expect(everything(spans)).not.toContain(ID);
    const [span] = spans;
    expect(span.attributes['url.path']).toBe('/v1/conversations/{id}');
    expect(String(span.attributes['url.full'])).toBe(`${base}/v1/conversations/{id}?limit=3`);
    expect(span.attributes['http.response.status_code']).toBe(200);
  });
});
