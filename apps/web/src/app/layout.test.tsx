import { describe, expect, it, vi } from 'vitest';

const { connection } = vi.hoisted(() => ({ connection: vi.fn(async () => undefined) }));
vi.mock('next/server', () => ({ connection }));
vi.mock('./globals.css', () => ({}));

import RootLayout from '@/app/layout';

describe('RootLayout', () => {
  it('waits for a request before rendering, so every page can carry the per-request CSP nonce', async () => {
    connection.mockClear();
    const tree = await RootLayout({ children: null });
    expect(connection).toHaveBeenCalledTimes(1);
    expect(tree.type).toBe('html');
  });
});
