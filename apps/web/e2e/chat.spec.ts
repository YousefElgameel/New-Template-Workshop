import { expect, test, type Response } from '@playwright/test';

/**
 * `/chat` smoke e2e — MANUAL/LOCAL ONLY (needs the full stack up: web + api, the api's migrations
 * applied and the corpus ingested, so that an answer can cite a document).
 *   make up   (or make dev),  then:  pnpm -C apps/web test:e2e e2e/chat.spec.ts
 *
 * One journey: open `/chat`, start a new chat, ask a question, see an answer with at least one
 * citation, then reload the page and reopen that conversation from the list.
 *
 * Run it **against the real api**: with `MOCK_UPSTREAM=true` the web serves fixtures, the new
 * conversation is not stored, and the last step cannot be proved — the test says so and stops
 * there instead of passing it. Conversations are shared until auth lands (ADR-0014), and this one
 * stays in the list; its title starts with `e2e-smoke` so a person can recognise it.
 */

// A model answer can take minutes in the worst case (the web's hop timeout is 5 minutes).
const ANSWER_TIMEOUT = 300_000;

// The sidebar is a drawer below 768 px: pin a desktop viewport so the list is on the page.
test.use({ viewport: { width: 1280, height: 800 } });

test('chat: new chat, ask a question, see the answer with a citation, reopen it from the list', async ({
  page,
}) => {
  test.setTimeout(ANSWER_TIMEOUT + 60_000);

  // Whether the web answered from fixtures: its mocked responses carry `x-mock-upstream: true`.
  let mocked = false;
  page.on('response', (response: Response) => {
    if (response.url().includes('/api/v1/conversations') && response.headers()['x-mock-upstream']) {
      mocked = true;
    }
  });

  const stamp = Date.now();
  const question = `e2e-smoke ${stamp}: How do I run the tests?`;
  const list = page.getByRole('navigation', { name: 'Conversations', exact: true });
  const thread = page.getByRole('list', { name: 'Conversation', exact: true });

  await test.step('open /chat and wait for the conversation list', async () => {
    await page.goto('/chat');
    await expect(list).toBeVisible();
    await expect(list.getByRole('button', { name: '+ New chat' })).toBeEnabled();
    // either the rows or "No chats yet" once the list has loaded
    await expect(
      list.getByRole('button').nth(1).or(page.getByTestId('conversations-empty')),
    ).toBeVisible({ timeout: 15_000 });
  });

  await test.step('start a new chat', async () => {
    await list.getByRole('button', { name: '+ New chat' }).click();
    await expect(page.getByTestId('chat-empty')).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Message' })).toBeFocused();
  });

  await test.step('ask a question and see the answer stream in', async () => {
    await page.getByRole('textbox', { name: 'Message' }).fill(question);
    await page.getByRole('button', { name: 'Send' }).click();

    const asked = thread.locator('li[data-role="user"]');
    await expect(asked).toContainText(question);

    // The answer is done when its message stops being `aria-busy` (the newest message is busy while
    // it streams). A failed answer is removed and an error line appears instead: stop at once on
    // that, with the error in the message, rather than waiting out the timeout.
    const answer = thread.locator('li[data-role="assistant"]');
    const finished = thread.locator('li[data-role="assistant"][aria-busy="false"]');
    const failed = thread.getByRole('alert');
    await expect(finished.or(failed)).toBeVisible({ timeout: ANSWER_TIMEOUT });
    await expect(failed, 'the answer failed: an error line is under the question').toHaveCount(0);
    await expect(answer).toHaveCount(1);
    await expect(answer.getByTestId('message-text')).not.toHaveText('');
  });

  await test.step('the answer cites at least one document', async () => {
    const sources = thread
      .locator('li[data-role="assistant"]')
      .getByRole('list', { name: 'Sources' })
      .getByRole('listitem');
    await expect(sources.first()).toBeVisible();
    expect(await sources.count()).toBeGreaterThanOrEqual(1);
    // a document path, shown as plain text (never a link)
    await expect(sources.first().locator('code')).toContainText(/^docs\//);
    await expect(sources.first().locator('a')).toHaveCount(0);
  });

  // Reported as SKIPPED (not a pass): the first half ran, the reopen half cannot be tested.
  test.skip(
    mocked,
    'MOCK_UPSTREAM is on (x-mock-upstream seen): the conversation is not stored, so reopening it cannot be tested. Run against the real api.',
  );

  await test.step('reload and reopen the conversation from the list', async () => {
    await page.reload();
    await expect(list).toBeVisible();
    // an untitled conversation took the question as its title (flattened, at most 200 characters)
    const row = list.getByRole('button', { name: new RegExp(`e2e-smoke ${stamp}:`) });
    await expect(row).toBeVisible({ timeout: 15_000 });
    await expect(row).toContainText(question);
    await row.click();
    await expect(row).toHaveAttribute('aria-current', 'true');

    await expect(thread.locator('li[data-role="user"]')).toContainText(question);
    const stored = thread.locator('li[data-role="assistant"]');
    await expect(stored).toHaveCount(1);
    await expect(stored.getByTestId('message-text')).not.toHaveText('');
    await expect(
      stored.getByRole('list', { name: 'Sources' }).getByRole('listitem').first(),
    ).toBeVisible();
  });
});
