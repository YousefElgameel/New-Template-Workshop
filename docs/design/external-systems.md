# External systems

_Every database or API the application reads from (or, exceptionally, writes to) that this
project does **not** own. The api reaches external databases only through the **read-only**
engine (`EXTERNAL_DATABASE_URL`, `app/db/external.py` — non-SELECT statements are refused) and
external APIs only through `traced_client`._

## External databases

**None.** Team Assistant uses only its own Azure SQL database
([`db-design.md`](db-design.md)); `EXTERNAL_DATABASE_URL` stays unset.

## External APIs

**None.** The only outbound dependencies are the shared platform services — Azure AI Foundry
(chat + embeddings) and Azure AI Search — which are the AI runtime's, not external systems
(see `ARCHITECTURE.md` B3).

_To add a system later, restore the template's table layout from git history (`git show 0408cc7:docs/design/external-systems.md`:
owner, engine, environments, access mode, credentials as a Key Vault name, network path, schema
reference, allow-listed tables, PII, SLA) and update `ARCHITECTURE.md` B4/B5._
