"""RAG ingestion job (ADR-0009): load → chunk → embed → upload to Azure AI Search.

Owns the **index definition** (``build_index``) so the retriever and the index never drift:
fields ``id`` (key), ``content`` (searchable), ``source`` (filterable, the ``docs/…`` path),
``title`` (searchable, the document's first ``# `` heading or else its file name),
``chunk_index``, and ``content_vector`` (HNSW vector profile, ``AZURE_AI_EMBEDDING_DIMENSIONS``
wide). ``ensure_index`` is idempotent, and it **refuses** to touch an index whose vector size
differs from the configured one — it never reuses or recreates it silently.

The corpus is every ``.md`` file under one directory (decision 14): ``CORPUS_DIR`` when set, else
``/app/corpus`` when ``APP_ENV`` is not ``local`` and the image put one there (the repository's
``docs/**/*.md``, checked for secrets at build time, ADR-0013), else the repository's ``docs/``.
A document's ``source`` is always ``docs/<path inside that directory>``, so citations read the
same locally and in the container.

The corpus is untrusted input (threat model T3, G3, G6), so the load is defensive: a file whose
resolved path leaves the corpus root (a symlink, ``..``) is skipped and counted, never read; a file
whose ``source`` would not be one printable line, a hard link, a hidden file or a very large one is
skipped and counted; the text of every file is cleaned before it is chunked (control, format and
other invisible characters removed, HTML comments dropped everywhere — a comment is invisible to a
reader and fully visible to a model) and the title is read from the cleaned text.

After a run in which every upload succeeded, chunks of this index that the run did not produce are
removed — only chunks whose ``source`` starts with ``docs/``, never when a file could not be
resolved, and not when that would be most of the index (a wrong corpus directory looks like that:
``--force-prune`` overrides, ``--no-prune`` turns the removal off). Every embedding, search and
index call has an explicit timeout (``AI_RETRIEVAL_TIMEOUT_SECONDS``, 30 s). Logs and the printed
report carry counts and seconds only.

Run it from ``apps/api`` (needs the AI + search settings; the api identity or a dev key with
*Search Index Data Contributor* + *Search Service Contributor*)::

    uv run python -m app.ai.ingest                      # the corpus directory
    uv run python -m app.ai.ingest --corpus ../../docs --chunk-size 800 --overlap 80
    uv run python -m app.ai.ingest --no-prune           # upload only, remove nothing
    uv run python -m app.ai.ingest --force-prune        # also when that removes most of the index

Batches are embedded with ``aembed_documents`` and uploaded with ``upload_documents``; the
report prints counts and seconds only — never content. Very large corpora deserve a real
pipeline (an indexer + skillset); this job is the starting point.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
import time
import unicodedata
from collections.abc import Awaitable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from langchain_core.embeddings import Embeddings

from app.ai.config import (
    DEFAULT_RETRIEVAL_TIMEOUT_SECONDS,
    AINotConfigured,
    AISettings,
    get_ai_settings,
)
from app.ai.tools.retrieve import (
    CONTENT_FIELD,
    ID_FIELD,
    SEARCH_RETRY_TOTAL,
    SOURCE_FIELD,
    TITLE_FIELD,
    VECTOR_FIELD,
    build_search_client,
    build_search_credential,
)
from app.config import get_settings
from app.logging_config import configure_logging, get_logger

CHUNK_INDEX_FIELD = "chunk_index"
DEFAULT_CHUNK_SIZE = 1000
DEFAULT_OVERLAP = 100
VECTOR_PROFILE = "vector-profile"
HNSW_CONFIG = "hnsw"
MARKDOWN_SUFFIX = ".md"
SOURCE_PREFIX = "docs"
MAX_TITLE_LENGTH = 200
# A ``source`` is a path: longer than this is not a document path, it is a file name made to be one.
MAX_SOURCE_LENGTH = 300
# Documents removed from the index per request (the service accepts up to 1,000).
DELETE_BATCH_SIZE = 500
# A file over this many bytes is not documentation (and is read whole into memory): skipped.
MAX_FILE_BYTES = 2_000_000
# A line longer than this is not a heading (and the heading pattern is slow on long space runs).
MAX_HEADING_LINE = 1000
# Removing at least this many chunks, and more than this fraction of the index's own, looks like a
# wrong corpus directory rather than an edit: the run refuses unless it is forced.
PRUNE_BRAKE_MIN = 20
PRUNE_BRAKE_FRACTION = 0.5
# A constant string: the dimension check never sends document text.
DIMENSION_PROBE = "dimension check"


class IngestError(RuntimeError):
    """Ingestion stopped before changing anything it should not have; the message says why."""


class CorpusNotFound(IngestError):
    """The corpus directory does not exist."""


class CorpusEmpty(IngestError):
    """The corpus directory holds no ``.md`` file."""


class CorpusTooLarge(IngestError):
    """The corpus is over its size cap (threat model G7): refused before any file is read."""

    def __init__(
        self,
        files: int,
        size: int,
        max_files: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.files, self.size = files, size
        self.max_files, self.max_bytes = max_files, max_bytes
        over: list[str] = []
        if max_files is not None and files > max_files:
            over.append(f"{files} files, over the cap of {max_files} (INGEST_MAX_FILES)")
        if max_bytes is not None and size > max_bytes:
            over.append(f"{size} bytes, over the cap of {max_bytes} (INGEST_MAX_BYTES)")
        super().__init__(
            f"the corpus is too large: {' and '.join(over) or 'over its cap'}. "
            f"Nothing was uploaded or removed"
        )


class IngestTimeout(IngestError):
    """One call of the run took longer than its budget; the run stopped there."""

    def __init__(self, step: str, seconds: float) -> None:
        self.step, self.seconds = step, seconds
        super().__init__(
            f"{step} took longer than {seconds:g} s (AI_RETRIEVAL_TIMEOUT_SECONDS), so the run "
            f"stopped (a call that timed out may still finish, so the index may be partly updated)"
        )


class PruneRefused(IngestError):
    """The stale chunks are most of the index: the upload stands and nothing is removed."""

    def __init__(self, stale: int, total: int) -> None:
        self.stale, self.total = stale, total
        super().__init__(
            f"{stale} of {total} chunks would be removed, which looks like a wrong corpus "
            f"directory (CORPUS_DIR or --corpus), not an edit. The upload completed and nothing "
            f"was removed; "
            f"check the directory, or run again with --force-prune"
        )


class IndexDimensionMismatch(IngestError):
    """The index exists with a vector size other than the configured one."""

    def __init__(self, index: str, actual: int, expected: int) -> None:
        self.index, self.actual, self.expected = index, actual, expected
        super().__init__(
            f'search index "{index}" has vectors of {actual} dimensions, but '
            f"AZURE_AI_EMBEDDING_DIMENSIONS is {expected}. The index was not changed. An index "
            f"cannot be resized in place: ask its owner to delete it, or use the dimension it "
            f"was built with."
        )


class IndexHasNoVectorField(IngestError):
    """The index exists but has no ``content_vector`` field, so its vector size is unknown."""

    def __init__(self, index: str) -> None:
        super().__init__(
            f'search index "{index}" exists but has no "{VECTOR_FIELD}" vector field, so its '
            f"vector size cannot be checked. The index was not changed; ask its owner to delete "
            f"it so it can be created with the right shape."
        )


class CorpusUnreadable(IngestError):
    """A ``.md`` file is not valid UTF-8 text."""


class EmbeddingDimensionMismatch(IngestError):
    """The embedding deployment returns vectors of a size other than the configured one."""

    def __init__(self, actual: int, expected: int) -> None:
        self.actual, self.expected = actual, expected
        super().__init__(
            f"the embedding deployment returns vectors of {actual} dimensions, but "
            f"AZURE_AI_EMBEDDING_DIMENSIONS is {expected}. The run stopped before uploading the "
            f"batch that failed; set the variable to {actual} (text-embedding-3-large is 3072) "
            f"or change the deployment."
        )


@dataclass(frozen=True)
class IngestDocument:
    source: str
    text: str
    title: str = ""


@dataclass
class IngestReport:
    """What a run did, as counts and seconds — never a document's text or a list of paths."""

    documents: int = 0
    chunks: int = 0
    uploaded: int = 0
    failed: int = 0
    seconds: float = 0.0
    skipped_outside_root: int = 0
    skipped_unsafe: int = 0
    skipped_unresolved: int = 0
    cleaned: int = 0
    removed: int = 0
    removal_failed: int = 0
    removal_refused: bool = False
    # Why nothing was removed when it was refused (counts only), for the command line to print.
    removal_refusal: str = field(default="", repr=False, compare=False)
    # What the run put in the index (ids), for the stale-chunk pass; not part of the report.
    live_ids: set[str] = field(default_factory=set, repr=False, compare=False)


@dataclass(frozen=True)
class CorpusLoad:
    """The documents of a corpus directory, and what the load skipped or changed (counts)."""

    documents: list[IngestDocument]
    skipped_outside_root: int = 0
    skipped_unsafe: int = 0
    # Files whose path could not be resolved (a loop, a vanished target, an I/O error): not known to
    # be outside the root, so a run with any of them removes nothing from the index.
    skipped_unresolved: int = 0
    cleaned: int = 0


def chunk_text(
    text: str, size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_OVERLAP
) -> list[str]:
    """Fixed-size character windows with overlap; whitespace-only chunks are dropped."""
    if size <= 0:
        raise ValueError("size must be > 0")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must be >= 0 and < size")
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    step = size - overlap
    for start in range(0, len(text), step):
        piece = text[start : start + size].strip()
        if piece:
            chunks.append(piece)
        if start + size >= len(text):
            break
    return chunks


def chunk_id(source: str, index: int) -> str:
    """Stable, key-safe id (re-ingesting the same source overwrites the same documents)."""
    return hashlib.sha1(f"{source}#{index}".encode()).hexdigest()


# `# Title`, `# Title #` (closing hashes), but `# C#` keeps its `#`; up to 3 leading spaces.
_HEADING = re.compile(r"^ {0,3}#[ \t]+(\S.*?)(?:[ \t]+#+)?[ \t]*$")
# A code fence opens with 3+ backticks or tildes (up to 3 leading spaces).
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def clean_title(raw: str) -> str:
    """One line of printable text: control and format characters become spaces, runs of
    whitespace collapse, and the length is bounded."""
    spaced = "".join(ch if ch.isprintable() else " " for ch in raw)
    return " ".join(spaced.split())[:MAX_TITLE_LENGTH]


def clean_source(raw: str) -> str:
    """One line of printable text, bounded — the same rules as a title, for a path."""
    spaced = "".join(ch if ch.isprintable() else " " for ch in raw)
    return " ".join(spaced.split())[:MAX_SOURCE_LENGTH]


# Neither control nor format characters, yet they show as nothing (or carry hidden bytes):
# variation selectors and a few blank letters.
_INVISIBLE = frozenset(
    [*range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0), 0x3164, 0xFFA0, 0x115F, 0x1160, 0x2800]
)
# Control characters that separate words: they become newlines, not nothing.
_SEPARATORS = "\x0b\x0c\x85\u2028\u2029"


def is_safe_source(source: str) -> bool:
    """A ``source`` is safe when it is one printable line (no newline, control or format character)
    and not longer than ``MAX_SOURCE_LENGTH``; spacing in a file name is its own business."""
    return 0 < len(source) <= MAX_SOURCE_LENGTH and source.isprintable()


def _clean_characters(text: str) -> str:
    """Drop control and format characters (NUL, zero-width and bidirectional marks, the byte-order
    mark, Unicode tag characters…) and the invisible ones in ``_INVISIBLE``, but keep newlines and
    tabs; characters that separate words (vertical tab, form feed, NEL, line and paragraph
    separators) become newlines so words do not run together."""
    out: list[str] = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
        elif ch in _SEPARATORS:
            out.append("\n")
        elif ord(ch) in _INVISIBLE or unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        else:
            out.append(ch)
    return "".join(out)


def strip_html_comments(text: str) -> str:
    """Remove ``<!-- … -->`` everywhere — inside code fences too: a comment is invisible in a
    rendered page and fully visible to a model, and whether a line opens a fence is a rendering
    decision the attacker controls. An unterminated one runs to the end. Linear in the size of the
    text."""
    if "<!--" not in text:
        return text
    out: list[str] = []
    position = 0
    while True:
        start = text.find("<!--", position)
        if start == -1:
            out.append(text[position:])
            break
        out.append(text[position:start])
        end = text.find("-->", start + 4)
        if end == -1:
            break
        position = end + 3
    return "".join(out)


def clean_text(text: str) -> str:
    """The text of a document as it is chunked: characters first (so a zero-width character inside
    ``<!--`` cannot hide a comment from the next step), then comments."""
    return strip_html_comments(_clean_characters(text))


def extract_title(text: str, fallback: str) -> str:
    """The first top-level ``# `` heading outside a code fence (and outside front matter),
    else ``fallback`` (the file name). Either way the result goes through ``clean_title``;
    a leading byte-order mark is ignored."""
    lines = text.lstrip("\ufeff").splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for number in range(1, len(lines)):
            if lines[number].strip() == "---":
                start = number + 1
                break
    fence: tuple[str, int] | None = None
    for line in lines[start:]:
        opening = _FENCE.match(line)
        if opening:
            run = opening.group(1)
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and line.strip() == run:
                fence = None  # a closing fence: same character, at least as long, no info string
            continue
        if fence is not None:
            continue
        if len(line) > MAX_HEADING_LINE:
            continue
        match = _HEADING.match(line)
        if match:
            title = clean_title(match.group(1))
            if title:
                return title
    return clean_title(fallback) or "untitled"


def build_index(name: str, dimensions: int) -> Any:
    """The ``SearchIndex`` the retriever expects (keyword + vector fields, HNSW profile)."""
    from azure.search.documents.indexes.models import (
        HnswAlgorithmConfiguration,
        SearchableField,
        SearchField,
        SearchFieldDataType,
        SearchIndex,
        SimpleField,
        VectorSearch,
        VectorSearchProfile,
    )

    fields = [
        SimpleField(name=ID_FIELD, type=SearchFieldDataType.String, key=True, filterable=True),
        SearchableField(name=CONTENT_FIELD),
        SimpleField(name=SOURCE_FIELD, type=SearchFieldDataType.String, filterable=True),
        SearchableField(name=TITLE_FIELD),
        SimpleField(
            name=CHUNK_INDEX_FIELD, type=SearchFieldDataType.Int32, filterable=True, sortable=True
        ),
        SearchField(
            name=VECTOR_FIELD,
            type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
            searchable=True,
            vector_search_dimensions=dimensions,
            vector_search_profile_name=VECTOR_PROFILE,
        ),
    ]
    vector_search = VectorSearch(
        algorithms=[HnswAlgorithmConfiguration(name=HNSW_CONFIG)],
        profiles=[
            VectorSearchProfile(name=VECTOR_PROFILE, algorithm_configuration_name=HNSW_CONFIG)
        ],
    )
    return SearchIndex(name=name, fields=fields, vector_search=vector_search)


def build_index_client(settings: AISettings | None = None) -> Any:
    from azure.search.documents.indexes import SearchIndexClient

    s = settings or get_ai_settings()
    s.require_search()
    # azure-core's defaults are 300 s timeouts and 10 retries: explicit, like the search client.
    return SearchIndexClient(
        endpoint=s.search_endpoint,
        credential=build_search_credential(s),
        connection_timeout=s.retrieval_timeout_seconds,
        read_timeout=s.retrieval_timeout_seconds,
        retry_total=SEARCH_RETRY_TOTAL,
    )


def vector_dimensions(index: Any) -> int | None:
    """The vector size of an index's ``content_vector`` field, if it has one."""
    for index_field in index.fields:
        if index_field.name == VECTOR_FIELD:
            return index_field.vector_search_dimensions
    return None


def ensure_index(index_client: Any, index: Any) -> Any:
    """Create the index, or update it in place when its vector size already matches.

    An existing index with a *different* vector size raises ``IndexDimensionMismatch`` and is
    left untouched: this job never deletes or recreates an index (its owner does).
    """
    try:
        existing = index_client.get_index(index.name)
    except ResourceNotFoundError:
        existing = None
    if existing is not None:
        actual = vector_dimensions(existing)
        expected = vector_dimensions(index)
        if actual is None:
            raise IndexHasNoVectorField(index.name)
        if expected is not None and actual != expected:
            raise IndexDimensionMismatch(index.name, actual, expected)
    return index_client.create_or_update_index(index)


async def _bounded[T](step: str, seconds: float, awaitable: Awaitable[T]) -> T:
    """Await with a deadline; a timeout becomes an ``IngestTimeout`` naming the step."""
    try:
        async with asyncio.timeout(seconds):
            return await awaitable
    except TimeoutError as exc:
        raise IngestTimeout(step, seconds) from exc


async def check_embedding_dimensions(
    embeddings: Embeddings,
    expected: int,
    *,
    timeout_seconds: float = DEFAULT_RETRIEVAL_TIMEOUT_SECONDS,
) -> None:
    """Embed a constant probe and compare its size, before the index is touched."""
    vector = await _bounded(
        "the embedding dimension check",
        timeout_seconds,
        embeddings.aembed_query(DIMENSION_PROBE),
    )
    if len(vector) != expected:
        raise EmbeddingDimensionMismatch(len(vector), expected)


# Where the api image keeps its corpus (``apps/api/Dockerfile``, ADR-0013): the repo's docs, as .md.
IMAGE_CORPUS_DIR = Path("/app/corpus")


def default_corpus_dir() -> Path:
    """Where the corpus is when ``CORPUS_DIR`` is not set: ``/app/corpus`` when the environment is
    not ``local`` and the image put one there; otherwise the repository's ``docs/`` (found from
    this file), else ``./docs``."""
    if get_settings().app_env != "local" and IMAGE_CORPUS_DIR.is_dir():
        return IMAGE_CORPUS_DIR
    try:
        candidate = Path(__file__).resolve().parents[4] / "docs"
    except IndexError:  # the installed image has a shallower tree
        return Path("docs")
    return candidate if candidate.is_dir() else Path("docs")


def resolve_corpus_dir(settings: AISettings | None = None) -> Path:
    s = settings or get_ai_settings()
    return Path(s.corpus_dir) if s.corpus_dir else default_corpus_dir()


def scan_corpus(
    root: Path,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> CorpusLoad:
    """Every ``.md`` file under ``root`` as a document, sorted by path, and what was skipped.

    ``source`` is ``docs/<path inside root>`` (``/`` separators), whatever ``root`` is, so a
    citation reads the same on a laptop and in the image. Each candidate's path is **resolved
    first** and the resolved path is what is read: one that resolves outside the root (a symlink,
    a junction, ``..``) is skipped and counted without being read. Also skipped and counted: a
    link whose target is not ``.md``, a source that is not one printable line, a hidden file or
    directory, a hard link, and a file over ``MAX_FILE_BYTES``. A path that cannot be resolved at
    all is counted apart (``skipped_unresolved``) because it is not known to be outside. The text
    that is kept has been through ``clean_text``; ``cleaned`` counts the files that changed by it.

    With ``max_files`` / ``max_bytes`` set, a corpus of more ``.md`` files than that, or whose files
    add up to more bytes, raises ``CorpusTooLarge`` **before any file is read** (threat model G7);
    the
    directory walk itself is not bounded. Every ``.md`` counts, including one that would be skipped
    later, and a link counts by its target's size: the cap errs on the side of refusing.
    """
    if not root.is_dir():
        raise CorpusNotFound(f"the corpus directory {root} does not exist")
    real_root = root.resolve()
    docs: list[IngestDocument] = []
    outside = unsafe = unresolved = cleaned = 0
    # Sorted on the posix path, not the Path: Windows compares paths case-insensitively.
    candidates = [
        p
        for p in sorted(
            (p for p in root.rglob("*") if p.is_file()),
            key=lambda p: p.relative_to(root).as_posix(),
        )
        if p.suffix.lower() == MARKDOWN_SUFFIX
    ]
    if max_files is not None or max_bytes is not None:
        total = 0
        for p in candidates:
            try:
                total += p.stat().st_size
            except OSError:
                pass  # counted as unresolved below
        if (max_files is not None and len(candidates) > max_files) or (
            max_bytes is not None and total > max_bytes
        ):
            raise CorpusTooLarge(len(candidates), total, max_files, max_bytes)
    for file in candidates:
        try:
            resolved = file.resolve(strict=True)
        except OSError, RuntimeError:  # a loop, a vanished target, an unreadable link
            unresolved += 1
            continue
        if not resolved.is_relative_to(real_root):
            outside += 1
            continue
        relative = file.relative_to(root).as_posix()
        source = f"{SOURCE_PREFIX}/{relative}"
        if (
            not is_safe_source(source)
            or resolved.suffix.lower() != MARKDOWN_SUFFIX
            or any(part.startswith(".") for part in relative.split("/"))
        ):
            unsafe += 1
            continue
        try:
            info = resolved.stat()
        except OSError:
            unresolved += 1
            continue
        if info.st_nlink > 1 or info.st_size > MAX_FILE_BYTES:
            unsafe += 1
            continue
        try:
            # utf-8-sig drops a byte-order mark; invalid bytes fail the run, never become U+FFFD.
            raw = resolved.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CorpusUnreadable(f"{relative} is not valid UTF-8 text") from exc
        except OSError as exc:
            raise CorpusUnreadable(f"{relative} could not be read") from exc
        text = clean_text(raw)
        if text != raw:
            cleaned += 1
        docs.append(IngestDocument(source=source, text=text, title=extract_title(text, file.name)))
    return CorpusLoad(
        documents=docs,
        skipped_outside_root=outside,
        skipped_unsafe=unsafe,
        skipped_unresolved=unresolved,
        cleaned=cleaned,
    )


def load_corpus(root: Path) -> list[IngestDocument]:
    """The documents of ``scan_corpus`` (see there for what is skipped and cleaned)."""
    return scan_corpus(root).documents


def _batches(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


async def ingest_documents(
    documents: Iterable[IngestDocument],
    *,
    embeddings: Embeddings,
    search_client: Any,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    batch_size: int = 64,
    expected_dimensions: int | None = None,
    timeout_seconds: float = DEFAULT_RETRIEVAL_TIMEOUT_SECONDS,
) -> IngestReport:
    """Chunk, embed and upload. Returns counts and seconds only.

    With ``expected_dimensions`` set, a batch whose vectors have another size stops the run
    before it is uploaded. Each embedding call and each upload has ``timeout_seconds``; one that
    exceeds it stops the run (``IngestTimeout``).
    """
    log = get_logger("app.ai.ingest")
    started = time.perf_counter()
    report = IngestReport()
    records: list[dict[str, Any]] = []
    for doc in documents:
        report.documents += 1
        title = doc.title or Path(doc.source).name
        for i, piece in enumerate(chunk_text(doc.text, chunk_size, overlap)):
            records.append(
                {
                    ID_FIELD: chunk_id(doc.source, i),
                    CONTENT_FIELD: piece,
                    SOURCE_FIELD: doc.source,
                    TITLE_FIELD: title,
                    CHUNK_INDEX_FIELD: i,
                }
            )
    report.chunks = len(records)
    report.live_ids = {r[ID_FIELD] for r in records}
    for batch in _batches(records, batch_size):
        vectors = await _bounded(
            "embedding a batch",
            timeout_seconds,
            embeddings.aembed_documents([r[CONTENT_FIELD] for r in batch]),
        )
        if expected_dimensions is not None:
            for vector in vectors:
                if len(vector) != expected_dimensions:
                    raise EmbeddingDimensionMismatch(len(vector), expected_dimensions)
        payload = [{**r, VECTOR_FIELD: v} for r, v in zip(batch, vectors, strict=True)]
        results = await _bounded(
            "uploading a batch",
            timeout_seconds,
            asyncio.to_thread(search_client.upload_documents, documents=payload),
        )
        ok = sum(1 for r in results if getattr(r, "succeeded", False))
        report.uploaded += ok
        report.failed += len(payload) - ok
        log.info("ingest batch uploaded", uploaded=ok, failed=len(payload) - ok)
    report.seconds = round(time.perf_counter() - started, 2)
    log.info(
        "ingest complete",
        documents=report.documents,
        chunks=report.chunks,
        uploaded=report.uploaded,
        failed=report.failed,
        seconds=report.seconds,
    )
    return report


def _list_index(search_client: Any) -> tuple[list[tuple[str, str]], int | None, int]:
    """``(id, source)`` of every chunk in the index that has a source, the number of chunks the
    service says it holds (``None`` if it does not say), and how many rows came back. The SDK pages
    through the results; a chunk without a ``source`` is not one this job made, so it is not listed.
    """
    rows = search_client.search(
        search_text="*", select=[ID_FIELD, SOURCE_FIELD], include_total_count=True
    )
    total = rows.get_count() if hasattr(rows, "get_count") else None
    listed: list[tuple[str, str]] = []
    seen = 0
    for row in rows:
        seen += 1
        doc_id, source = row.get(ID_FIELD), row.get(SOURCE_FIELD)
        if isinstance(doc_id, str) and isinstance(source, str) and source:
            listed.append((doc_id, source))
    return listed, total, seen


async def prune_stale(
    search_client: Any,
    *,
    live_ids: set[str],
    timeout_seconds: float = DEFAULT_RETRIEVAL_TIMEOUT_SECONDS,
    batch_size: int = DELETE_BATCH_SIZE,
    force: bool = False,
) -> tuple[int, int]:
    """Remove the chunks of this index that the run did not produce — those of a file that is no
    longer in the corpus, and the tail of one that got shorter. Selected by ``source`` — only a
    chunk whose source starts with ``docs/`` is ever removed — and by id. Refuses (``PruneRefused``)
    to remove at least ``PRUNE_BRAKE_MIN`` chunks that are more than ``PRUNE_BRAKE_FRACTION`` of
    the index's own, unless ``force``. Returns ``(removed, failed)``.

    Call it only after a run in which every upload succeeded: a failed run removes nothing.
    """
    log = get_logger("app.ai.ingest")
    listed, total, seen = await _bounded(
        "listing the index", timeout_seconds, asyncio.to_thread(_list_index, search_client)
    )
    if total is not None and seen < total:
        # The listing stopped early (a paging limit): safe, it can only remove less, but say so.
        log.warning("ingest index listing incomplete", listed=seen, indexed=total)
    ours = [doc_id for doc_id, source in listed if source.startswith(f"{SOURCE_PREFIX}/")]
    stale = [doc_id for doc_id in ours if doc_id not in live_ids]
    if (
        not force
        and len(stale) >= PRUNE_BRAKE_MIN
        and len(stale) > len(ours) * PRUNE_BRAKE_FRACTION
    ):
        raise PruneRefused(len(stale), len(ours))
    removed = failed = 0
    for batch in _batches(stale, batch_size):
        results = await _bounded(
            "removing stale chunks",
            timeout_seconds,
            asyncio.to_thread(
                search_client.delete_documents, documents=[{ID_FIELD: doc_id} for doc_id in batch]
            ),
        )
        ok = sum(1 for r in results if getattr(r, "succeeded", False))
        removed += ok
        failed += len(batch) - ok
    log.info("ingest stale chunks removed", listed=len(listed), removed=removed, failed=failed)
    return removed, failed


def _ensure_index_blocking(settings: AISettings, index: Any) -> None:
    """The index calls are blocking network I/O: ``ingest_corpus`` runs this in a thread."""
    ensure_index(build_index_client(settings), index)


async def ingest_corpus(
    *,
    corpus: Path | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
    batch_size: int = 64,
    force_prune: bool = False,
    no_prune: bool = False,
) -> IngestReport:
    """One whole run: scan the corpus, check the embedding size, ensure the index, upload, prune.

    What the command line (``run``) and ``POST /v1/admin/ingest`` (``app.ai.ingest_job``) both call.
    Raises ``IngestError`` / ``AINotConfigured`` before it changes anything it should not.
    """
    from app.ai.client import get_embeddings

    started = time.perf_counter()
    log = get_logger("app.ai.ingest")
    settings = get_ai_settings()
    settings.require_search()
    settings.require_embeddings()
    # Off the event loop: walking and cleaning a cap's worth of files would stall every request.
    scan = await asyncio.to_thread(
        scan_corpus,
        corpus or resolve_corpus_dir(settings),
        max_files=settings.ingest_max_files,
        max_bytes=settings.ingest_max_bytes,
    )
    if not scan.documents:
        raise CorpusEmpty("the corpus directory holds no .md file")
    if not any(chunk_text(d.text, chunk_size, overlap) for d in scan.documents):
        raise CorpusEmpty(
            "no document has any text left after cleaning; nothing was uploaded or removed"
        )
    log.info(
        "ingest files read",
        documents=len(scan.documents),
        skipped_outside_root=scan.skipped_outside_root,
        skipped_unsafe=scan.skipped_unsafe,
        skipped_unresolved=scan.skipped_unresolved,
        cleaned=scan.cleaned,
    )

    timeout = settings.retrieval_timeout_seconds
    embeddings = get_embeddings()
    # Before the index is touched: a wrong dimension must not create a wrong index.
    await check_embedding_dimensions(
        embeddings, settings.embedding_dimensions, timeout_seconds=timeout
    )
    index = build_index(settings.search_index or "", settings.embedding_dimensions)
    await asyncio.to_thread(_ensure_index_blocking, settings, index)
    search_client = build_search_client(settings)
    report = await ingest_documents(
        scan.documents,
        embeddings=embeddings,
        search_client=search_client,
        chunk_size=chunk_size,
        overlap=overlap,
        batch_size=batch_size,
        expected_dimensions=settings.embedding_dimensions,
        timeout_seconds=timeout,
    )
    report.skipped_outside_root = scan.skipped_outside_root
    report.skipped_unsafe = scan.skipped_unsafe
    report.skipped_unresolved = scan.skipped_unresolved
    report.cleaned = scan.cleaned
    if no_prune:
        log.info("ingest stale chunks kept", reason="--no-prune")
    elif report.failed:
        log.warning("ingest stale chunks kept", reason="some uploads failed")
    elif scan.skipped_unresolved:
        # An unresolved file is not in the live set, though it may be a good document: keep chunks.
        log.warning("ingest stale chunks kept", reason="some files could not be resolved")
    else:
        try:
            report.removed, report.removal_failed = await prune_stale(
                search_client,
                live_ids=report.live_ids,
                timeout_seconds=timeout,
                force=force_prune,
            )
        except PruneRefused as refused:
            log.warning(
                "ingest stale chunks kept",
                reason="too many",
                stale=refused.stale,
                total=refused.total,
            )
            report.removal_refused = True
            report.removal_refusal = str(refused)
    report.seconds = round(time.perf_counter() - started, 2)
    return report


async def run(argv: Sequence[str] | None = None) -> IngestReport:
    parser = argparse.ArgumentParser(prog="python -m app.ai.ingest", description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="the corpus directory (default: CORPUS_DIR, else the repository's docs/)",
    )
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)
    parser.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument(
        "--force-prune",
        action="store_true",
        help="remove stale chunks even when that would remove most of the index",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="upload only: do not remove chunks of files that are no longer in the corpus",
    )
    args = parser.parse_args(argv)
    report = await ingest_corpus(
        corpus=args.corpus,
        chunk_size=args.chunk_size,
        overlap=args.overlap,
        batch_size=args.batch_size,
        force_prune=args.force_prune,
        no_prune=args.no_prune,
    )
    if report.removal_refusal:
        print(f"ingest: {report.removal_refusal}", file=sys.stderr)
    return report


def main() -> None:
    configure_logging()
    try:
        report = asyncio.run(run(sys.argv[1:]))
    except (IngestError, AINotConfigured) as exc:
        print(f"ingest stopped: {exc}", file=sys.stderr)
        sys.exit(2)
    print(
        f"documents={report.documents} chunks={report.chunks} "
        f"uploaded={report.uploaded} failed={report.failed} "
        f"skipped_outside_root={report.skipped_outside_root} "
        f"skipped_unsafe={report.skipped_unsafe} "
        f"skipped_unresolved={report.skipped_unresolved} cleaned={report.cleaned} "
        f"removed={report.removed} removal_failed={report.removal_failed} "
        f"removal_refused={int(report.removal_refused)} seconds={report.seconds}"
    )
    if report.failed or report.removal_failed or report.removal_refused:
        sys.exit(1)


if __name__ == "__main__":
    main()
