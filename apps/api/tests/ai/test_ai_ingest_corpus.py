"""Ingestion of the docs corpus (api 3.2): titles, the corpus root, `.md` only, and the guards on
the vector dimension. Everything runs against fakes — no Azure, no network."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import structlog

import app.ai.client as client_mod
from app.ai import ingest
from app.ai.config import AINotConfigured, get_ai_settings
from app.ai.tools.retrieve import (
    CONTENT_FIELD,
    SOURCE_FIELD,
    TITLE_FIELD,
    VECTOR_FIELD,
)
from tests.ai.fakes import FakeEmbeddings, FakeIndexClient, FakeSearchClient

# ── titles ───────────────────────────────────────────────────────────────────


class TestExtractTitle:
    def test_first_top_level_heading(self) -> None:
        text = "intro\n\n# HTTP API reference\n\n## Errors\n# Second\n"
        assert ingest.extract_title(text, "x.md") == "HTTP API reference"

    def test_falls_back_to_the_file_name(self) -> None:
        assert ingest.extract_title("no heading here\n## only a sub-heading\n", "notes.md") == (
            "notes.md"
        )
        assert ingest.extract_title("", "empty.md") == "empty.md"

    def test_ignores_hash_lines_inside_code_fences(self) -> None:
        text = (
            "```bash\n# install the tools\nmake setup\n```\n\n"
            "~~~\n# also not a title\n~~~\n# Real\n"
        )
        assert ingest.extract_title(text, "x.md") == "Real"

    def test_a_fence_of_the_other_kind_does_not_close_it(self) -> None:
        text = "```\n~~~\n# still inside the backtick fence\n```\n# After\n"
        assert ingest.extract_title(text, "x.md") == "After"

    def test_skips_front_matter(self) -> None:
        text = "---\n# not a title: a yaml comment\ntitle: x\n---\n# The Title\n"
        assert ingest.extract_title(text, "x.md") == "The Title"

    def test_closing_hashes_are_trimmed_but_a_trailing_hash_in_a_word_is_kept(self) -> None:
        assert ingest.extract_title("# Overview ##\n", "x.md") == "Overview"
        assert ingest.extract_title("# Learning C#\n", "x.md") == "Learning C#"

    @pytest.mark.parametrize(
        "line",
        [
            "#NoSpace",  # not a heading in markdown
            "    # indented four spaces is code",
            "#",
            "#   ",
            "Title\n=====",  # setext headings are not read
        ],
    )
    def test_things_that_are_not_headings(self, line: str) -> None:
        assert ingest.extract_title(f"{line}\n", "fallback.md") == "fallback.md"

    def test_up_to_three_leading_spaces_still_count(self) -> None:
        assert ingest.extract_title("   # Indented a little\n", "x.md") == "Indented a little"

    def test_control_characters_become_spaces_not_nothing(self) -> None:
        assert ingest.extract_title("# Tab\there\x00now\n", "x.md") == "Tab here now"
        assert ingest.extract_title("# a\u200bb\n", "x.md") == "a b"

    def test_a_byte_order_mark_does_not_hide_the_heading_or_the_front_matter(self) -> None:
        assert ingest.extract_title("\ufeff# Bom\n", "x.md") == "Bom"
        assert ingest.extract_title("\ufeff---\n# yaml\n---\n# After\n", "x.md") == "After"

    def test_a_longer_outer_fence_is_not_closed_by_a_shorter_inner_one(self) -> None:
        text = "````md\n```\n# inside the outer fence\n```\n````\n# Real title\n"
        assert ingest.extract_title(text, "x.md") == "Real title"

    def test_a_closing_fence_cannot_carry_an_info_string(self) -> None:
        # "```bash" inside a ``` fence is content, not a close, so the next # is still code
        text = "```\n```bash\n# still code\n```\n# Real\n"
        assert ingest.extract_title(text, "x.md") == "Real"

    def test_the_file_name_fallback_is_cleaned_and_bounded_too(self) -> None:
        assert ingest.extract_title("no heading", "a\nb\x00c.md") == "a b c.md"
        assert len(ingest.extract_title("", "n" * 500 + ".md")) == ingest.MAX_TITLE_LENGTH
        assert ingest.extract_title("", "\x00\n") == "untitled"

    def test_title_is_single_line_printable_and_bounded(self) -> None:
        title = ingest.extract_title("# Tab\there\x00 ​X " + "y" * 400 + "\n", "x.md")
        assert len(title) <= ingest.MAX_TITLE_LENGTH
        assert all(ch.isprintable() for ch in title)
        assert "\x00" not in title


# ── the corpus ───────────────────────────────────────────────────────────────


def _write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TestLoadCorpus:
    def test_reads_markdown_only_recursively_with_docs_relative_sources(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "b.md", "# Bee\ntext")
        _write(tmp_path, "sub/deep/a.md", "# Ay\ntext")
        _write(tmp_path, "notes.txt", "not corpus")
        _write(tmp_path, "image.png", "binary-ish")
        _write(tmp_path, "UPPER.MD", "no heading")
        docs = ingest.load_corpus(tmp_path)
        assert [d.source for d in docs] == ["docs/UPPER.MD", "docs/b.md", "docs/sub/deep/a.md"]
        assert [d.title for d in docs] == ["UPPER.MD", "Bee", "Ay"]
        assert all("\\" not in d.source for d in docs)

    def test_source_does_not_depend_on_where_the_corpus_lives(self, tmp_path: Path) -> None:
        for root in (tmp_path / "docs", tmp_path / "app" / "corpus"):
            _write(root, "x/y.md", "# Y")
            assert ingest.load_corpus(root)[0].source == "docs/x/y.md"

    def test_missing_directory_is_a_clear_error(self, tmp_path: Path) -> None:
        with pytest.raises(ingest.CorpusNotFound, match="does not exist"):
            ingest.load_corpus(tmp_path / "nope")

    def test_a_file_is_not_a_corpus(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", "x")
        with pytest.raises(ingest.CorpusNotFound):
            ingest.load_corpus(tmp_path / "a.md")

    def test_a_file_that_is_not_utf8_fails_the_run_naming_the_file_only(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "bad.md").write_bytes(b"# Ok title\n\xff\xfe broken SECRET-BYTES")
        with pytest.raises(ingest.CorpusUnreadable) as caught:
            ingest.load_corpus(tmp_path)
        assert str(caught.value) == "sub/bad.md is not valid UTF-8 text"
        assert "SECRET-BYTES" not in str(caught.value)

    def test_a_byte_order_mark_is_dropped_from_text_and_title(self, tmp_path: Path) -> None:
        (tmp_path / "bom.md").write_bytes("\ufeff# Title with BOM\nbody".encode())
        doc = ingest.load_corpus(tmp_path)[0]
        assert doc.title == "Title with BOM" and not doc.text.startswith("\ufeff")

    def test_an_empty_directory_loads_nothing(self, tmp_path: Path) -> None:
        assert ingest.load_corpus(tmp_path) == []


class TestCorpusDir:
    def test_setting_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORPUS_DIR", "/app/corpus")
        assert ingest.resolve_corpus_dir(get_ai_settings()) == Path("/app/corpus")

    def test_default_is_the_repository_docs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CORPUS_DIR", raising=False)
        resolved = ingest.resolve_corpus_dir(get_ai_settings())
        assert resolved.name == "docs" and resolved.is_dir()
        assert (resolved / "architecture").is_dir()  # the real repo docs, not a stray ./docs

    def test_blank_setting_means_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CORPUS_DIR", "   ")
        assert get_ai_settings().corpus_dir is None


# ── chunks carry the title ───────────────────────────────────────────────────


def test_every_chunk_carries_source_and_title() -> None:
    client = FakeSearchClient()
    docs = [
        ingest.IngestDocument("docs/a.md", "x " * 400, title="Alpha"),
        ingest.IngestDocument("docs/b.md", "short", title=""),
    ]
    report = asyncio.run(
        ingest.ingest_documents(
            docs,
            embeddings=FakeEmbeddings(dimensions=2),
            search_client=client,
            chunk_size=100,
            overlap=10,
        )
    )
    records = [r for batch in client.uploaded for r in batch]
    assert len(records) == report.chunks > 2
    by_source = {r[SOURCE_FIELD]: r[TITLE_FIELD] for r in records}
    assert by_source == {"docs/a.md": "Alpha", "docs/b.md": "b.md"}
    assert all(r[TITLE_FIELD] for r in records)


def test_re_ingesting_overwrites_the_same_chunk_ids() -> None:
    docs = [ingest.IngestDocument("docs/a.md", "same text", title="A")]
    first, second = FakeSearchClient(), FakeSearchClient()
    for client in (first, second):
        asyncio.run(
            ingest.ingest_documents(docs, embeddings=FakeEmbeddings(2), search_client=client)
        )
    ids = lambda c: [r["id"] for batch in c.uploaded for r in batch]  # noqa: E731
    assert ids(first) == ids(second) and len(ids(first)) == 1


# ── dimension guards ─────────────────────────────────────────────────────────


def _index(dimensions: int | None) -> Any:
    """A real ``SearchIndex`` of the right shape, or one with no vector field."""
    index = ingest.build_index("team-assistant-docs", dimensions or 4)
    if dimensions is None:
        index.fields = [f for f in index.fields if f.name != VECTOR_FIELD]
    return index


class TestEnsureIndex:
    def test_creates_a_missing_index(self) -> None:
        client = FakeIndexClient(existing=None)
        wanted = _index(3072)
        ingest.ensure_index(client, wanted)
        assert client.get_calls == ["team-assistant-docs"] and client.created == [wanted]

    def test_updates_an_index_of_the_same_dimension(self) -> None:
        client = FakeIndexClient(existing=_index(3072))
        ingest.ensure_index(client, _index(3072))
        assert len(client.created) == 1

    def test_stops_on_another_dimension_naming_both_and_changes_nothing(self) -> None:
        client = FakeIndexClient(existing=_index(1536))
        with pytest.raises(ingest.IndexDimensionMismatch) as caught:
            ingest.ensure_index(client, _index(3072))
        message = str(caught.value)
        assert "1536" in message and "3072" in message
        assert "team-assistant-docs" in message
        assert (caught.value.actual, caught.value.expected) == (1536, 3072)
        assert client.created == [], "an index with another dimension is never updated"

    def test_an_existing_index_without_a_vector_field_is_refused_and_untouched(self) -> None:
        client = FakeIndexClient(existing=_index(None))
        with pytest.raises(ingest.IndexHasNoVectorField, match="team-assistant-docs"):
            ingest.ensure_index(client, _index(3072))
        assert client.created == [] and client.deleted == []

    @pytest.mark.parametrize("existing", [_index(1536), _index(3072), _index(None), None])
    def test_the_job_never_deletes_an_index(self, existing: Any) -> None:
        client = FakeIndexClient(existing=existing)
        try:
            ingest.ensure_index(client, _index(3072))
        except ingest.IngestError:
            pass
        assert client.deleted == []


class TestEmbeddingDimension:
    def test_probe_accepts_the_configured_size(self) -> None:
        embeddings = FakeEmbeddings(dimensions=3072)
        asyncio.run(ingest.check_embedding_dimensions(embeddings, 3072))
        assert embeddings.queries == [ingest.DIMENSION_PROBE]

    def test_probe_refuses_another_size_naming_both(self) -> None:
        with pytest.raises(ingest.EmbeddingDimensionMismatch) as caught:
            asyncio.run(ingest.check_embedding_dimensions(FakeEmbeddings(dimensions=3072), 1536))
        assert "3072" in str(caught.value) and "1536" in str(caught.value)

    def test_a_batch_of_the_wrong_size_is_not_uploaded(self) -> None:
        client = FakeSearchClient()
        with pytest.raises(ingest.EmbeddingDimensionMismatch):
            asyncio.run(
                ingest.ingest_documents(
                    [ingest.IngestDocument("docs/a.md", "text", title="A")],
                    embeddings=FakeEmbeddings(dimensions=4),
                    search_client=client,
                    expected_dimensions=8,
                )
            )
        assert client.uploaded == []


# ── the whole run, in order ──────────────────────────────────────────────────


class _Recorder:
    """Shares one ordered event list between the fakes."""

    def __init__(self) -> None:
        self.events: list[str] = []


class _OrderedEmbeddings(FakeEmbeddings):
    def __init__(self, dimensions: int, rec: _Recorder) -> None:
        super().__init__(dimensions)
        self._rec = rec

    def embed_query(self, text: str) -> list[float]:
        self._rec.events.append("probe")
        return super().embed_query(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._rec.events.append("embed")
        return super().embed_documents(texts)


class _OrderedIndex(FakeIndexClient):
    def __init__(self, rec: _Recorder, existing: Any | None = None) -> None:
        super().__init__(existing)
        self._rec = rec

    def get_index(self, name: str) -> Any:
        self._rec.events.append("get_index")
        return super().get_index(name)

    def create_or_update_index(self, index: Any) -> Any:
        self._rec.events.append("create_index")
        return super().create_or_update_index(index)


class _OrderedSearch(FakeSearchClient):
    def __init__(self, rec: _Recorder) -> None:
        super().__init__()
        self._rec = rec

    def upload_documents(self, documents: list[dict[str, Any]]) -> list[Any]:
        self._rec.events.append("upload")
        return super().upload_documents(documents)


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Settings for a run, a small corpus, and recording fakes behind the client seams."""
    for name in ("AZURE_AI_AUTH_MODE", "AZURE_AI_API_KEY", "AZURE_SEARCH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_AI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_AI_EMBEDDING_DEPLOYMENT", "embed")
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.invalid")
    monkeypatch.setenv("AZURE_SEARCH_INDEX", "team-assistant-docs")
    monkeypatch.setenv("AZURE_AI_EMBEDDING_DIMENSIONS", "8")
    corpus = tmp_path / "corpus"
    _write(corpus, "a.md", "# Alpha\n\nSECRET-DOCUMENT-TEXT alpha body")
    _write(corpus, "b/c.md", "# Gamma\n\nSECRET-DOCUMENT-TEXT gamma body")
    monkeypatch.setenv("CORPUS_DIR", str(corpus))

    class World:
        rec = _Recorder()
        embeddings = _OrderedEmbeddings(8, rec)
        index = _OrderedIndex(rec)
        search = _OrderedSearch(rec)

    monkeypatch.setattr(client_mod, "get_embeddings", lambda: World.embeddings)
    monkeypatch.setattr(ingest, "build_index_client", lambda settings=None: World.index)
    monkeypatch.setattr(ingest, "build_search_client", lambda settings=None, **kw: World.search)
    return World, corpus


def test_run_probes_then_ensures_the_index_then_uploads(wired) -> None:
    world, _ = wired
    report = asyncio.run(ingest.run([]))
    assert world.rec.events[:3] == ["probe", "get_index", "create_index"]
    assert "upload" in world.rec.events and world.rec.events.index("upload") > 2
    assert report.documents == 2 and report.uploaded == report.chunks == 2 and report.failed == 0
    uploaded = [r for batch in world.search.uploaded for r in batch]
    assert {r[SOURCE_FIELD]: r[TITLE_FIELD] for r in uploaded} == {
        "docs/a.md": "Alpha",
        "docs/b/c.md": "Gamma",
    }
    assert all(len(r[VECTOR_FIELD]) == 8 for r in uploaded)


def test_run_with_the_wrong_embedding_size_never_touches_the_index(wired) -> None:
    world, _ = wired
    world.embeddings.dimensions = 16  # the deployment returns 16, the setting says 8
    with pytest.raises(ingest.EmbeddingDimensionMismatch, match="16.*8"):
        asyncio.run(ingest.run([]))
    assert world.rec.events == ["probe"]
    assert world.index.get_calls == [] and world.search.uploaded == []


def test_run_against_an_index_of_another_size_uploads_nothing(wired) -> None:
    world, _ = wired
    world.index.existing = _index(3072)
    with pytest.raises(ingest.IndexDimensionMismatch, match="3072.*8"):
        asyncio.run(ingest.run([]))
    assert world.index.created == [] and world.search.uploaded == []


def test_run_with_an_empty_corpus_stops_before_any_call(wired, tmp_path: Path) -> None:
    world, _ = wired
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ingest.CorpusEmpty):
        asyncio.run(ingest.run(["--corpus", str(empty)]))
    assert world.rec.events == []


def test_run_with_a_missing_corpus_stops_before_any_call(wired, tmp_path: Path) -> None:
    world, _ = wired
    with pytest.raises(ingest.CorpusNotFound):
        asyncio.run(ingest.run(["--corpus", str(tmp_path / "nowhere")]))
    assert world.rec.events == []


def test_run_is_idempotent(wired) -> None:
    world, _ = wired
    asyncio.run(ingest.run([]))
    first = [r["id"] for batch in world.search.uploaded for r in batch]
    world.search.uploaded.clear()
    asyncio.run(ingest.run([]))
    second = [r["id"] for batch in world.search.uploaded for r in batch]
    assert sorted(first) == sorted(second)


def test_the_corpus_flag_overrides_the_setting(wired, tmp_path: Path) -> None:
    world, _ = wired
    other = tmp_path / "other"
    _write(other, "only.md", "# Only one")
    report = asyncio.run(ingest.run(["--corpus", str(other)]))
    assert report.documents == 1
    assert world.search.uploaded[0][0][SOURCE_FIELD] == "docs/only.md"


def test_logs_carry_counts_and_seconds_never_document_text(wired) -> None:
    _, corpus = wired
    with structlog.testing.capture_logs() as logs:
        asyncio.run(ingest.run([]))
    done = next(e for e in logs if e["event"] == "ingest complete")
    assert done["documents"] == 2 and done["chunks"] == 2 and done["failed"] == 0
    assert isinstance(done["seconds"], float) and done["seconds"] >= 0
    rendered = repr(logs)
    assert "SECRET-DOCUMENT-TEXT" not in rendered
    assert "Alpha" not in rendered and "docs/a.md" not in rendered
    assert str(corpus) not in rendered and corpus.name not in rendered, "no paths in logs"


def test_the_report_has_seconds(wired) -> None:
    assert asyncio.run(ingest.run([])).seconds >= 0


# ── the command line ─────────────────────────────────────────────────────────


def test_main_prints_a_one_line_reason_and_exits_2_on_an_ingest_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def boom(argv=None):
        raise ingest.IndexDimensionMismatch("team-assistant-docs", 1536, 3072)

    monkeypatch.setattr(ingest, "run", boom)
    monkeypatch.setattr(ingest, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["ingest"])
    with pytest.raises(SystemExit) as caught:
        ingest.main()
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("ingest stopped:") and "1536" in err and "3072" in err
    assert "Traceback" not in err


def test_main_also_turns_a_missing_setting_into_a_one_line_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def not_configured(argv=None):
        raise AINotConfigured("AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_INDEX must be set")

    monkeypatch.setattr(ingest, "run", not_configured)
    monkeypatch.setattr(ingest, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["ingest"])
    with pytest.raises(SystemExit) as caught:
        ingest.main()
    assert caught.value.code == 2
    err = capsys.readouterr().err
    assert err.startswith("ingest stopped:") and "AZURE_SEARCH_ENDPOINT" in err
    assert "Traceback" not in err


def test_main_prints_counts_and_seconds_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fine(argv=None):
        return ingest.IngestReport(documents=2, chunks=5, uploaded=5, failed=0, seconds=1.25)

    monkeypatch.setattr(ingest, "run", fine)
    monkeypatch.setattr(ingest, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["ingest"])
    ingest.main()
    assert capsys.readouterr().out.strip() == (
        "documents=2 chunks=5 uploaded=5 failed=0 seconds=1.25"
    )


def test_content_field_is_unchanged_for_the_retriever() -> None:
    # the retriever and the index share these names; a rename here must be deliberate
    assert (CONTENT_FIELD, SOURCE_FIELD, TITLE_FIELD) == ("content", "source", "title")
