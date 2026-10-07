"""Stored messages → the conversation history a model sees (api 4.2).

A conversation's earlier turns are replayed to the model so a follow-up question ("and on the api
side?") can be answered. They are **untrusted text**: a stored assistant message may itself have
been steered by a poisoned passage, and a user message is whatever someone typed. So the mapping is
deliberately narrow:

- ``user`` → a human message, ``assistant`` → an AI message — nothing else;
- any other role is **dropped**, never mapped to a system message: stored text must never reach
  the model in the system role (rule 70);
- only the two text fields are read — no id, no citations list, no timestamp is passed on.

A ``[n]`` inside an earlier answer refers to *that* turn's context, not to the current one; the
system prompt says so and ``app.ai.citations`` only ever resolves numbers against the current
retrieval.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

HISTORY_MESSAGES = 10
# A stored row is at most this long today (the question cap); replay never trusts that.
MAX_HISTORY_CHARS = 4_000


def history_messages(rows: Iterable[Any]) -> list[AnyMessage]:
    """The messages (oldest first, as given) a model should see as earlier turns."""
    history: list[AnyMessage] = []
    for row in rows:
        role = getattr(row, "role", None)
        content = getattr(row, "content", None)
        if not isinstance(content, str):
            continue
        if role == "user":
            history.append(HumanMessage(content=content[:MAX_HISTORY_CHARS]))
        elif role == "assistant":
            history.append(AIMessage(content=content[:MAX_HISTORY_CHARS]))
        # any other role: dropped — it is never turned into a system message
    return history
