# ADR-0013: Corpus baked into the api image

|        |            |
| ------ | ---------- |
| Status | Proposed   |
| Date   | 2026-10-06 |

## Context

Team Assistant answers from the repo's own `docs/**/*.md` (B3). Locally the ingestion job reads
`./docs` directly. A deployed api container has no checkout, and the platform gives this use case
no shared file store for a corpus (ADR-0012: only container apps, database, storage, Key Vault
and index are owned here). `POST /v1/admin/ingest` must therefore read the corpus from
somewhere inside the running container.

## Decision

We copy `docs/**/*.md` into the api image at `/app/corpus` at build time, and the ingest
endpoint reads `/app/corpus` when deployed and `./docs` when run locally.

## Consequences

- Good: no extra storage resource, no runtime network dependency, a corpus version always
  matches the code version that was built and tested with it.
- Good: re-indexing is reproducible per image tag.
- Bad: a docs-only change needs an image rebuild and deploy before it can be re-indexed.
- Bad: the image carries all of `docs/` (size grows with the docs; template docs are included
  until the corpus scope is narrowed — ARCHITECTURE.md B8 decision 14).

## Alternatives considered

- **Blob Storage container the ingest job reads** — rejected for now: one more owned
  resource, a role grant request to the cloud team, and a sync step, for a corpus that already
  lives in git.
- **Git clone at ingest time** — rejected: needs credentials and egress from the container.

## References

- Related ADRs: ADR-0009, ADR-0012
- Docs: `docs/architecture/ARCHITECTURE.md` B3, B8 decision 13
