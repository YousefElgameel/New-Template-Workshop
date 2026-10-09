/**
 * Limits on asks in flight at the BFF (web roadmap 3.4; threat model G1) and the same-origin check on
 * its write routes (G4).
 *
 * An ask holds one of the api's few concurrency slots (`AI_MAX_CONCURRENT_ASKS`, 4) for up to its
 * deadline and spends model quota shared with other use cases, and the page has no login. So the BFF
 * caps how many asks it has in flight — in total for this instance, and per client — and answers the
 * api's own `429 busy` shape when a cap is reached, before anything is sent upstream.
 *
 * The client is the **rightmost** `X-Forwarded-For` address: Azure Container Apps appends the address
 * it saw and passes the rest through unchanged, so everything to the left is whatever the client
 * claimed. With no such header (local development, tests) every request shares one bucket. Addresses
 * are only ever keys of an in-memory map: never logged, never put on a span or a metric.
 *
 * A slot is released exactly once, whatever happens: the answer ends, fails, the upstream cannot be
 * reached, or the browser goes away mid-stream.
 */

import { isIP } from 'node:net';

export const DEFAULT_MAX_INFLIGHT = 8;
export const DEFAULT_MAX_INFLIGHT_PER_CLIENT = 2;
/** A configured value above this is a mistake; the default applies. */
export const MAX_CONFIGURED_LIMIT = 1000;
/** The bucket of every request that carries no usable forwarded address. */
export const SHARED_CLIENT = 'shared';
const MAX_ADDRESS_LENGTH = 64;

export interface AskLimits {
  maxInflight: number;
  maxPerClient: number;
}

/** Proxies that sit between the ingress and the app and append their own address (0 = none). */
export const MAX_TRUSTED_PROXY_HOPS = 5;

/** `ASK_TRUSTED_PROXY_HOPS`: a whole number from 0 to 5; anything else is 0. */
export function readProxyHops(raw: string | undefined): number {
  const text = (raw ?? '').trim();
  if (!/^\d$/.test(text)) return 0;
  const value = Number(text);
  return value <= MAX_TRUSTED_PROXY_HOPS ? value : 0;
}

/** A whole number from 1 to 1000; anything else (unset, blank, a typo, 0, a huge value) is `fallback`. */
export function readLimit(raw: string | undefined, fallback: number): number {
  const text = (raw ?? '').trim();
  if (!/^\d{1,4}$/.test(text)) return fallback;
  const value = Number(text);
  return value >= 1 && value <= MAX_CONFIGURED_LIMIT ? value : fallback;
}

/** The limits from the server-side environment, read at each acquire so a restart is not needed in tests. */
export function readAskLimits(env: Record<string, string | undefined> = process.env): AskLimits {
  return {
    maxInflight: readLimit(env.ASK_MAX_INFLIGHT, DEFAULT_MAX_INFLIGHT),
    maxPerClient: readLimit(env.ASK_MAX_INFLIGHT_PER_CLIENT, DEFAULT_MAX_INFLIGHT_PER_CLIENT),
  };
}

/** The eight 16-bit groups of an IPv6 address (`::` expanded, an embedded IPv4 tail converted), or null. */
function ipv6Groups(address: string): number[] | null {
  let text = address;
  const tail = /(\d+\.\d+\.\d+\.\d+)$/.exec(text);
  if (tail) {
    if (isIP(tail[1]) !== 4) return null;
    const [a, b, c, d] = tail[1].split('.').map(Number);
    text = `${text.slice(0, -tail[1].length)}${((a << 8) | b).toString(16)}:${((c << 8) | d).toString(16)}`;
  }
  const halves = text.split('::');
  if (halves.length > 2) return null;
  const head = halves[0] ? halves[0].split(':') : [];
  const rest = halves.length === 2 && halves[1] ? halves[1].split(':') : [];
  if (halves.length === 1 ? head.length !== 8 : head.length + rest.length > 7) return null;
  const fill: string[] = halves.length === 2 ? Array(8 - head.length - rest.length).fill('0') : [];
  const groups = [...head, ...fill, ...rest].map((g) =>
    /^[0-9a-f]{1,4}$/i.test(g) ? parseInt(g, 16) : NaN,
  );
  return groups.length === 8 && groups.every((g) => !Number.isNaN(g)) ? groups : null;
}

/**
 * One client, one key, whatever form the address takes: an IPv4 address is itself (a `:port` or a
 * zone is dropped); an IPv4-mapped IPv6 address is that IPv4 address; any other IPv6 address is
 * its /64 network (a client owns a whole /64, so rotating inside it must not buy more slots).
 * Anything that is not an address is `null`.
 */
export function normaliseAddress(raw: string): string | null {
  let text = raw.trim();
  if (text.startsWith('[')) {
    const close = text.indexOf(']');
    if (close === -1) return null;
    text = text.slice(1, close);
  } else if (/^\d+\.\d+\.\d+\.\d+:\d{1,5}$/.test(text)) {
    text = text.slice(0, text.lastIndexOf(':'));
  }
  const zone = text.indexOf('%');
  if (zone !== -1) text = text.slice(0, zone);
  const kind = isIP(text);
  if (kind === 4) return text;
  if (kind !== 6) return null;
  const groups = ipv6Groups(text.toLowerCase());
  if (!groups) return null;
  if (groups.slice(0, 5).every((g) => g === 0) && groups[5] === 0xffff) {
    return `${groups[6] >> 8}.${groups[6] & 255}.${groups[7] >> 8}.${groups[7] & 255}`;
  }
  return `${groups
    .slice(0, 4)
    .map((g) => g.toString(16))
    .join(':')}::/64`;
}

/**
 * The client key of a request: the `X-Forwarded-For` entry `hops` places from the right (0 = the
 * rightmost, the address the ingress saw), normalised, or {@link SHARED_CLIENT}. Entries further left
 * are whatever the client claimed and are never used; an entry that is empty, too long or not an
 * address is the shared bucket, not the next one over. `hops` is `ASK_TRUSTED_PROXY_HOPS`: the number
 * of proxies in front of the ingress that append their own address.
 */
export function clientKeyOf(
  req: Request,
  hops: number = readProxyHops(process.env.ASK_TRUSTED_PROXY_HOPS),
): string {
  const header = req.headers.get('x-forwarded-for');
  if (!header) return SHARED_CLIENT;
  const parts = header.split(',');
  const entry = (parts[parts.length - 1 - hops] ?? '').trim();
  if (entry === '' || entry.length > MAX_ADDRESS_LENGTH) return SHARED_CLIENT;
  return normaliseAddress(entry) ?? SHARED_CLIENT;
}

export type Release = () => void;
export type Refusal = 'instance' | 'client';
export type Acquired = { ok: true; release: Release } | { ok: false; why: Refusal };

export class AskLimiter {
  private total = 0;
  private readonly perClient = new Map<string, number>();

  /** Take a slot for `client`, or say which cap is reached. Counts only; nothing is logged here. */
  tryAcquire(client: string, limits: AskLimits = readAskLimits()): Acquired {
    if (this.total >= limits.maxInflight) return { ok: false, why: 'instance' };
    const held = this.perClient.get(client) ?? 0;
    if (held >= limits.maxPerClient) return { ok: false, why: 'client' };
    this.total += 1;
    this.perClient.set(client, held + 1);
    let released = false;
    return {
      ok: true,
      release: () => {
        if (released) return;
        released = true;
        this.total -= 1;
        const now = (this.perClient.get(client) ?? 1) - 1;
        if (now <= 0) this.perClient.delete(client);
        else this.perClient.set(client, now);
      },
    };
  }

  /** Counts for tests and diagnostics: asks in flight, and how many clients hold one. */
  snapshot(): { inflight: number; clients: number } {
    return { inflight: this.total, clients: this.perClient.size };
  }
}

/** One limiter per process, shared by every module graph Next builds (as `trace.ts` does). */
const KEY = Symbol.for('team-assistant.web.ask-limiter');
type Store = Record<symbol, unknown>;

export function askLimiter(): AskLimiter {
  const store = globalThis as unknown as Store;
  let limiter = store[KEY] as AskLimiter | undefined;
  if (!limiter) {
    limiter = new AskLimiter();
    store[KEY] = limiter;
  }
  return limiter;
}

/** TEST-ONLY: a fresh limiter. */
export function __resetAskLimiterForTests(): void {
  delete (globalThis as unknown as Store)[KEY];
}

/**
 * Pass `body` through unchanged and call `release` exactly once when it ends — normally, with an
 * error, or because the reader (the browser) cancelled it. A `null` body releases at once.
 *
 * `maxHoldMs` is a backstop: a reader that stops reading without the connection closing would
 * otherwise hold the slot for as long as the ingress lets it. When it elapses the slot is released
 * and the source cancelled (the answer is over: the hop timeout is shorter).
 */
export function releaseWhenDone(
  body: ReadableStream<Uint8Array> | null,
  release: Release,
  maxHoldMs?: number,
): ReadableStream<Uint8Array> | null {
  if (!body) {
    release();
    return null;
  }
  const reader = body.getReader();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let finished = false;
  const finish = () => {
    if (timer !== undefined) clearTimeout(timer);
    if (finished) return;
    finished = true;
    release();
  };
  if (maxHoldMs !== undefined) {
    timer = setTimeout(() => {
      finish();
      void reader.cancel().catch(() => undefined);
    }, maxHoldMs);
    timer.unref?.();
  }
  return new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        const { done, value } = await reader.read();
        if (done) {
          finish();
          controller.close();
          return;
        }
        controller.enqueue(value);
      } catch (err) {
        finish();
        controller.error(err);
      }
    },
    async cancel(reason) {
      finish();
      await reader.cancel(reason).catch(() => undefined);
    },
  });
}

/**
 * Whether a state-changing request may come from where it says it does. Refused when `Origin` is
 * present and is not this app's own host (a cross-site page; `Origin: null` too), or when the browser
 * says `Sec-Fetch-Site: cross-site`. A request with neither header (curl, a test) passes: this is a
 * defence against a browser being used by another site, not an authentication.
 *
 * The comparison is on the host (with port), not the full origin: behind the ingress the request
 * reaches the app as http while the browser's origin is https.
 */
export function isSameOriginRequest(req: Request): boolean {
  if (req.headers.get('sec-fetch-site')?.trim().toLowerCase() === 'cross-site') return false;
  const origin = req.headers.get('origin');
  if (origin === null) return true;
  let originHost: string;
  try {
    originHost = new URL(origin).host;
  } catch {
    return false; // `null`, or anything that is not an origin
  }
  const own = (req.headers.get('host') ?? safeHost(req.url)).trim().toLowerCase();
  return originHost.toLowerCase() === own;
}

function safeHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return '';
  }
}
