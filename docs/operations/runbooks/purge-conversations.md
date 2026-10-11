# Runbook — Removing conversations from the database

**When to use:** someone asks for their words to be removed from the assistant's chat history, a person
pasted personal data into a chat, or the team decides to clear old conversations. This is the **only**
way to remove a conversation: the api has no delete endpoint and no purge job (decisions 5 and 6 of
[`ARCHITECTURE.md`](../../architecture/ARCHITECTURE.md) B8; [ADR-0014](../../adr/0014-shared-conversations-until-auth.md)).

**Who:** the **database owner** — the person who owns this deployment's Azure SQL server and database (they are
use-case infrastructure, [ADR-0012](../../adr/0012-bicep-shared-platform.md)) — or someone they delegate
the task to for a limited time. Not the api, not an agent.

**Read this first.** Conversations are **shared**: there is no owner column, so every user of the
internal deployment can read every conversation, and there is no way to find "one person's
conversations" except by what they wrote. A conversation's title is the first 200 characters of its
first question, and message text can hold personal data. Treat everything this runbook shows you
as personal data: do not paste results into chat, tickets or this repository. This file is part of the
corpus the assistant answers from, so it can quote it to any user; it holds nothing secret.

> **No agent runs any statement below.** A person does, after reading it, against the real database. The
> statements are reviewed like code, and `tests/test_runbooks.py` pins their table and column names to the
> models and their safety properties (rollback by default, expected counts, a cutoff guard, a snapshot with locks).
> Nothing here needs or contains a secret or a connection string.

## What this removes, and what it does not

| Where the text may be                                           | Removed by this runbook? | What to do                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| --------------------------------------------------------------- | ------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `conversations` and `messages` tables of the Azure SQL database | **Yes**                  | The procedures below.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Azure SQL automated backups                                     | **No**                   | Rows leave the live database at once but stay in the point-in-time-restore (PITR) backups until they age out: the default retention is **7 days** and is configurable from 1 to 35 ([Microsoft Learn](https://learn.microsoft.com/en-us/azure/azure-sql/database/automated-backups-overview?view=azuresql)). **Check what is set on this server**, including any long-term-retention (LTR) policy, which keeps backups for weeks to years: `az sql db str-policy show` and `az sql db ltr-policy show` (run by the database owner). Tell the requester the date after which the text is gone. |
| **A restore brings it back**                                    | —                        | After **any** restore of this database (also the one under "If it goes wrong"), replay every earlier removal from the request log (below): that is why the log keeps ids and cutoffs.                                                                                                                                                                                                                                                                                                                                                                                                         |
| Application Insights / Log Analytics                            | Not applicable           | Application telemetry is **content-free by design** (rule 60: never a question, an answer or a title), so there is normally nothing to remove. If a line did leak, use [`pii-purge.md`](pii-purge.md): it covers **telemetry only**, not the database. This does **not** say anything about SQL auditing or Query Store (next row).                                                                                                                                                                                                                                                           |
| SQL statement text kept by the database or your client          | **No**                   | The phrase you search for (step A1) is part of the batch text. Query Store, SQL auditing (if enabled) and your client's query history may keep it. Ask the database owner whether auditing or Query Store capture batch text and for how long; clear your client's history afterwards.                                                                                                                                                                                                                                                                                                        |
| The search index (Azure AI Search)                              | Not applicable           | The index holds the team's documents (`docs/**/*.md`), never chat text. A document that names someone is changed in git and re-indexed (`POST /v1/admin/ingest`).                                                                                                                                                                                                                                                                                                                                                                                                                             |
| The model service (Foundry) that answered                       | **No**                   | Questions and retrieved passages were sent to it. Its own retention (abuse monitoring) is the platform team's: ask them what applies to this account (a 30-day period is reported for the default setting, **not verified here**). This repository cannot delete it.                                                                                                                                                                                                                                                                                                                          |

## Before you start

1. **Access, least privilege.** The task needs `SELECT` and `DELETE` on the two tables and nothing else.
   The database owner creates a database role for it, adds you for the task, and removes you afterwards (the
   statements are under "Grant and revoke" below; the owner runs them). **Do not use the api's own identity,
   a shared account, or the server's Entra administrator account as a habit**: use your own named identity,
   so the audit trail says who did it, and prefer a time-boxed grant.
2. **Connect as yourself** with Microsoft Entra authentication (Azure Data Studio, SSMS, or `sqlcmd -G`). If your
   address is not allowed through the SQL server's firewall, the database owner opens it for the task (the
   firewall rules are in this project's `infra/`); never open it to everyone.
3. **Confirm the schema** the tables live in. The statements say `dbo`; if this shows another schema, stop and
   change every `dbo.` in the batches **in a reviewed change to this runbook**, not on the fly:

   ```sql
   SELECT s.name AS table_schema, t.name AS table_name
   FROM sys.tables AS t
   JOIN sys.schemas AS s ON s.schema_id = t.schema_id
   WHERE t.name IN (N'conversations', N'messages');
   ```

4. **Pick a quiet time.** The delete holds locks on the rows it removes until it ends: the api can neither add a
   message to them nor change them meanwhile, so an ask on a conversation being removed waits and then **fails**
   (the foreign key refuses an orphan message). It never leaves a row behind. A removal of more than about
   **4,000 messages** in one transaction can block the api's writes to `messages` for the whole time and fill the log:
   tell the team, run it in a window, and know how to stop it (cancel the query: with `XACT_ABORT ON` that rolls
   everything back; then run `SELECT @@TRANCOUNT;` and, if it is not `0`, `ROLLBACK TRANSACTION;`).
5. **Run each batch whole, never a selection, and no `GO` in the middle.** Every batch starts with the values you must
   edit (an id or a date, the expected counts, the commit switch) and **refuses to run until you have**.

The tables are `dbo.conversations` (`id`, `title`, `created_at`, `updated_at`) and `dbo.messages` (`id`, `seq`,
`conversation_id`, `role`, `content`, `citations`, `token_count`, `created_at`). The foreign key from
`messages.conversation_id` to `conversations.id` is `NO ACTION`, so **messages are deleted before their
conversation**. `updated_at` is UTC and moves whenever a message is added.

## How the delete batches are made safe

- **Two runs.** First a **dry run** (a plain `SELECT`: it changes nothing) gives the counts. Then the **delete batch**
  runs once with `@commit = 0` (it deletes inside a transaction, shows the counts it would remove and **rolls
  back**) and, if they equal the counts you wrote in `@expected_…`, once more with `@commit = 1`.
- **It commits only when everything matches:** `@commit = 1` **and** the deleted counts equal the expected ones.
  Anything else rolls back. Any error rolls back (`SET XACT_ABORT ON`).
- **It holds what it removes.** The rows to remove are read with `UPDLOCK, HOLDLOCK` first, so a message cannot be
  added to a conversation between the two deletes (which would otherwise leave a conversation with only its newest
  message), and Procedure B deletes by that **snapshot of ids**, never by the date twice.
- **The placeholders are harmless:** the zero id matches nothing, the sample cutoff is `1900-01-01`, the expected
  counts start at `-1` (the batch stops until you set them), and a cutoff less than 30 days ago stops the batch.

## Procedure A — one conversation

### A1. Find the id

The requester cannot read an id from the chat page (its address has none). Get it from the list the app itself shows
(`GET /api/v1/conversations`: ids and titles, which are shared data) or by searching. The search shows ids and dates,
**not text**. The phrase is a variable, but it is still part of the batch text, which the database and your client may
keep (see "What this does not remove"): prefer the **start of the first question** or a **date range** to a longer
phrase, and never an email address or a number if you can avoid it. `%`, `_` and `[` in the phrase match loosely.

```sql
DECLARE @term nvarchar(200) = N'<the first words of the request>';

SELECT TOP (50) c.id, c.created_at, c.updated_at
FROM dbo.conversations AS c
WHERE c.title LIKE N'%' + @term + N'%'
   OR EXISTS (
        SELECT 1
        FROM dbo.messages AS m
        WHERE m.conversation_id = c.id
          AND m.content LIKE N'%' + @term + N'%'
      )
ORDER BY c.updated_at DESC;
```

It scans the text of every message: fine at the expected low volume; narrow it with `AND c.updated_at >= '<date>'` if
slow. **Confirm each id is the requester's without reading the content aloud or into a ticket**: ask for the date and
the first words, and check them here, as a yes or no:

```sql
DECLARE @id uniqueidentifier = '00000000-0000-0000-0000-000000000000';  -- replace
DECLARE @first_words nvarchar(200) = N'<the first words the requester gave>';

SELECT
    CASE WHEN c.title LIKE @first_words + N'%' THEN 'yes' ELSE 'no' END AS title_starts_with_them,
    c.created_at,
    c.updated_at
FROM dbo.conversations AS c
WHERE c.id = @id;
```

Removing the wrong person's conversation can only be undone by a point-in-time restore: be sure.

### A2. Dry run

```sql
DECLARE @id uniqueidentifier = '00000000-0000-0000-0000-000000000000';  -- replace

SELECT
    (SELECT COUNT(*) FROM dbo.conversations WHERE id = @id)          AS conversations,
    (SELECT COUNT(*) FROM dbo.messages WHERE conversation_id = @id)  AS messages;
```

Expect `conversations = 1` and a message count that fits what you were told. `0` means a wrong id.

### A3. Delete

Edit the first lines: the id, `@expected_messages` (the number from A2). Run once with `@commit = 0`; if the counts it
shows equal what A2 showed, set `@commit = 1` and run it again.

```sql
SET XACT_ABORT ON;
BEGIN TRANSACTION;

DECLARE @id uniqueidentifier = '00000000-0000-0000-0000-000000000000';  -- replace
DECLARE @expected_messages int = -1;  -- the messages count of the dry run
DECLARE @commit bit = 0;              -- 1 only after a run with 0 showed the expected counts

IF @expected_messages < 0 THROW 50001, 'Set @expected_messages from the dry run.', 1;

-- Hold the conversation: no message can be added to it until this transaction ends.
IF NOT EXISTS (SELECT 1 FROM dbo.conversations WITH (UPDLOCK, HOLDLOCK) WHERE id = @id)
    THROW 50002, 'No such conversation: check the id.', 1;

DELETE FROM dbo.messages WHERE conversation_id = @id;
DECLARE @messages_deleted int = @@ROWCOUNT;

DELETE FROM dbo.conversations WHERE id = @id;
DECLARE @conversations_deleted int = @@ROWCOUNT;

SELECT @conversations_deleted AS conversations_deleted, @messages_deleted AS messages_deleted;

IF @commit = 1 AND @conversations_deleted = 1 AND @messages_deleted = @expected_messages
    COMMIT TRANSACTION;
ELSE
    ROLLBACK TRANSACTION;
```

## Procedure B — every conversation not touched since a date

For a retention clean-up ("remove everything last used before 1 January"). The unit is the **conversation**, by
`updated_at` (its last activity): never delete old messages out of a conversation that is still in use.

### B1. Dry run

The cutoff is **UTC** (Dubai is four hours ahead) and an ISO literal, `yyyy-mm-ddThh:mm:ss`: a locale-dependent date
string is a trap. A cutoff less than 30 days ago stops the batch, so a typo in the year cannot delete everything.

```sql
DECLARE @cutoff datetime2(3) = '1900-01-01T00:00:00';  -- UTC, ISO; replace

IF @cutoff > DATEADD(DAY, -30, SYSUTCDATETIME()) THROW 50003, 'The cutoff is less than 30 days ago.', 1;

SELECT
    COUNT(*) AS conversations_total,
    SUM(CASE WHEN updated_at < @cutoff THEN 1 ELSE 0 END) AS conversations_to_delete,
    SUM(CASE WHEN updated_at < @cutoff THEN 0 ELSE 1 END) AS conversations_to_keep,
    MIN(updated_at) AS oldest,
    MAX(CASE WHEN updated_at < @cutoff THEN updated_at END) AS newest_to_delete
FROM dbo.conversations;

SELECT COUNT(*) AS messages_to_delete
FROM dbo.messages AS m
WHERE m.conversation_id IN (SELECT id FROM dbo.conversations WHERE updated_at < @cutoff);
```

If the share to delete is not what was agreed, or `conversations_to_keep` is `0`, **stop**: wrong date or wrong database.

### B2. Delete

Edit the cutoff and both expected counts (from B1). Run once with `@commit = 0`; if the counts equal B1's, set
`@commit = 1` and run it again.

```sql
SET XACT_ABORT ON;
BEGIN TRANSACTION;

DECLARE @cutoff datetime2(3) = '1900-01-01T00:00:00';  -- UTC, ISO; the same cutoff as B1
DECLARE @expected_conversations int = -1;  -- conversations_to_delete of B1
DECLARE @expected_messages int = -1;       -- messages_to_delete of B1
DECLARE @commit bit = 0;                   -- 1 only after a run with 0 showed the expected counts

IF @expected_conversations < 0 OR @expected_messages < 0 THROW 50001, 'Set the expected counts from the dry run.', 1;
IF @cutoff > DATEADD(DAY, -30, SYSUTCDATETIME()) THROW 50003, 'The cutoff is less than 30 days ago.', 1;

-- Snapshot the ids and hold those conversations: no message can be added to them until this ends.
SELECT id INTO #doomed FROM dbo.conversations WITH (UPDLOCK, HOLDLOCK) WHERE updated_at < @cutoff;

DELETE m FROM dbo.messages AS m WHERE m.conversation_id IN (SELECT id FROM #doomed);
DECLARE @messages_deleted int = @@ROWCOUNT;

DELETE FROM dbo.conversations WHERE id IN (SELECT id FROM #doomed);
DECLARE @conversations_deleted int = @@ROWCOUNT;

SELECT @conversations_deleted AS conversations_deleted, @messages_deleted AS messages_deleted;

IF @commit = 1 AND @conversations_deleted = @expected_conversations AND @messages_deleted = @expected_messages
    COMMIT TRANSACTION;
ELSE
    ROLLBACK TRANSACTION;
```

## Grant and revoke (the database owner runs these, not you)

A role with the two permissions the task needs, created once; a person is added for the task and **removed
afterwards**. Run them as the database owner on the database, with the person's own Entra name.

```sql-admin
CREATE ROLE conversation_purger;
GRANT SELECT, DELETE ON OBJECT::dbo.conversations TO conversation_purger;
GRANT SELECT, DELETE ON OBJECT::dbo.messages TO conversation_purger;

CREATE USER [name@example.com] FROM EXTERNAL PROVIDER;
ALTER ROLE conversation_purger ADD MEMBER [name@example.com];

-- When the task is done:
ALTER ROLE conversation_purger DROP MEMBER [name@example.com];
DROP USER [name@example.com];
```

`DELETE` with a `WHERE` needs `SELECT`, so the person can read personal data: that is why the grant is for the task and
ends with it. Note the grant and the revocation in the request log.

## Verify

- [ ] Run the dry run again: **0 conversations, 0 messages** for that id or cutoff.
- [ ] `SELECT @@TRANCOUNT;` in your session is `0` (nothing left open, nothing blocking the api).
- [ ] The api no longer has it: `GET /v1/conversations/<id>` (or `/api/v1/conversations/<id>` through the web app)
      answers `404` with `{"error": "not_found", "trace_id": "…"}`, and it is gone from the chat page's list. (A `404`
      alone does not prove the right database was cleaned: the dry run is the check.)
- [ ] The grant is revoked (the database owner).
- [ ] The requester is told: removed from the live database on `<date>`; gone from backups by `<date + the server's PITR
and any LTR retention>`; and that the model service and any copy they made are outside this runbook.

## Record it (no content)

Keep a note outside this repository (the team's request log, readable only by the people who handle removals): the
request reference, who asked, who ran it, the date, the **ids** (or the cutoff) and the counts, the backup date given to
the requester, and the grant and revocation. **Never** the words, titles or message text. The log is what lets you replay
the removals after a restore.

## Retention decision

There is **no automatic retention for now**: conversations are kept until someone removes them with this runbook
(`ARCHITECTURE.md` B8 decisions 5 and 6, accepted with [ADR-0014](../../adr/0014-shared-conversations-until-auth.md) and
the [threat model](../../security/threat-models/team-assistant.md), I2 and G11). The chat page tells users that chats are
shared and must not hold personal data. **Revisit it when authentication lands** (the Okta module): owners make "remove
my conversations" a feature rather than a procedure, and a retention period can be decided and automated. Until then,
run Procedure B on a schedule the team agrees, or not at all, on purpose.

## If it goes wrong

- Committed the wrong thing: stop and tell the database owner at once. The point-in-time restore of the database (within its
  retention) is the only way back; it creates a **new** database to copy rows from, and every removal made since the
  restore point must then be **replayed** from the request log.
- The batch stops with `50001`, `50002` or `50003`: it did what it is for; fix the value it names and run it again.
- It errors on a foreign key (547): a message was added to a conversation you were removing (it ended before your lock,
  or the lock was not taken). Everything is rolled back; run it again.
- It seems stuck: it is waiting for a lock, or the api is waiting for it. Cancel it, then `SELECT @@TRANCOUNT;` and
  `ROLLBACK TRANSACTION;` if it is not `0`.
- You saw personal data you should not have: that is the nature of this task; do not copy it anywhere, and note in the
  request log that you ran the procedure, not what you saw.

## References

- Related: [`pii-purge.md`](pii-purge.md) (telemetry, not the database) · [ADR-0014](../../adr/0014-shared-conversations-until-auth.md)
  (shared conversations) · [ADR-0012](../../adr/0012-bicep-shared-platform.md) (the database is this project's) ·
  [`ARCHITECTURE.md`](../../architecture/ARCHITECTURE.md) B8 (decisions 5, 6) · [`db-design.md`](../../design/db-design.md)
  (`conversations`, `messages`) · [threat model](../../security/threat-models/team-assistant.md) (I1, I2, AI-8, G11)
- Backups: [Automatic, geo-redundant backups — Azure SQL Database](https://learn.microsoft.com/en-us/azure/azure-sql/database/automated-backups-overview?view=azuresql)
- Roadmap: api 5.8
