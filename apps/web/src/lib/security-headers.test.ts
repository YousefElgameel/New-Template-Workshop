import { describe, expect, it } from 'vitest';
import nextConfig from '../../next.config';
import {
  API_CSP,
  PERMISSIONS_POLICY,
  STATIC_SECURITY_HEADERS,
  cspFor,
  isApiPath,
  newNonce,
  pageCsp,
} from '@/lib/security-headers';

const directives = (csp: string) =>
  Object.fromEntries(
    csp.split(';').map((d) => {
      const [name, ...values] = d.trim().split(/\s+/);
      return [name, values];
    }),
  );

describe('newNonce', () => {
  it('is base64, 128 bits, and different every time', () => {
    const nonces = new Set(Array.from({ length: 200 }, () => newNonce()));
    expect(nonces.size).toBe(200);
    for (const nonce of nonces) {
      expect(nonce).toMatch(/^[A-Za-z0-9+/]{22}==$/);
    }
  });
});

describe('pageCsp', () => {
  const csp = directives(pageCsp('NONCE=='));

  it('allows only the app itself', () => {
    expect(csp['default-src']).toEqual(["'self'"]);
    expect(csp['img-src']).toEqual(["'self'"]);
    expect(csp['connect-src']).toEqual(["'self'"]);
    expect(csp['font-src']).toEqual(["'self'"]);
  });

  it('allows scripts and styles by nonce, never inline or eval, in production', () => {
    expect(csp['script-src']).toEqual(["'self'", "'nonce-NONCE=='", "'strict-dynamic'"]);
    expect(csp['style-src']).toEqual(["'self'", "'nonce-NONCE=='"]);
    expect(pageCsp('N==')).not.toContain('unsafe-inline');
    expect(pageCsp('N==')).not.toContain('unsafe-eval');
  });

  it('adds eval for development only', () => {
    expect(directives(pageCsp('N==', true))['script-src']).toContain("'unsafe-eval'");
    // and inline styles, because the dev server injects <style> without a nonce; production does not
    expect(directives(pageCsp('N==', true))['style-src']).toContain("'unsafe-inline'");
  });

  it('forbids framing, plugins, a changed base and foreign form targets', () => {
    expect(csp['frame-ancestors']).toEqual(["'none'"]);
    expect(csp['object-src']).toEqual(["'none'"]);
    expect(csp['base-uri']).toEqual(["'self'"]);
    expect(csp['form-action']).toEqual(["'self'"]);
  });

  it('puts the nonce it is given in the policy, and nothing else varies', () => {
    expect(pageCsp('A==')).toContain("'nonce-A=='");
    expect(pageCsp('A==').replaceAll('A==', 'X')).toBe(pageCsp('B==').replaceAll('B==', 'X'));
  });
});

describe('the policy of a JSON route', () => {
  it('allows nothing and cannot be framed', () => {
    expect(directives(API_CSP)).toEqual({
      'default-src': ["'none'"],
      'frame-ancestors': ["'none'"],
    });
  });

  it.each([
    '/api/v1/conversations',
    '/api/ping-backend',
    '/api',
    '/health',
    '/%61pi/ping-backend',
    '/%68ealth',
    '/api/%zz',
  ])('is used for %s', (path) => {
    expect(isApiPath(path)).toBe(true);
    expect(cspFor(path, 'N==')).toBe(API_CSP);
  });

  it.each(['/', '/chat', '/healthy', '/apixyz', '/chat/api/x'])(
    'is not used for the page %s',
    (path) => {
      expect(isApiPath(path)).toBe(false);
      expect(cspFor(path, 'N==')).toBe(pageCsp('N=='));
    },
  );
});

describe('the static headers', () => {
  const byKey = Object.fromEntries(STATIC_SECURITY_HEADERS.map((h) => [h.key, h.value]));

  it('has nosniff and the referrer policy', () => {
    expect(byKey['X-Content-Type-Options']).toBe('nosniff');
    expect(byKey['Referrer-Policy']).toBe('strict-origin-when-cross-origin');
  });

  it('turns the sensors, payment and USB off', () => {
    for (const feature of [
      'camera',
      'microphone',
      'geolocation',
      'accelerometer',
      'gyroscope',
      'magnetometer',
      'payment',
      'usb',
    ]) {
      expect(byKey['Permissions-Policy']).toContain(`${feature}=()`);
    }
    expect(PERMISSIONS_POLICY).not.toContain('*');
  });

  it('are set for every path by next.config.ts', async () => {
    const rules = await nextConfig.headers!();
    expect(rules).toHaveLength(1);
    expect(rules[0].source).toBe('/:path*');
    expect(rules[0].headers).toEqual(STATIC_SECURITY_HEADERS);
  });
});
