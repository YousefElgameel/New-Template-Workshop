import { NextRequest } from 'next/server';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { config, proxy } from '@/proxy';
import { API_CSP } from '@/lib/security-headers';

const call = (path: string, headers: Record<string, string> = {}) =>
  proxy(new NextRequest(`http://localhost:3000${path}`, { headers }));

const nonceOf = (csp: string) => /'nonce-([^']+)'/.exec(csp)?.[1];

// `NextResponse.next({ request })` reports the headers it hands to the router as `x-middleware-request-*`;
// reading those is the way to test it without a server. A Next upgrade that renames them fails here first.
describe('proxy', () => {
  afterEach(() => vi.unstubAllEnvs());

  it('has no eval in production and only in development', () => {
    vi.stubEnv('NODE_ENV', 'production');
    expect(call('/chat').headers.get('content-security-policy')).not.toContain('unsafe-eval');
    vi.stubEnv('NODE_ENV', 'development');
    expect(call('/chat').headers.get('content-security-policy')).toContain("'unsafe-eval'");
    expect(call('/health').headers.get('content-security-policy')).toBe(API_CSP);
  });

  it('sets a nonce policy on a page response', () => {
    const csp = call('/chat').headers.get('content-security-policy')!;
    expect(csp).toContain("default-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(nonceOf(csp)).toMatch(/^[A-Za-z0-9+/]{22}==$/);
  });

  it('gives Next the same policy and nonce on the request, which is how the nonce reaches the page', () => {
    const res = call('/chat');
    const csp = res.headers.get('content-security-policy')!;
    const forwarded = res.headers.get('x-middleware-request-content-security-policy');
    expect(forwarded).toBe(csp);
    expect(res.headers.get('x-middleware-request-x-nonce')).toBe(nonceOf(csp));
  });

  it('uses a fresh nonce for every request', () => {
    const nonces = new Set(
      Array.from({ length: 50 }, () => nonceOf(call('/').headers.get('content-security-policy')!)),
    );
    expect(nonces.size).toBe(50);
  });

  it.each(['/api/v1/conversations', '/api/ping-backend', '/health'])(
    'sets the closed policy on %s',
    (path) => {
      expect(call(path).headers.get('content-security-policy')).toBe(API_CSP);
    },
  );

  it('ignores a policy or nonce the client sends', () => {
    const res = call('/chat', {
      'content-security-policy': "default-src *; script-src 'unsafe-inline'",
      'x-nonce': 'forged',
    });
    const csp = res.headers.get('content-security-policy')!;
    expect(csp).not.toContain('unsafe-inline');
    expect(csp).not.toContain('*');
    expect(res.headers.get('x-middleware-request-content-security-policy')).toBe(csp);
    expect(res.headers.get('x-middleware-request-x-nonce')).not.toBe('forged');
  });

  it('leaves out the build’s static files and nothing a page needs (as a plain regular expression)', () => {
    const [pattern] = config.matcher;
    const re = new RegExp(`^${pattern}$`);
    expect(re.test('/chat')).toBe(true);
    expect(re.test('/api/v1/conversations')).toBe(true);
    expect(re.test('/health')).toBe(true);
    expect(re.test('/_next/static/chunks/a.js')).toBe(false);
    expect(re.test('/_next/image')).toBe(false);
    expect(re.test('/favicon.ico')).toBe(false);
  });
});
