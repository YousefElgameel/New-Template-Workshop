# ADR-0015: Shared-secret header guards the ingest endpoint

|        |            |
| ------ | ---------- |
| Status | Proposed   |
| Date   | 2026-10-06 |

## Context

`POST /v1/admin/ingest` rebuilds the Azure AI Search index and spends embedding quota (F3). The
platform has no user auth and no admin role, so the endpoint would be callable by anyone who can
reach the api. The api is only reachable by the BFF on the internal network (rule 50), but a
cheap, explicit guard is still wanted. Adding real auth needs an ADR and a threat model; this
is not that — it is a stop-gap for one operational endpoint.

## Decision

We require an `x-admin-key` header equal to the `INGEST_ADMIN_KEY` environment variable
(compared in constant time; a Key Vault secret when deployed). If the variable is unset the
endpoint returns `401` (fail closed), and the BFF does not expose the route.

## Consequences

- Good: ingest cannot be triggered by an ordinary user or an accident; no new infrastructure.
- Good: fails closed when misconfigured.
- Bad: one shared secret, no per-person audit trail; rotation is manual.
- Bad: an extra secret to provision (a human step) — a Key Vault entry when deployed, a `.env`
  entry locally.
- Bad: replaced by Okta roles when auth lands (this ADR is then superseded).

## Alternatives considered

- **Leave it open** — rejected: any reachable caller can burn embedding quota.
- **CLI only (`python -m app.ai.ingest`), no endpoint** — rejected: F3 asks for an endpoint, and
  a deployed container is awkward to shell into.
- **Expose it through the BFF behind a UI button** — rejected: needs real auth first.

## References

- Related ADRs: ADR-0009
- Docs: `.claude/rules/50-security.md`, `docs/architecture/ARCHITECTURE.md` B8 decision 9
