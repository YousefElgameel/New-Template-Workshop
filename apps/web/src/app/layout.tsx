import type { Metadata } from 'next';
import { connection } from 'next/server';
import './globals.css';

export const metadata: Metadata = {
  title: 'Team Assistant',
  description: 'Team Assistant — web BFF + UI',
};

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  // Every page is rendered per request: the Content-Security-Policy carries a fresh nonce
  // (`src/proxy.ts`), and Next can only apply a nonce while rendering for a request.
  await connection();
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
