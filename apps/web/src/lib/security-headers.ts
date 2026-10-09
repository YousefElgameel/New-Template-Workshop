/**
 * Security headers of every response of the web app (web roadmap 3.5; threat model G5).
 *
 * - `Content-Security-Policy` is built per request by `src/proxy.ts`: a page gets a policy with a
 *   fresh nonce (Next applies it to its own scripts and styles while rendering, which is why every
 *   page is rendered dynamically); an `/api` or `/health` response is JSON and gets a policy that
 *   allows nothing.
 * - The other three are fixed, set for every path by `next.config.ts`.
 *
 * Pure functions, no Next imports: `next.config.ts` imports this file.
 */

/** A fresh, unpredictable nonce: 128 random bits, base64. */
export function newNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes));
}

/** Directives joined into one header value. */
function joinDirectives(directives: string[]): string {
  return directives.join('; ');
}

/**
 * The policy of an HTML page. `'strict-dynamic'` lets the nonced framework scripts load their own
 * chunks. Development only: `'unsafe-eval'` (React rebuilds server stacks with `eval`) and inline styles
 * (the dev server injects `<style>` tags without a nonce) — as Next's own CSP guide does.
 */
export function pageCsp(nonce: string, dev = false): string {
  return joinDirectives([
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ''}`,
    `style-src 'self' ${dev ? "'unsafe-inline'" : `'nonce-${nonce}'`}`,
    "img-src 'self'",
    "connect-src 'self'",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ]);
}

/** The policy of a JSON response (`/api/**`, `/health`): nothing may load or embed it. */
export const API_CSP = "default-src 'none'; frame-ancestors 'none'";

/**
 * Whether a request path is a JSON route rather than a page. The path is decoded first (`/%61pi/x` is
 * `/api/x` to Next's router); one that cannot be decoded is treated as a JSON route, the strict policy.
 */
export function isApiPath(pathname: string): boolean {
  let path: string;
  try {
    path = decodeURIComponent(pathname);
  } catch {
    return true;
  }
  return path === '/health' || path === '/api' || path.startsWith('/api/');
}

export function cspFor(pathname: string, nonce: string, dev = false): string {
  return isApiPath(pathname) ? API_CSP : pageCsp(nonce, dev);
}

/** Sensors, payment and USB are off: this app uses none of them. */
export const PERMISSIONS_POLICY = [
  'accelerometer=()',
  'camera=()',
  'geolocation=()',
  'gyroscope=()',
  'magnetometer=()',
  'microphone=()',
  'payment=()',
  'usb=()',
].join(', ');

/** The headers that do not vary per request. */
export const STATIC_SECURITY_HEADERS: ReadonlyArray<{ key: string; value: string }> = [
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  { key: 'Permissions-Policy', value: PERMISSIONS_POLICY },
];
