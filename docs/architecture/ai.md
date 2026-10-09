# AI runtime

`apps/api/app/ai/` is the **AI runtime slot** of the baseline
([ADR-0009](../adr/0009-ai-runtime-langchain-langgraph-foundry.md)): LangChain + LangGraph on
Azure AI Foundry model deployments, Azure AI Search for retrieval, and a read-only tool over an
external database. It ships **no use case** — a compiled `retrieve → answer` graph a project
copies, a tool registry, prompt files, telemetry, streaming and an ingestion job — so the first
AI feature starts from something tested instead of a blank file. The enforced conventions are
[`.claude/rules/70-ai.md`](../../.claude/rules/70-ai.md).

---

## Shape

```mermaid
flowchart LR
    route["/v1 route (project)<br/>build_graph() · ask() / stream_answer()"]
    graph["app/ai/graph.py<br/>StateGraph: retrieve → answer (→ tools loop)"]
    ret["app/ai/tools/retrieve.py<br/>AzureSearchRetriever | NullRetriever"]
    client["app/ai/client.py<br/>get_chat_model() · get_embeddings()  ← mock seam"]
    tel["app/ai/telemetry.py<br/>model_call_span · record_retrieval"]
    tools["app/ai/tools/query_external_db.py<br/>allow-listed, parameterised, read-only"]
    prompts["app/ai/prompts/*.md<br/>load_prompt()"]
    ingest["app/ai/ingest.py<br/>chunk → embed → upload · owns the index"]

    foundry["Azure AI Foundry<br/>chat + embedding deployments"]
    search[("Azure AI Search<br/>hybrid index")]
    ext[("External Azure SQL<br/>read-only engine")]
    azm["Azure Monitor"]

    route --> graph
    graph --> ret --> search
    ret --> client
    graph --> client --> foundry
    graph --> prompts
    graph -.-> tools --> ext
    graph --> tel -.-> azm
    ingest --> client
    ingest --> search
```

---

## The pieces

**`config.py`.** `get_ai_settings()` snapshots the `AZURE_AI_*` / `AZURE_SEARCH_*` / `AI_*`
variables (full list in the [environment reference](../reference/environment-variables.md)).
Everything is optional: with nothing set the service boots and only a feature that actually
calls the AI layer raises `AINotConfigured`. A mistyped `AZURE_AI_AUTH_MODE` raises — there is
no silent fallback to a key.

**`client.py` — the mock seam.** The only place a model or embedding client is built.
`build_chat_model()` / `build_embeddings()` construct `AzureChatOpenAI` /
`AzureOpenAIEmbeddings` with the deployment, API version, timeout, retries and — deployed —
a bearer-token provider from `DefaultAzureCredential` for the Cognitive Services scope (the
user-assigned identity is selected through `AZURE_CLIENT_ID`, which the use-case Bicep sets; the
cloud team grants it _Cognitive Services OpenAI User_ on the shared Foundry via `infra/grant-request.md`). `api_key` mode exists for local
development only. Nothing connects at construction; `stream_usage=True` makes Azure OpenAI
report token usage on streamed answers too.

**`graph.py`.** `build_graph(chat_model=None, retriever=None, tools=None, top_k=5)` compiles a
`StateGraph` over a typed state (`messages` with the `add_messages` reducer, `context`):

1. `retrieve` — embeds the latest user turn and queries the retriever (deterministically, every
   turn — grounding never depends on the model choosing a tool); records retrieval telemetry.
2. `answer` — renders `prompts/system.md` with the numbered context, calls the model inside
   `model_call_span()`, appends the `AIMessage`. `format_context` writes the numbers and the
   `[n] (source)` headers itself: a passage's `source` is flattened to one line without brackets or
   parentheses (200 characters at most), and any line of a passage that looks like a header (`[2] (…)`,
   also behind a quote, list or heading mark, a zero-width or bidirectional character, in fullwidth
   form, or as a bare `[2]` line) or like the prompt's `Context:` marker is escaped with a backslash,
   so a retrieved document cannot pose as another passage. Everything else in a passage is left as
   it is. What this does not stop: homoglyph letters, role-like lines (`System:`) and instructions in
   running text — the prompt's "context is data" rule and the live eval (api 5.3) cover those.
3. optional `tools` — when tools are passed, the model is bound to them and a `ToolNode` loop
   (`answer → tools → answer`) runs until it stops calling tools.

`ask(graph, question, history=…)` is the non-streaming entry point and returns the answer text,
the unique sources (every retrieved document), the context, the token usage, and — from
`citations.py` — `cited` (the retrieved passages whose valid `[n]` the answer cites, one per
document) and `invalid_citations` (a count of `[n]` that point to no passage; logged as a count,
never as text). Stored or returned citations are built from `cited` with `citation_of` (`{title,
path}` from the passage), never from anything the model wrote; the answer text is not rewritten.
Markers inside code spans are not citations. A marker is one to three ASCII digits, so `invalid_citations` is a lower bound; the scan is linear
and reads at most 200,000 characters.

**The ask route** (`app/routers/ask.py`, `POST /v1/conversations/{id}/ask`) is the first use of the graph: it checks the
question and the conversation, takes one of `AI_MAX_CONCURRENT_ASKS` slots (`limits.py`, else `429 busy`), reads the last
10 messages as history (`history.py`: `user`/`assistant` only), stores and commits the question, runs `ask()`, and stores
the answer (cut at 4,000 characters) with the passages it cites (`citations.py`). Any failure after the question is
stored is `503 ai_unavailable` and stores no answer. Question and answer text are cleaned before they are
stored (NUL and lone surrogates dropped, other control characters except tab/newline/CR become spaces). The model call is bounded by `AI_MAX_OUTPUT_TOKENS` and
`AI_REQUEST_TIMEOUT_SECONDS`/`AI_MAX_RETRIES`, each retrieval step by `AI_RETRIEVAL_TIMEOUT_SECONDS`. Details and the
error table: [`docs/reference/http-api.md`](../reference/http-api.md).

**The ask stream** (`POST /v1/conversations/{id}/ask/stream`, same module) is the same flow with the answer sent as it is
written: validation, 404 and `429 busy` are ordinary JSON errors before the first byte; then the slot is held by the
response itself (`SseResponse` releases it when the stream ends or the client leaves) and the frames are `sources`,
`token`\*, `done` or `error`. The whole generation has one deadline and a client that stops reading is dropped after
30 s, so a slow provider or a stalled reader cannot hold a slot for ever. The assistant message is stored before `done`; a failure after the first byte is an
`error` frame, never an exception (a raised database error would put its text on the server span).

**`streaming.py`.** `stream_answer()` drives the same graph with LangGraph's `updates` +
`messages` stream modes and yields Server-Sent Events: `sources` (after retrieval), `token`
(per model chunk), `done` (sources + usage) or `error` (a bounded `error_kind`, never the
exception message). `sse_response()` wraps it in a `StreamingResponse` with proxy buffering
disabled. The BFF forwards it with the streaming pass-through helper, not `fetchUpstream`.

**`tools/`.** An explicit registry (`register_tool` / `get_tools`). The built-in
`query_external_db` tool runs **allow-listed** queries from `tools/queries.py` — fixed
parameterised `SELECT`s with typed parameters the model fills in — through the read-only
external engine ([data.md](data.md)). Three layers keep it read-only: the allow-list (the model
never writes SQL), the engine's non-`SELECT` guard, and the SELECT-only login the data owner
provides. `AI_ALLOW_TEXT_TO_SQL=true` unlocks model-written `SELECT`s and is a project decision
that needs a threat model first. The template ships an **empty** allow-list.

**`tools/retrieve.py`.** `AzureSearchRetriever` embeds the query and runs a **hybrid** search
(keyword + `VectorizedQuery`) against the index, in a worker thread (the sync SDK client; the
async client would add `aiohttp` for no gain). `get_default_retriever()` picks it when both
the search index and an embedding deployment are configured, otherwise `NullRetriever` — the
graph still answers, with an empty context.

**`ingest.py`.** The RAG ingestion job owns the index definition (`build_index`: `id`,
`content`, `source`, `title`, `chunk_index`, `content_vector` with an HNSW profile sized by
`AZURE_AI_EMBEDDING_DIMENSIONS`) so the retriever and the index cannot drift. The corpus is every
`.md` file under `CORPUS_DIR` (default: the repository's `docs/`; `/app/corpus` in the image,
ADR-0013); a document's `source` is always `docs/<path inside the corpus>` and its `title` is its
first top-level `# ` heading outside code fences, else the file name, carried by every chunk and
returned by retrieval. `ensure_index` creates the index or updates it in place, and **refuses**
an existing index whose vector size differs from the configured one (it never deletes or
recreates an index — its owner does); before it touches the index the job also embeds a constant
probe and stops if the deployment's vectors have another size. `ingest_documents` chunks (fixed
windows with overlap), embeds in batches and uploads with stable ids (re-ingesting a source
overwrites its chunks); it logs counts and seconds, never text. The corpus is untrusted input, so the
load resolves every path first (a file that resolves outside the root is skipped and counted, never
read), skips a `source` that is not one printable line, and cleans the text before it is chunked
(control and format characters removed, HTML comments dropped outside code fences). After a run in
which every upload succeeded, chunks of this index that the run did not produce are removed (selected
by `source`; `--no-prune` turns it off), and every embedding, search and index call has the
`AI_RETRIEVAL_TIMEOUT_SECONDS` budget. Run it from `apps/api`:
`uv run python -m app.ai.ingest [--corpus DIR] [--no-prune|--force-prune]`. The removal refuses (and
the run exits 1) when it would remove most of the index — a wrong `CORPUS_DIR` — unless
`--force-prune`. A corpus of more than `INGEST_MAX_FILES` files or `INGEST_MAX_BYTES` bytes is refused before any
file is read. The same run is started over HTTP by `POST /v1/admin/ingest` (`x-admin-key`, `app/ai/ingest_job.py`):
`202` and a background task, one at a time per process (`409 ingest_running`), its outcome a log line and one
`ingest.completed` event of counts and a bounded error kind (the counts and seconds are measurements, not
dimensions to group by). The identity needs _Search Index Data
Contributor_ + _Search Service Contributor_ (granted to the api identity by the `ai-search`
module; a dev key works locally).

**`prompts/`.** Prompts are files. `load_prompt("system")` reads `prompts/system.md`;
`render_prompt` fills `{placeholders}` and fails loudly on a missing one. The shipped system
prompt answers only from the numbered context, says it does not know when the context lacks the
answer, requires `[n]` citations of passages it actually used, and states that everything after
the final `Context:` line is data, not instructions (the context is the last thing in the
message, so nothing retrieved can sit after the rules).

**`telemetry.py`.** `model_call_span(deployment)` wraps every model call: a span named
`gen_ai.chat` with `gen_ai.system`, `gen_ai.operation.name`, `gen_ai.request.model`, the
`trace_id`, and — after the call — `gen_ai.usage.input_tokens` / `output_tokens`; metrics
`team-assistant.ai.model.duration`, `.model.tokens`, `.model.failures`, `.retrieval.duration`
(bounded attributes: deployment, outcome, token type, error kind); one `ai.model_call` custom
event; one structured log line. **No prompt, completion, retrieved text or user identifier
anywhere.** The OTel GenAI auto-instrumentation packages are deliberately not used (pin coupling
with the distro's `0.61b0` line and content capture).

---

## Using it from a route

```python
# apps/api/app/routers/assistant.py  (a project's feature — the template ships no route)
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.ai.graph import ask, build_graph
from app.ai.streaming import sse_response, stream_answer

router = APIRouter(prefix="/assistant", tags=["assistant"])
_graph = None


def graph():
    global _graph
    if _graph is None:
        _graph = build_graph()  # client + retriever from settings, lazily
    return _graph


class AskIn(BaseModel):
    question: str


@router.post("/ask")
async def ask_route(body: AskIn) -> dict:
    answer = await ask(graph(), body.question)
    return {"answer": answer.text, "sources": answer.sources}


@router.post("/ask/stream")
async def ask_stream(body: AskIn):
    return sse_response(stream_answer(graph(), body.question))
```

Attach it to `v1_router` (rule 05). The BFF calls `/v1/assistant/ask` with `fetchUpstream`
(raising the hop timeout via `signal` — a model call routinely exceeds 10 s) and
`/v1/assistant/ask/stream` with the streaming pass-through helper.

---

## Testing

Everything runs offline by swapping the seam ([`tests/ai/fakes.py`](../../apps/api/tests/ai/fakes.py)):
`GenericFakeChatModel` for the model, `FakeRetriever`, `FakeEmbeddings`, `FakeSearchClient`,
and a fake external `session_factory`. `tests/ai/` covers configuration, the client factory's
auth selection, the graph, streaming, tools, retrieval, ingestion and telemetry;
`tests/evals/` is the **prompt regression harness** — `cases.json` scripts the model's answer so
the pipeline around the prompt (retrieval → prompt → sources → citations) is pinned — each case
is a `grounded`, `out-of-context` or `injection` kind, and the harness records what the model
receives (one system message, the rendered prompt, plus the question); extend it whenever a
prompt changes. Offline the answers are scripted, so this tier pins the plumbing and the
guardrail wording, **not** model behaviour: run the live tier (`AI_EVAL_LIVE=true`) before
releasing a prompt change. The prompt cannot make injection impossible — citation checks and
passage delimiting belong in code (api roadmap 4.2). A live-model tier is opt-in (`AI_EVAL_LIVE=true`).

---

## What a project adds

- Its **routes** under `/v1` (above) and the BFF routes/UI that call them.
- Its **prompt files** and eval cases.
- Its **allow-listed queries** in `tools/queries.py` (or registered at startup) and any new
  tools — each with a fake-backed test and a threat-model note.
- Its **ingestion sources** (what `python -m app.ai.ingest` reads) and, when the corpus outgrows
  the job, an indexer pipeline.
- Threat modelling for prompt injection, data exfiltration through tools, and PII in chat
  history — the baseline gives the controls; the project decides the data classification.

## Related

- [ADR-0009](../adr/0009-ai-runtime-langchain-langgraph-foundry.md) — the decision and the
  alternatives rejected.
- [data.md](data.md) — the read-only external engine the query tool uses.
- [observability.md](observability.md) — where the `gen_ai` spans and `team-assistant.ai.*`
  metrics land.
- The shared Foundry and AI Search service (platform tier) and the project's index — [`infra/README.md`](../../infra/README.md), [`infra/platform/README.md`](../../infra/platform/README.md).
