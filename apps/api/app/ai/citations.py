"""Citations: which retrieved passages an answer cites, and which of its ``[n]`` markers are bogus.

The model writes ``[n]`` after statements that rest on passage *n* of the context (the prompt in
``prompts/system.md`` says so). Everything the api later shows or stores as a *citation* must come
from retrieval, never from what the model wrote — so this module only ever **reads** the markers
as numbers and looks the passages up in the list retrieval returned:

- ``citation_numbers`` — the numbers an answer cites, in order of first appearance;
- ``invalid_citation_numbers`` — those that point to no passage (``0``, or past the last one);
- ``cited_chunks`` — the retrieved passages the answer actually cites, each document once;
- ``citation_of`` — a ``{title, path}`` pair built from a retrieved passage (never from text the
  model wrote). Both strings are one bounded line of printable text but are otherwise **untrusted**
  (``<``, ``(`` and a ``javascript:`` prefix survive): render them as plain text, never as markup
  or as a link target.

What counts as a marker: one to three ASCII digits, alone (``[2]``) or comma-separated
(``[1, 3]``); ``[1][2]`` is two markers. Not inside code — a fenced block (an unclosed fence runs to
the end of the text, as in Markdown) or an inline span: ``items[1]`` is code. Longer numbers
(``[1000]``) are not markers, so ``invalid_citation_numbers`` is a **lower bound**: a count of 0
does not prove the answer is clean. Only the first ``MAX_SCAN_CHARS`` characters are read, which
keeps the work bounded however long a model reply is. The answer text itself is never rewritten
here — a bad marker is reported (a count, no text), not repaired.
"""

from __future__ import annotations

import re

from app.ai.tools.retrieve import RetrievedChunk

MAX_TITLE_LENGTH = 200
MAX_SCAN_CHARS = 200_000

# Up to 3 leading spaces, then a run of 3+ backticks or tildes (an opening or closing fence).
_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"`[^`\n]*`")
# ASCII only: a fullwidth or Arabic-Indic digit is not a citation the page would show as one.
_MARKER = re.compile(r"\[\s*([0-9]{1,3}(?:\s*,\s*[0-9]{1,3})*)\s*\]", re.ASCII)


def _strip_fenced(text: str) -> str:
    """``text`` without its fenced code blocks — one linear pass over the lines.

    A block opens on a line that starts (after up to 3 spaces) with 3+ backticks or tildes — for
    backticks, with no further backtick on the line — and closes on a line holding only a run of the
    same character that is at least as long. A block still open at the end runs to the end.
    """
    kept: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.splitlines():
        opening = _FENCE_LINE.match(line)
        if fence is None:
            if opening:
                run = opening.group(1)
                if run[0] == "~" or "`" not in line[opening.end() :]:
                    fence = (run[0], len(run))
                    continue
            kept.append(line)
        elif opening:
            run = opening.group(1)
            if run[0] == fence[0] and len(run) >= fence[1] and line.strip() == run:
                fence = None
    return "\n".join(kept)


def _without_code(text: str) -> str:
    return _INLINE_CODE.sub(" ", _strip_fenced(text[:MAX_SCAN_CHARS]))


def citation_numbers(text: str) -> list[int]:
    """The numbers cited in ``text``, each once, in order of first appearance."""
    seen: set[int] = set()
    ordered: list[int] = []
    for match in _MARKER.finditer(_without_code(text)):
        for part in match.group(1).split(","):
            number = int(part)
            if number not in seen:
                seen.add(number)
                ordered.append(number)
    return ordered


def invalid_citation_numbers(text: str, passage_count: int) -> list[int]:
    """The cited numbers that point to no passage of a context with ``passage_count`` passages."""
    return [n for n in citation_numbers(text) if not 1 <= n <= passage_count]


def cited_chunks(text: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """The retrieved passages ``text`` cites — valid numbers only, one per document (``source``),
    in order of first citation. A passage that was not retrieved can never appear here."""
    cited: list[RetrievedChunk] = []
    sources: set[str] = set()
    for number in citation_numbers(text):
        if not 1 <= number <= len(chunks):
            continue
        chunk = chunks[number - 1]
        key = chunk.get("source") or f"#{number}"
        if key in sources:
            continue
        sources.add(key)
        cited.append(chunk)
    return cited


def _file_name(source: str) -> str:
    return source.replace("\\", "/").rsplit("/", 1)[-1]


def citation_of(chunk: RetrievedChunk) -> dict[str, str]:
    """``{"title", "path"}`` for a retrieved passage: its stored title (else its file name) and its
    ``source`` path, one printable line each, bounded in length. Untrusted text — see the module
    docstring."""

    def one_line(value: str) -> str:
        spaced = "".join(ch if ch.isprintable() else " " for ch in value)
        return " ".join(spaced.split())[:MAX_TITLE_LENGTH]

    path = one_line(chunk.get("source") or "")
    title = one_line(chunk.get("title") or "") or one_line(_file_name(path)) or "untitled"
    return {"title": title, "path": path}
