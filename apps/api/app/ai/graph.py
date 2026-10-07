"""The LangGraph skeleton (ADR-0009): ``retrieve → answer`` (+ an optional tool loop).

Shape a project copies for its own graphs:

- **Typed state** (``GraphState``): the message history (``add_messages`` reducer) and the
  retrieved context.
- **Nodes are small async functions** that do one I/O each, with telemetry *inside* the node.
- **Retrieval is deterministic** — it runs before the model on every turn, so grounding never
  depends on the model deciding to call a tool.
- **The model is injected** (``chat_model=``) or taken from the ``client.py`` seam; tests pass
  a fake. Same for the retriever.
- **Tools are optional**: pass ``tools=[...]`` to add a ``ToolNode`` loop
  (``answer → tools → answer`` until the model stops calling tools).

``ask()`` is the non-streaming entry point; ``streaming.py`` streams the same compiled graph.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition

from app.ai.citations import cited_chunks, invalid_citation_numbers
from app.ai.config import get_ai_settings
from app.ai.prompts import render_prompt
from app.ai.telemetry import extract_usage, model_call_span, record_retrieval
from app.ai.tools.retrieve import DEFAULT_TOP_K, RetrievedChunk, Retriever, get_default_retriever
from app.logging_config import get_logger


class GraphState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    context: list[RetrievedChunk]


MAX_SOURCE_LENGTH = 200

# Lines of a passage that would read as structure of the prompt if left as they are: a passage
# header (`[2] (docs/x.md)`, or a bare `[2]` whose `(…)` sits on the next line) or the prompt's own
# `Context:` marker. They are recognised on the line's *body* — see `_body`.
_HEADER_BODY = re.compile(r"\[\s*\d+\s*\](?:\s*\(|\s*$)")
_MARKER_BODY = re.compile(r"context\s*:", re.IGNORECASE)
# Characters that may sit in front of a header without changing how a model reads it: quote, list,
# heading, emphasis and table marks (on top of whitespace and format/control characters).
_DECORATION = ">-*+#_|"


def _body(line: str) -> str:
    """The start of ``line`` with leading decoration removed and the rest folded to NFKC (so
    fullwidth ``［２］（`` reads as ``[2](``), cut to 80 characters: enough to see a header."""
    start = 0
    while start < len(line) and (
        line[start].isspace()
        or line[start] in _DECORATION
        or unicodedata.category(line[start]) in ("Cf", "Cc", "Zs")
    ):
        start += 1
    return unicodedata.normalize("NFKC", line[start : start + 80])


def _looks_structural(line: str) -> bool:
    body = _body(line)
    return bool(_HEADER_BODY.match(body) or _MARKER_BODY.match(body))


def _header_source(source: str) -> str:
    """A ``source`` fit for a header: one printable line, no brackets or parentheses, bounded —
    so it can neither break onto a second line nor close the header's own parentheses."""
    spaced = "".join(ch if ch.isprintable() else " " for ch in source)
    flat = " ".join(spaced.split()).translate(str.maketrans("()[]", "____"))
    return flat[:MAX_SOURCE_LENGTH] or "unknown"


def _neutralise(content: str) -> str:
    """Escape, with a leading backslash, every line of a passage that looks like a passage header
    or like the prompt's ``Context:`` marker (also behind a quote or list mark, a zero-width or
    bidirectional character, or in fullwidth form), so a retrieved document cannot pose as another
    passage or as the start of the context. Every other line is kept as it is; line separators
    (``\\r\\n``, U+2028, …) become ``\\n``."""
    return "\n".join(
        "\\" + line if _looks_structural(line) else line for line in content.splitlines()
    )


def format_context(chunks: list[RetrievedChunk]) -> str:
    """Numbered context block for the system prompt (the model cites ``[n]``).

    The numbers and headers are written here, by the code: a passage's ``source`` is flattened to
    one line and its text has header-like and ``Context:`` lines escaped (``_neutralise``), so the
    only lines that read ``[n] (…)`` at the start are the real headers, numbered 1..N in order.
    """
    if not chunks:
        return "(no context available)"
    return "\n\n".join(
        f"[{i}] ({_header_source(c.get('source') or '')})\n{_neutralise(c.get('content', ''))}"
        for i, c in enumerate(chunks, start=1)
    )


def last_user_text(messages: list[AnyMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    # Content blocks → join the text parts.
    parts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content]
    return "".join(parts)


def build_graph(
    *,
    chat_model: BaseChatModel | None = None,
    retriever: Retriever | None = None,
    tools: list[BaseTool] | None = None,
    top_k: int = DEFAULT_TOP_K,
    deployment_label: str | None = None,
) -> Any:
    """Compile the ``retrieve → answer`` graph. Nothing connects until the first run."""

    def _model() -> BaseChatModel:
        if chat_model is not None:
            return chat_model
        from app.ai.client import get_chat_model

        return get_chat_model()

    def _label() -> str:
        return deployment_label or get_ai_settings().chat_deployment or "unknown"

    bound_tools = list(tools or [])

    async def retrieve(state: GraphState) -> GraphState:
        question = last_user_text(state.get("messages", []))
        active = retriever if retriever is not None else get_default_retriever()
        started = time.perf_counter()
        try:
            chunks = await active.retrieve(question, top_k) if question else []
        except Exception:
            record_retrieval((time.perf_counter() - started) * 1000, "error")
            raise
        return {"context": chunks}

    async def answer(state: GraphState) -> GraphState:
        model = _model()
        if bound_tools:
            model = model.bind_tools(bound_tools)
        system = SystemMessage(
            content=render_prompt("system", context=format_context(state.get("context", [])))
        )
        with model_call_span(_label()) as call:
            response = await model.ainvoke([system, *state.get("messages", [])])
            call.set_usage(response)
        return {"messages": [response]}

    graph = StateGraph(GraphState)
    graph.add_node("retrieve", retrieve)
    graph.add_node("answer", answer)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "answer")
    if bound_tools:
        graph.add_node("tools", ToolNode(bound_tools))
        graph.add_conditional_edges("answer", tools_condition, {"tools": "tools", END: END})
        graph.add_edge("tools", "answer")
    else:
        graph.add_edge("answer", END)
    return graph.compile()


@dataclass
class Answer:
    text: str
    sources: list[str] = field(default_factory=list)
    context: list[RetrievedChunk] = field(default_factory=list)
    # The retrieved passages the answer cites (valid `[n]` only, one per document) — what a
    # route stores and returns as citations. `sources` is every retrieved document.
    cited: list[RetrievedChunk] = field(default_factory=list)
    # How many `[n]` markers in the text point to no passage (a count, never the text).
    invalid_citations: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None


def unique_sources(chunks: list[RetrievedChunk]) -> list[str]:
    seen: list[str] = []
    for c in chunks:
        source = c.get("source") or ""
        if source and source not in seen:
            seen.append(source)
    return seen


async def ask(
    graph: Any,
    question: str,
    *,
    history: list[AnyMessage] | None = None,
    config: dict[str, Any] | None = None,
) -> Answer:
    """Run the graph once for ``question`` (with optional prior turns) and collect the answer."""
    inputs: GraphState = {
        "messages": [*(history or []), HumanMessage(content=question)],
        "context": [],
    }
    result = await graph.ainvoke(inputs, config)
    final = result["messages"][-1]
    text = _message_text(final) if isinstance(final, AIMessage) else str(final)
    input_tokens, output_tokens = extract_usage(final)
    context = result.get("context", []) or []
    cited = cited_chunks(text, context)
    invalid = len(invalid_citation_numbers(text, len(context)))
    get_logger("app.ai.graph").info(
        "answer citations", retrieved=len(context), cited=len(cited), invalid=invalid
    )
    return Answer(
        text=text,
        sources=unique_sources(context),
        context=context,
        cited=cited,
        invalid_citations=invalid,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
