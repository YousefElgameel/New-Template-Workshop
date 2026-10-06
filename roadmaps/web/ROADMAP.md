# Roadmap — web (Team Assistant)

_Frontend: pages, BFF routes, components, mocks. Schema: [`roadmaps/README.md`](../README.md).
Planned 2026-10-06 from `docs/architecture/ARCHITECTURE.md` Part B (B2–B8), the page spec
`docs/design/wireframes/chat.md` (+ `chat.html`) and the api roadmap
([`roadmaps/api/ROADMAP.md`](../api/ROADMAP.md)). Decisions referenced as "decision N" are in B8._

_The browser only talks to this Next server (the BFF rule); every upstream call is `/v1` through
`fetchUpstream` / `streamUpstream`. Until an api item is `done`, the matching BFF route serves a
`MOCK_UPSTREAM=true` fixture, so pages can be built and tested ahead of the api. The template's
demo (`/api/v1/assistant/*`, `src/mocks/assistant.ts`, the example `/chat`) is replaced by the
conversation surface (decision 18) — the removal is item 4.1._

## Phase 1 — Foundation (contract + mocks)

### 1.1 — Conversation types and mock fixtures

- **status:** done
- **depends_on:** []
- **layers:** [bff]
- **acceptance:**
  - typed shapes for the contract in B4a: conversation, conversation list item, message (with `citations: [{ title, path }]`), the JSON ask response, and the stream frames `sources` / `token` / `done` / `error` (`{ error_kind, error, trace_id }`) — hand-declared for now and marked to be swapped for the generated `api-types.ts` in 4.2
  - fixtures (in `src/mocks/`) for: an empty list, a list of three conversations, one conversation with messages that carry citations, a created conversation, a JSON answer, a streamed answer, and a streamed failure (`error` frame)
  - the mock helpers can pace SSE frames as the existing `mockSse` does
- **how_to_test:**
  - `pnpm -C apps/web test` (or `just test`) — expect **16 test files, 149 tests** passed in web (15 files / 136 tests before, plus `src/mocks/conversations.test.ts`: 13); api unchanged at 346
  - `pnpm -C apps/web typecheck` — clean. The type-check is half of the test: the fixtures are typed from the generated contract, so a backend change that breaks them fails here
  - prove it: delete `truncated: false,` from one entry in `conversationDetails` (`src/mocks/conversations.ts`) → `tsc` reports a TS2418 error; put it back. And in the same file make a detail's `updated_at` earlier than its last message → 2 tests fail. (I did both and restored the file.)
  - read the fixtures: `src/mocks/conversations.ts` — an empty list, three conversations newest first, a detail for each (long, short, empty), a created conversation, a JSON answer, a streamed answer and a streamed failure
  - nothing uses them at runtime yet; `pnpm -C apps/web build` still compiles
- **needs_human:**
  - none for this item. Reply **done** to close it.
- **notes:**
  - 2026-10-06 — planned.
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — implemented; awaiting test by Yousef Wahid. Files: `src/lib/conversation-types.ts`, `src/mocks/conversations.ts` and `conversations.test.ts` (new), `apps/web/CLAUDE.md`, `src/mocks/README.md`, `CHANGELOG.md` + `package.json` (0.1.1). Review: `code-reviewer` (no security review: no route, env or boundary change), no blockers; its findings were fixed.
  - 2026-10-06 — DEVIATION from the acceptance text, on purpose: it says the shapes are "hand-declared for now". The create/list/open types are instead **derived from the generated `api-types.ts`** (api 2.1 is done), as rule 30 requires ("type from openapi, never by hand"), so a backend change breaks the type-check. Only the ask response and the stream frames — which the api does not serve yet — are hand-declared (`TODO(contract)`, swapped in 4.2).
  - 2026-10-06 — fixed after review: `updated_at` now equals the last message's `created_at` (the api bumps it on every append; my first fixtures had it earlier); citations are `docs/…` paths (the assistant indexes only `docs/**/*.md`; mine were `README.md`/`.claude/…`, which the api could never return); the failure frame's `count` is derived from the citations; the test no longer pins `truncated` to false. Both new rules are covered by tests that fail when broken.
  - 2026-10-06 — open questions for the api roadmap, not changed here: `SourcesFrame.count` is carried over from the template's frame but api 4.3 does not list it; `ErrorFrame.error_kind` is a plain `string` until the api fixes its kinds (`apps/api/app/ai/telemetry.py`). No `truncated: true` fixture was added (not required); one is cheap if the "older messages omitted" UI needs it.
  - 2026-10-06 — version 0.1.1 (PATCH: nothing changes at runtime).
  - 2026-10-06 — confirmed by Yousef Wahid; closed.

### 1.2 — BFF conversation routes (list, create, open)

- **status:** done
- **depends_on:** [1.1, api:2.1]
- **layers:** [bff]
- **acceptance:**
  - `GET /api/v1/conversations` → api `GET /v1/conversations` (optional `limit`, passed through only if 1–100); `POST /api/v1/conversations` → api `POST /v1/conversations` (optional title ≤ 200 characters); `GET /api/v1/conversations/[id]` → api `GET /v1/conversations/{id}`
  - `[id]` must be a UUID, otherwise the BFF answers `400` itself and makes no upstream call (the id is placed into the upstream URL — rule 50, no unvalidated input in outbound URLs)
  - api `404` / `422` are passed through with their body; an upstream failure is a BFF `502` with `target: 'api'` and the `trace_id`; every response echoes `x-trace-id`
  - routes are wrapped in `withBff` (dynamic segment passed as `routeClass`), call `fetchUpstream`, and log route names and statuses only — never titles or message text
  - with `MOCK_UPSTREAM=true` each route serves the fixtures from 1.1; works against the fixtures until api:2.1 lands
- **how_to_test:**
  - `pnpm -C apps/web test` (or `just test`) — expect **18 test files, 250 tests** passed (136 before web 1.1; +13 fixtures, +101 for this item — the new ones are `src/app/api/v1/conversations/routes.test.ts` and `src/lib/conversations-bff.test.ts`, plus one in `mocks.test.ts`); api unchanged at 346
  - `pnpm -C apps/web typecheck` and `pnpm -C apps/web build` — clean; the build lists `ƒ /api/v1/conversations` and `ƒ /api/v1/conversations/[id]`
  - **against the mocks** (no api needed): put `MOCK_UPSTREAM=true` in `apps/web/.env.local`, run `pnpm -C apps/web dev`, then in PowerShell:
    ```
    Invoke-RestMethod http://localhost:3000/api/v1/conversations                       # the three fixture conversations
    Invoke-RestMethod "http://localhost:3000/api/v1/conversations?limit=2"             # two items, count 2
    Invoke-RestMethod http://localhost:3000/api/v1/conversations/5b1f3a52-8c1e-4d3a-9d0b-1a2b3c4d5e01   # the thread with citations
    Invoke-RestMethod -Method Post http://localhost:3000/api/v1/conversations -ContentType application/json -Body '{"title":"hello"}'   # 201
    ```
  - **against the real api** (optional, writes a row to the dev database): remove `MOCK_UPSTREAM`, start the api (`uv run --directory apps/api uvicorn app.main:app --port 8000`) and repeat; the open call needs an id returned by the create call
  - the rejections, each with no upstream call: `?limit=0` → 400; `…/conversations/not-a-uuid` → 400; a body over 4 KB → 413; `-ContentType text/plain` with a body → 415; `-Body '{"x":1}'` → 400
  - prove the tests bite: in `src/lib/conversations-bff.ts` delete the `isUuid` check's effect, the declared-length shortcut, `redirect: 'error'`, or the error-shape guard — the matching tests fail (I did each and restored it)
- **needs_human:**
  - optional: the live checks above
  - decide whether to schedule web 1.3 (below) before the pages ship
  - reply **done** to close the item, or tell me what to change
- **notes:**
  - 2026-10-06 — planned.
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — implemented; awaiting test by Yousef Wahid. Files: `src/app/api/v1/conversations/route.ts` and `[id]/route.ts`, `src/lib/conversations-bff.ts`, the two test files, `src/lib/mocks.ts` (+ test: `APP_ENV` is trimmed), `apps/web/CLAUDE.md`, `docs/reference/http-api.md`, `CHANGELOG.md` + `package.json` (0.2.0). Reviews: `security-reviewer` and `code-reviewer` — no CRITICAL/HIGH; the findings below were verified before acting.
  - 2026-10-06 — DECISIONS: the `limit` query is validated by the BFF (`400` if not 1–100) rather than silently dropped; a body over **4 KB** is `413` (the cap you left to my default); BFF-side validation answers `400 invalid_request` like the existing routes, and the api's `422` is documented as "means the two checks drifted".
  - 2026-10-06 — fixed after review: a client that disconnects mid-upload gave a framework 500 (now `400`); a slow trickle could hold a handler open (5 s read timeout); a body without `Content-Type: application/json` is now `415` (a cross-site "simple" request could otherwise create conversations); redirects are not followed; a reply over 8 MiB, or an error body that is not the api's `{ error, trace_id }`, becomes a `502` instead of reaching the browser; the "declared length" test never set a `content-length` (a string-bodied `Request` has none), so that branch was untested — it is now, with a body that must not be read; the mock list ignored `limit`; `APP_ENV` is trimmed in the mock guard. Each new safeguard has a test that fails when the safeguard is removed.
  - 2026-10-06 — NOT fixed here, recorded as web 1.3: the web's own request and outbound spans carry the concrete conversation id (`http.target`, `url.full`) — logs and metrics are clean. The api already scrubs it (`scrub_ids_server_hook`).
  - 2026-10-06 — for the api roadmap (not edited from here): a lone UTF-16 surrogate in a title passes the BFF and `JSON.parse`, then fails `.encode("utf-16-le")` in the api — likely a 500; worth a `422` there.
  - 2026-10-06 — version 0.2.0 (MINOR: new BFF routes, backward compatible).
  - 2026-10-06 — confirmed by Yousef Wahid; closed.

### 1.3 — Keep resource ids off the web's telemetry spans

- **status:** todo
- **depends_on:** [1.2]
- **layers:** [bff]
- **acceptance:**
  - the web's inbound request span and its outbound (undici) span to the api no longer carry a concrete conversation id: any UUID in `http.target`, `http.url`, `url.path` and `url.full` is rewritten to `{id}`, as the api does with `scrub_ids_server_hook`
  - a test shows the id on a span without the scrub (a control) and its absence with it, using an in-memory exporter; the hook is wired where the telemetry is initialised and is fail-safe (it can never break a request)
  - `.claude/rules/60-observability.md` and `apps/web/CLAUDE.md` say so, and the "known gap" notes are removed
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - none
- **notes:**
  - 2026-10-06 — added from the web 1.2 security review (MEDIUM). Conversations are shared and personal-data-capable (ADR-0014); an id is not a secret once the list endpoint exists, but it should not be exported to Application Insights either. Touches `src/lib/observability.ts` (the distro's http and undici instrumentation options), which is why it is its own item. Do it before the pages ship.

## Phase 2 — Conversation history (F2)

### 2.1 — ConversationList component

- **status:** done
- **depends_on:** [1.1]
- **layers:** [ui]
- **acceptance:**
  - renders rows (title + a relative time such as "today 10:12" / "yesterday"), a selected state, a **+ New chat** button, and the empty state "No chats yet"
  - selecting a row and pressing **+ New chat** are callbacks (the component does not fetch)
  - keyboard accessible: rows are reachable and activatable with the keyboard, the selected row is announced
  - long titles are truncated without breaking the layout
  - styled with Tailwind v4 like the other chat components
- **how_to_test:**
  - `pnpm -C apps/web test` (or `just test`) — expect **20 test files, 283 tests** passed (250 before; +33 in `src/components/chat/ConversationList.test.tsx` and `src/lib/relative-time.test.ts`); api unchanged at 346
  - `pnpm -C apps/web typecheck` and `pnpm -C apps/web build` — clean
  - the component is not on a page yet (that is 2.2), so there is nothing to click. To look at it now, temporarily render `<ConversationList conversations={conversationListFixture.items} selectedId={conversationIds.errors} onSelect={console.log} onNewChat={() => undefined} />` (fixtures in `src/mocks/conversations.ts`) in a scratch page and check: three rows with a time under each title, the second row highlighted with a blue left border, Tab reaching **+ New chat** then each row, Enter/Space selecting, a long title cut with an ellipsis, and the focus ring fully visible. Remove the scratch page afterwards
  - prove the tests bite: in `ConversationList.tsx` remove `aria-current`, the `truncate` class, the `p-1` padding or the selected row's `hover:bg-blue-100`; in `relative-time.ts` change `days === 1` or `Sep` → `Sept` — the matching test fails each time (I did all of these and restored them)
- **needs_human:**
  - optional: look at it in a browser once 2.2 puts it on `/chat` (contrast, spacing and the focus ring cannot be checked in jsdom)
  - reply **done** to close the item, or tell me what to change
- **notes:**
  - 2026-10-06 — planned.
  - 2026-10-06 — started by Claude. Picked by the skill's rule: 1.3 comes first in phase order but depends on 1.2, which is still `awaiting-test`; 2.1 needs only 1.1.
  - 2026-10-06 — implemented; awaiting test by Yousef Wahid. Files: `src/components/chat/ConversationList.tsx` + `.test.tsx`, `src/lib/relative-time.ts` + `.test.ts` (new), `apps/web/CLAUDE.md`, `CHANGELOG.md` + `package.json` (0.2.1). Review: `code-reviewer` (no security review: no route, env or boundary change), no blockers; its findings were verified, then fixed.
  - 2026-10-06 — fixed after review: the focus ring was clipped by the scrolling list (`overflow-y-auto` clips both axes → `p-1`); the title was announced twice (a `title` on the button becomes its accessible description → the tooltip now sits on the text); hovering the selected row dropped its tint; time text and the empty state were under 4.5:1 contrast; en-GB writes September as "Sept" (verified) → fixed three-letter month names; the three overlapping "Conversations" labels are now one; the time is a `<time dateTime>`; formatters are cached; the "defaults now" test uses fake timers instead of a loose regex.
  - 2026-10-06 — what jsdom cannot prove (no layout, no screen reader): truncation, the focus ring and the contrast are pinned by class assertions only. Real checks are the browser look above and the manual Playwright smoke (web 4.3). The accessible name in a real browser separates title and time; jsdom concatenates them.
  - 2026-10-06 — version 0.2.1 (PATCH: nothing uses the component yet, so nothing changes at runtime).
  - 2026-10-06 — confirmed by Yousef Wahid; closed.

### 2.2 — `/chat` page: list, new chat, reopen

- **status:** done
- **depends_on:** [1.2, 2.1]
- **layers:** [ui]
- **acceptance:**
  - `/chat` is a two-column page (conversations left, thread right, input at the bottom) as in `wireframes/chat.md`; below 768 px the list collapses into a drawer opened by a button
  - on load it fetches `GET /api/v1/conversations` and shows the list, or "No chats yet"
  - **+ New chat** calls `POST /api/v1/conversations`, selects the new conversation and focuses the input
  - clicking a row calls `GET /api/v1/conversations/{id}` and shows its messages (with their citations); the input then continues that conversation
  - list and thread each show a loading state and an error line with the `trace_id` when the BFF call fails
  - the home page links to `/chat`
  - works against `MOCK_UPSTREAM=true` fixtures until api:2.1 lands
- **how_to_test:**
  - `cd apps/web && pnpm test` → all green (322 tests; `ChatWorkspace.test.tsx`, `conversations-client.test.ts` are the new ones)
  - `cd apps/web && MOCK_UPSTREAM=true pnpm dev`, open http://localhost:3000 → click **Open the chat** → `/chat`
  - the list shows 3 conversations (newest first, "today 10:12" style times); click **Error contract** → two messages, the answer with `[1] docs/reference/http-api.md`
  - **+ New chat** → "New conversation" appears at the top, selected, the cursor is in the input; typing and Enter keeps the text and says sending is not available yet (3.3)
  - narrow the window below 768 px → the list is hidden; **Show conversations** opens it as a drawer, choosing a row closes it
  - errors: set `MOCK_UPSTREAM=false` with no api running, reload `/chat` → red line "Could not load conversations (upstream_unreachable). Trace ID: …" and **Retry**; the same id is in the web's log line
- **needs_human:**
  - compare the page with `docs/design/wireframes/chat.html` in a browser (desktop and a narrow window) and say what to adjust (the page height is `100dvh - 7rem`, tuned by eye; the sidebar is a plain overlay, not animated)
- **notes:**
  - 2026-10-06 — planned. Sending a message is added in 3.3.
  - 2026-10-06 — from the 2.1 review, constraints on this page: (1) fetch the list **after mount** — `ConversationList` shows times in the browser's time zone, so rows must not be in server-rendered markup (a server/browser text mismatch otherwise); (2) the page must bound the sidebar's **width** (truncation needs it) and **height** (the list scrolls inside a parent with a fixed height, `min-h-0`); (3) the page needs an `h1` — the list brings an `h2`; (4) "today/yesterday" does not roll over at midnight without a re-render.
  - 2026-10-06 — started by Claude.
  - 2026-10-06 — implemented by Claude. Files: `ChatWorkspace` (+test), `conversations-client` (+test), `chat/page.tsx`, home link, `MessageInput` (`textareaRef`, `keepText`), `ConversationList` (`showEmpty`), CHANGELOG/CLAUDE.md, web 0.3.0. Reviews: code-reviewer (7 findings; 1–6 fixed with tests and mutation checks: empty state while loading, typed text kept, focus after choosing a row, wrong-shape 200 replies, new-chat vs open ordering, stale create error). Not needed: security review (no new route, env or outbound call).
  - 2026-10-06 — deferred: "today/yesterday" does not roll over at midnight without a re-render (from the 2.1 review); the page has no timer. Drawer has no Escape-to-close or focus trap. A first-paint server-render test for the list is not added (the list is empty until mount by construction).
  - 2026-10-06 — confirmed by Yousef Wahid.

## Phase 3 — Chat with citations (F1)

### 3.1 — BFF ask routes (JSON and streaming)

- **status:** todo
- **depends_on:** [1.1, api:4.3]
- **layers:** [bff]
- **acceptance:**
  - `POST /api/v1/conversations/[id]/ask` → api `POST /v1/conversations/{id}/ask`, and `POST /api/v1/conversations/[id]/ask/stream` → api `…/ask/stream` (SSE passed through unchanged with `streamUpstream` / `proxyStream`); body `{ "question": string }`
  - `[id]` must be a UUID (else `400`, no upstream call); an empty question → `400`; the BFF does not rewrite api `404`, `413 payload_too_large` or `503 ai_unavailable` — they reach the browser with their body and `trace_id`
  - neither the 10 s default upstream timeout nor a proxy cut the answer short: the hop timeout is set explicitly above the api's 60 s model timeout plus retries (api roadmap 4.2) and documented
  - `x-trace-id` is on the response headers (and in the `error` frame); logs carry route and status only — never the question or answer
  - `/v1/admin/ingest` is **not** exposed (api:3.3)
  - with `MOCK_UPSTREAM=true` both routes serve the 1.1 fixtures, including the failure stream; works against the fixtures until api:4.3 lands
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - none
- **notes:**
  - 2026-10-06 — planned. Replaces the template's `/api/v1/assistant/ask[/stream]` (removed in 4.1).

### 3.2 — Streaming client and Sources block

- **status:** todo
- **depends_on:** [1.1]
- **layers:** [ui]
- **acceptance:**
  - the browser streaming client handles the new frames: `sources` is `[{ title, path }]`, `done` carries `message_id` and token counts, `error` carries `error_kind`, `error` and `trace_id`; a malformed frame is skipped, never fatal; abort and network failures still call `onError` with a bounded kind
  - the chat message type holds citations as `{ title, path }` objects and the error with its `trace_id`
  - a `Sources` block renders under an assistant message: each citation shows the title, with the path beside it; no block when there are none
  - the existing chat component tests are updated to the new shapes and still pass
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - none
- **notes:**
  - 2026-10-06 — planned. The frame shapes come from api roadmap 4.3; adjust here if that item changes them.

### 3.3 — Send a question into the open conversation

- **status:** todo
- **depends_on:** [2.2, 3.1, 3.2]
- **layers:** [ui]
- **acceptance:**
  - **Send** posts the question to `/api/v1/conversations/{id}/ask/stream` for the selected conversation; the user message appears at once, **Send** is disabled while streaming, tokens appear as they arrive, and the Sources block appears as soon as the `sources` frame arrives
  - when `done` arrives the assistant message is final and the conversation moves to the top of the list (its title updates if it was "New conversation")
  - on an `error` frame or a failed request, a red line under the last user message shows the error code and the `trace_id`, and the input is enabled again; the question can be retried
  - the input shows a counter ("0 / 4,000") and refuses to send an empty question or one over 4,000 characters
  - sending with no conversation selected creates one first
  - works against `MOCK_UPSTREAM=true` fixtures until api:4.3 lands
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - try the full flow in a browser against the mocks (new chat, ask, citations, a forced failure) and confirm the behaviour matches the wireframe states
- **notes:**
  - 2026-10-06 — planned.

## Phase 4 — Cleanup and go-live

### 4.1 — Remove the template demo

- **status:** todo
- **depends_on:** [3.3]
- **layers:** [bff, ui]
- **acceptance:**
  - the template's demo is gone: `src/app/api/v1/assistant/**`, `src/mocks/assistant.ts`, the "(example)" copy and `MOCK_UPSTREAM` hint on the old `/chat` page, and any tests of them; nothing else imports them
  - `pnpm -C apps/web typecheck`, tests and a production build pass; `apps/web/CLAUDE.md` and the docs describe the conversation routes instead
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - confirm the file deletions before they happen (deleting files is always a stop)
- **notes:**
  - 2026-10-06 — planned. Decision 18.

### 4.2 — Switch to the generated types and turn the mocks off

- **status:** todo
- **depends_on:** [4.1, api:2.1, api:4.3]
- **layers:** [bff]
- **acceptance:**
  - the hand-declared types from 1.1 are replaced by `src/lib/api-types.ts` regenerated from the api's `docs/reference/openapi.json` (`make openapi`); fixtures are typed against it and the type-check fails if the contract drifts
  - the stream frame shapes are documented in `docs/reference/http-api.md` and the client matches them
  - `MOCK_UPSTREAM` stays available for offline work but is off by default in `.env.example`
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - run web and api together against dev (`just dev`) with `MOCK_UPSTREAM` unset, create a conversation, ask a question and confirm an answer streams back with citations and one `trace_id` appears in both services' logs
- **notes:**
  - 2026-10-06 — planned. Needs api migration applied (api:1.2) and the corpus ingested (api:3.3) for a real answer.

### 4.3 — Playwright smoke for `/chat`

- **status:** todo
- **depends_on:** [4.2]
- **layers:** [ui]
- **acceptance:**
  - an `e2e/chat.spec.ts` smoke test: open `/chat`, start a new chat, ask a question, see an answer and at least one citation, reopen the conversation from the list
  - it is manual/local like the existing smoke test (needs web and api up) and is documented where `test:e2e` is
- **how_to_test:**
  - _(filled in by `/implement-next`)_
- **needs_human:**
  - run it locally against dev and report the result (Playwright is not part of `just test`)
- **notes:**
  - 2026-10-06 — planned.
