"""RAG ingestion job (ADR-0009): load → chunk → embed → upload to Azure AI Search.

Owns the **index definition** (``build_index``) so the retriever and the index never drift:
fields ``id`` (key), ``content`` (searchable), ``source`` (filterable, the ``docs/…`` path),
``title`` (searchable, the document's first ``# `` heading or else its file name),
``chunk_index``, and ``content_vector`` (HNSW vector profile, ``AZURE_AI_EMBEDDING_DIMENSIONS``
wide). ``ensure_index`` is idempotent, and it **refuses** to touch an index whose vector size
differs from the configured one — it never reuses or recreates it silently.

The corpus is every ``.md`` file under one directory (decision 14): ``CORPUS_DIR`` when set
(``/app/corpus`` in the image, ADR-0013), else the repository's ``docs/``. A document's
``source`` is always ``docs/<path inside that directory>``, so citations read the same locally
and in the container.

Run it from ``apps/api`` (needs the AI + search settings; the api identity or a dev key with
*Search Index Data Contributor* + *Search Service Contributor*)::

    uv run python -m app.ai.ingest                      # the corpus directory
    uv run python -m app.ai.ingest --corpus ../../docs --chunk-size 800 --overlap 80

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
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from azure.core.exceptions import ResourceNotFoundError
from langchain_core.embeddings import Embeddings

from app.ai.config import AINotConfigured, AISettings, get_ai_settings
from app.ai.tools.retrieve import (
    CONTENT_FIELD,
    ID_FIELD,
    SOURCE_FIELD,
    TITLE_FIELD,
    VECTOR_FIELD,
    build_search_client,
    build_search_credential,
)
from app.logging_config import configure_logging, get_logger

CHUNK_INDEX_FIELD = "chunk_index"
DEFAULT_CHUNK_SIZE = 1000
DEFAULT_OVERLAP = 100
VECTOR_PROFILE = "vector-profile"
HNSW_CONFIG = "hnsw"
MARKDOWN_SUFFIX = ".md"
SOURCE_PREFIX = "docs"
MAX_TITLE_LENGTH = 200
# A constant string: the dimension check never sends document text.
DIMENSION_PROBE = "dimension check"


class IngestError(RuntimeError):
    """Ingestion stopped before changing anything it should not have; the message says why."""


class CorpusNotFound(IngestError):
    """The corpus directory does not exist."""


class CorpusEmpty(IngestError):
    """The corpus directory holds no ``.md`` file."""


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
    documents: int = 0
    chunks: int = 0
    uploaded: int = 0
    failed: int = 0
    seconds: float = 0.0


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
    return SearchIndexClient(endpoint=s.search_endpoint, credential=build_search_credential(s))


def vector_dimensions(index: Any) -> int | None:
    """The vector size of an index's ``content_vector`` field, if it has one."""
    for field in index.fields:
        if field.name == VECTOR_FIELD:
            return field.vector_search_dimensions
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


async def check_embedding_dimensions(embeddings: Embeddings, expected: int) -> None:
    """Embed a constant probe and compare its size, before the index is touched."""
    vector = await embeddings.aembed_query(DIMENSION_PROBE)
    if len(vector) != expected:
        raise EmbeddingDimensionMismatch(len(vector), expected)


def default_corpus_dir() -> Path:
    """The repository's ``docs/`` (found from this file), else ``./docs``."""
    try:
        candidate = Path(__file__).resolve().parents[4] / "docs"
    except IndexError:  # the installed image has a shallower tree
        return Path("docs")
    return candidate if candidate.is_dir() else Path("docs")


def resolve_corpus_dir(settings: AISettings | None = None) -> Path:
    s = settings or get_ai_settings()
    return Path(s.corpus_dir) if s.corpus_dir else default_corpus_dir()


def load_corpus(root: Path) -> list[IngestDocument]:
    """Every ``.md`` file under ``root`` as a document, sorted by path.

    ``source`` is ``docs/<path inside root>`` (``/`` separators), whatever ``root`` is, so a
    citation reads the same on a laptop and in the image.
    """
    if not root.is_dir():
        raise CorpusNotFound(f"the corpus directory {root} does not exist")
    docs: list[IngestDocument] = []
    # Sorted on the posix path, not the Path: Windows compares paths case-insensitively.
    for file in sorted(
        (p for p in root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(root).as_posix()
    ):
        if file.suffix.lower() != MARKDOWN_SUFFIX:
            continue
        relative = file.relative_to(root).as_posix()
        try:
            # utf-8-sig drops a byte-order mark; invalid bytes fail the run, never become U+FFFD.
            text = file.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            raise CorpusUnreadable(f"{relative} is not valid UTF-8 text") from exc
        source = f"{SOURCE_PREFIX}/{relative}"
        docs.append(IngestDocument(source=source, text=text, title=extract_title(text, file.name)))
    return docs


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
) -> IngestReport:
    """Chunk, embed and upload. Returns counts and seconds only.

    With ``expected_dimensions`` set, a batch whose vectors have another size stops the run
    before it is uploaded.
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
    for batch in _batches(records, batch_size):
        vectors = await embeddings.aembed_documents([r[CONTENT_FIELD] for r in batch])
        if expected_dimensions is not None:
            for vector in vectors:
                if len(vector) != expected_dimensions:
                    raise EmbeddingDimensionMismatch(len(vector), expected_dimensions)
        payload = [{**r, VECTOR_FIELD: v} for r, v in zip(batch, vectors, strict=True)]
        results = await asyncio.to_thread(search_client.upload_documents, documents=payload)
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
    args = parser.parse_args(argv)

    from app.ai.client import get_embeddings

    settings = get_ai_settings()
    settings.require_search()
    settings.require_embeddings()
    documents = load_corpus(args.corpus or resolve_corpus_dir(settings))
    if not documents:
        raise CorpusEmpty("the corpus directory holds no .md file")

    embeddings = get_embeddings()
    # Before the index is touched: a wrong dimension must not create a wrong index.
    await check_embedding_dimensions(embeddings, settings.embedding_dimensions)
    index = build_index(settings.search_index or "", settings.embedding_dimensions)
    ensure_index(build_index_client(settings), index)
    return await ingest_documents(
        documents,
        embeddings=embeddings,
        search_client=build_search_client(settings),
        chunk_size=args.chunk_size,
        overlap=args.overlap,
        batch_size=args.batch_size,
        expected_dimensions=settings.embedding_dimensions,
    )


def main() -> None:
    configure_logging()
    try:
        report = asyncio.run(run(sys.argv[1:]))
    except (IngestError, AINotConfigured) as exc:
        print(f"ingest stopped: {exc}", file=sys.stderr)
        sys.exit(2)
    print(
        f"documents={report.documents} chunks={report.chunks} "
        f"uploaded={report.uploaded} failed={report.failed} seconds={report.seconds}"
    )
    if report.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
