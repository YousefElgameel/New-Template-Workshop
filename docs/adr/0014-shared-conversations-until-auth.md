# ADR-0014: Conversations are shared until auth lands

|        |            |
| ------ | ---------- |
| Status | Proposed   |
| Date   | 2026-10-06 |

## Context

Team Assistant stores conversations and messages in Azure SQL (F2). The platform has no user
authentication yet — projects run internal-only and the planned module is Okta (rule 50; a later
roadmap phase). Without an identity there is nothing to attach a conversation to, so any
ownership column would be unenforceable. Conversation content can contain personal data.

## Decision

We store conversations with no owner and let every user of the internal deployment read every
conversation, until Okta lands and ownership is added in a new migration.

## Consequences

- Good: no fake or client-supplied user id that would look like access control but isn't.
- Good: the simplest schema; adding `owner` later is an additive migration.
- Bad: any user can read any other user's questions; the deployment must stay internal-only.
- Bad: the list endpoint (`GET /v1/conversations`) exposes the ids and titles of the 100 most recent
  conversations, and a title is the first 200 characters of someone's first question. A UUID is therefore
  not a secret: once the list exists, unguessable ids add no protection, and the open endpoint returns
  every message and citation.
- Bad: creation is unauthenticated and unlimited — any caller can add rows. Acceptable only while the
  deployment is reachable from the internal network alone; add a rate limit or ingress throttle with auth.
- Bad: existing rows will have no owner when auth lands — a backfill or "legacy/shared" rule is
  needed then.
- Bad: message content is personal-data-capable and kept indefinitely (no purge job); a threat
  model must cover it.

## Alternatives considered

- **Owner taken from a client-supplied header or cookie** — rejected: trivially forged, gives
  false assurance.
- **Single shared conversation, no history list** — rejected: drops F2.

## References

- Related ADRs: ADR-0008, ADR-0012
- Docs: `.claude/rules/50-security.md`, `docs/architecture/ARCHITECTURE.md` B7, B8
