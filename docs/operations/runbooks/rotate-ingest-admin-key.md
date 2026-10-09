# Runbook — Rotating the ingest admin key

**When to use:** on a schedule, when a person who knew `INGEST_ADMIN_KEY` leaves, or **at once** if the key may
have leaked (a log, a chat, a screenshot, a shell history). The key guards `POST /v1/admin/ingest`
([ADR-0015](../../adr/0015-admin-key-for-ingest.md)); with it anyone who can reach the api can start a re-index
and spend embedding quota. It is one shared secret, so rotating it changes it for everyone who uses it.

> The api reads the key from the environment **once per request** but the container's environment is fixed at
> start, so a new value needs a **restart** (a new revision) to take effect. Until then the old key still works.
>
> **Deployed:** the key reaches the api only once the use-case infrastructure wires it (a Key Vault secret and a
> Container App environment entry — not in `infra/` yet; see the api 3.3 roadmap item). Until then only a local `.env`
> carries a key, and a deployed api answers `401` to everyone.

## Rotate

1. **Generate** a new value of at least 32 characters from a CSPRNG, on your own machine, without echoing it
   into a shared terminal or a chat:
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`
   (a value under 32 characters is treated as **not set**: the endpoint then answers `401` to everyone).
2. **Store** it where the api reads it:
   - deployed: the use case's Key Vault secret that holds it (a new version), referenced by the api Container
     App — a person with Key Vault access does this, never the repository;
   - local: your own `apps/api/.env` (gitignored).
3. **Restart** the api so it picks the value up (deployed: restart the revision, or create a new one — with several
   revisions active, deactivate the old one, or the old key keeps working there; local: restart `uvicorn`). A
   restart also stops an ingestion run in progress; there is no cancel route.
4. **Tell** the people who start ingestion the new value through your team's secret channel, not by chat or email.

## Check

Replace `$OLD` and `$NEW` with the two values; `$API` is the api's base URL (internal).

| Check                  | Command                                                                                      | Expect                                                                                                                                 |
| ---------------------- | -------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| the old key is refused | `curl -s -o /dev/null -w "%{http_code}" -X POST -H "x-admin-key: $OLD" $API/v1/admin/ingest` | `401`                                                                                                                                  |
| no key is refused      | `curl -s -o /dev/null -w "%{http_code}" -X POST $API/v1/admin/ingest`                        | `401`                                                                                                                                  |
| the new key works      | `curl -s -X POST -H "x-admin-key: $NEW" $API/v1/admin/ingest`                                | `202` `{"status":"started"}` (starts a real run — do this once, when a re-index is wanted anyway; or skip it and trust the two `401`s) |

A `401` for the **new** key means the api did not restart, the value is under 32 characters, or the Key Vault
reference points at the old version. Log Analytics shows `admin key is not usable` with
`reason: too_short` or `reason: not_set` (once per process) and `admin call refused` with `reason: wrong_key`
or `no_key` — never the key or its length:

```kusto
AppTraces
| where TimeGenerated > ago(1h) and AppRoleName == "team-assistant-api"
| where Message in ("admin key is not usable, so admin routes refuse every call", "admin call refused")
| project TimeGenerated, Message, Properties
```

## If the key leaked

Rotate first (above), then check whether it was used: look for `ingest finished` / `ingest failed` lines and
`ingest.completed` events you did not start. A leaked key in an exported log line is also a PII/secret purge —
see [pii-purge](pii-purge.md); add the field to the redaction list if it slipped past
(`x_admin_key`, `admin_key` and `ingest_admin_key` already are).

## Notes

- There is one key, no per-person audit trail, and no expiry; this is the stop-gap of ADR-0015 until auth (Okta)
  replaces it.
- The key is never in a Bicep parameter file, a deployment output or the repository (rule 50).
