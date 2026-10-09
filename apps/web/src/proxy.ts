import { NextResponse, type NextRequest } from 'next/server';
import { cspFor, newNonce } from '@/lib/security-headers';

/**
 * Sets `Content-Security-Policy` on every response (web roadmap 3.5). The policy is also put on the
 * *request*: Next reads the nonce from it while rendering and applies it to its own scripts and
 * styles. Runs before the route handlers, so `withBff`'s own headers (`x-trace-id`) are untouched.
 */
export function proxy(request: NextRequest): NextResponse {
  const nonce = newNonce();
  const csp = cspFor(request.nextUrl.pathname, nonce, process.env.NODE_ENV === 'development');

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set('x-nonce', nonce);
  requestHeaders.set('Content-Security-Policy', csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set('Content-Security-Policy', csp);
  return response;
}

export const config = {
  // Everything except the build's own static files, which are not documents.
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
