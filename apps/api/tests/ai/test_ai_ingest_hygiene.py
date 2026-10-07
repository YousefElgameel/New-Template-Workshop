"""Ingestion hygiene (api 3.5): what a corpus directory may contain and how it is cleaned, the
timeouts on every call, and the removal of stale chunks. Everything runs against fakes and a
temporary corpus — no Azure, no network."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest
import structlog

from app.ai import ingest
from app.ai.tools.retrieve import CONTENT_FIELD, ID_FIELD, SOURCE_FIELD
from tests.ai.fakes import FakeEmbeddings, FakeSearchClient

ZWSP = chr(0x200B)
RLO = chr(0x202E)
BOM = chr(0xFEFF)
LS = chr(0x2028)
TAG_A = chr(0xE0041)


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError, NotImplementedError:
        pytest.skip("this machine cannot create symlinks")


def _pretend_outside(monkeypatch: pytest.MonkeyPatch, file: Path, elsewhere: Path) -> None:
    """Make ``file`` resolve to ``elsewhere`` (outside the corpus), as a symlink would."""
    real_resolve = Path.resolve

    def resolve(self: Path, strict: bool = False) -> Path:
        return elsewhere if self == file else real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)


# ── paths: nothing outside the root is read ───────────────────────────────────────────────────


class TestPathsStayInsideTheRoot:
    def test_a_symlink_to_a_file_outside_is_skipped_counted_and_never_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        corpus = tmp_path / "corpus"
        _write(corpus, "ok.md", "# Fine\nbody")
        outside = _write(tmp_path / "elsewhere", "secret.md", "# Secret\nOUTSIDE-SECRET-TEXT")
        _symlink(corpus / "leak.md", outside)

        opened: list[Path] = []
        real_read_text = Path.read_text

        def recording(self: Path, *args: Any, **kwargs: Any) -> str:
            opened.append(self.resolve())
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", recording)
        load = ingest.scan_corpus(corpus)

        assert [d.source for d in load.documents] == ["docs/ok.md"]
        assert load.skipped_outside_root == 1
        assert outside.resolve() not in opened, "a file outside the root must never be opened"
        assert "OUTSIDE-SECRET-TEXT" not in repr(load)

    def test_a_path_that_resolves_outside_is_skipped_on_any_machine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same rule without symlink rights: the resolver says where a file really is."""
        corpus = tmp_path / "corpus"
        _write(corpus, "ok.md", "# Fine")
        leak = _write(corpus, "leak.md", "# Leak\nOUTSIDE-SECRET-TEXT")
        elsewhere = tmp_path / "elsewhere" / "leak.md"
        real_resolve = Path.resolve

        def resolve(self: Path, strict: bool = False) -> Path:
            return elsewhere if self == leak else real_resolve(self, strict=strict)

        opened: list[Path] = []
        real_read_text = Path.read_text

        def recording(self: Path, *args: Any, **kwargs: Any) -> str:
            opened.append(self)
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
        monkeypatch.setattr(Path, "read_text", recording)
        load = ingest.scan_corpus(corpus)
        assert [d.source for d in load.documents] == ["docs/ok.md"]
        assert load.skipped_outside_root == 1
        assert leak not in opened and elsewhere not in opened

    def test_a_resolver_error_is_counted_apart_and_the_file_is_never_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        corpus = tmp_path / "corpus"
        _write(corpus, "ok.md", "# Fine")
        loop = _write(corpus, "loop.md", "# Loop")
        real_resolve = Path.resolve

        def resolve(self: Path, strict: bool = False) -> Path:
            if self == loop:
                raise RuntimeError("symlink loop")
            return real_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve)
        load = ingest.scan_corpus(corpus)
        assert [d.source for d in load.documents] == ["docs/ok.md"]
        assert load.skipped_unresolved == 1 and load.skipped_outside_root == 0

    def test_a_windows_junction_to_a_directory_outside_is_not_walked(self, tmp_path: Path) -> None:
        if os.name != "nt":
            pytest.skip("junctions are a Windows feature")
        import subprocess

        corpus = tmp_path / "corpus"
        _write(corpus, "ok.md", "# Fine")
        _write(tmp_path / "elsewhere", "inner/secret.md", "# Secret\nOUTSIDE-SECRET-TEXT")
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(corpus / "linked"), str(tmp_path / "elsewhere")],
            capture_output=True,
        )
        if made.returncode != 0:
            pytest.skip("this machine cannot create junctions")
        load = ingest.scan_corpus(corpus)
        assert "OUTSIDE-SECRET-TEXT" not in repr(load)
        assert [d.source for d in load.documents] == ["docs/ok.md"]
        assert load.skipped_outside_root >= 1  # what the junction exposed, resolved outside

    def test_a_symlinked_directory_outside_is_not_walked(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write(corpus, "ok.md", "# Fine")
        _write(tmp_path / "elsewhere", "inner/secret.md", "# Secret\nOUTSIDE-SECRET-TEXT")
        _symlink(corpus / "linked", tmp_path / "elsewhere", directory=True)
        load = ingest.scan_corpus(corpus)
        assert [d.source for d in load.documents] == ["docs/ok.md"]
        assert "OUTSIDE-SECRET-TEXT" not in repr(load)

    def test_a_symlink_that_stays_inside_the_root_is_allowed_and_keeps_its_own_path(
        self, tmp_path: Path
    ) -> None:
        corpus = tmp_path / "corpus"
        _write(corpus, "real/a.md", "# Real")
        _symlink(corpus / "alias.md", corpus / "real" / "a.md")
        load = ingest.scan_corpus(corpus)
        assert [d.source for d in load.documents] == ["docs/alias.md", "docs/real/a.md"]
        assert load.skipped_outside_root == 0

    def test_a_broken_or_looping_symlink_is_ignored_without_failing_the_run(
        self, tmp_path: Path
    ) -> None:
        corpus = tmp_path / "corpus"
        _write(corpus, "ok.md", "# Fine")
        _symlink(corpus / "gone.md", tmp_path / "does-not-exist.md")
        _symlink(corpus / "loop.md", corpus / "loop.md")
        load = ingest.scan_corpus(corpus)
        assert [d.source for d in load.documents] == ["docs/ok.md"]

    def test_a_root_written_with_dotdot_still_gives_clean_sources(self, tmp_path: Path) -> None:
        corpus = tmp_path / "a" / "corpus"
        _write(corpus, "x.md", "# X")
        (tmp_path / "b").mkdir()
        roundabout = tmp_path / "b" / ".." / "a" / "corpus"
        load = ingest.scan_corpus(roundabout)
        assert [d.source for d in load.documents] == ["docs/x.md"]

    def test_only_markdown_is_read(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", "# A")
        _write(tmp_path, "b.txt", "TEXT-FILE")
        _write(tmp_path, "c.md.bak", "BACKUP")
        _write(tmp_path, "d.MD", "# D")
        sources = [d.source for d in ingest.scan_corpus(tmp_path).documents]
        assert sources == ["docs/a.md", "docs/d.MD"]


# ── sources: one printable, bounded, relative line ────────────────────────────────────────────


class TestSources:
    def test_clean_source_is_printable_one_line_and_bounded(self) -> None:
        assert ingest.clean_source("docs/a/b.md") == "docs/a/b.md"
        assert ingest.clean_source("docs/a\nb.md") == "docs/a b.md"
        assert ingest.clean_source(f"docs/a{RLO}b{ZWSP}.md") == "docs/a b .md"
        assert len(ingest.clean_source("docs/" + "x" * 1000)) == ingest.MAX_SOURCE_LENGTH

    def test_double_spaces_in_a_file_name_are_fine_and_control_characters_are_not(self) -> None:
        assert ingest.is_safe_source("docs/a  b.md")
        assert not ingest.is_safe_source("docs/a\nb.md")
        assert not ingest.is_safe_source("docs/a" + chr(0x202E) + "b.md")
        assert not ingest.is_safe_source("docs/a" + chr(0x200B) + "b.md")
        assert not ingest.is_safe_source("")
        assert not ingest.is_safe_source("docs/" + "x" * ingest.MAX_SOURCE_LENGTH)

    def test_a_file_name_that_is_not_one_printable_line_is_skipped_and_counted(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "ok.md", "# Fine")
        try:
            _write(tmp_path, f"sneaky{RLO}name.md", "# Sneaky\nbody")
        except OSError:
            pytest.skip("this file system rejects the file name")
        load = ingest.scan_corpus(tmp_path)
        assert [d.source for d in load.documents] == ["docs/ok.md"]
        assert load.skipped_unsafe == 1

    def test_a_source_over_the_limit_is_skipped_and_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ingest, "MAX_SOURCE_LENGTH", 12)
        _write(tmp_path, "a.md", "# A")  # docs/a.md: 8 characters
        _write(tmp_path, "a-very-long-name.md", "# Long")
        load = ingest.scan_corpus(tmp_path)
        assert [d.source for d in load.documents] == ["docs/a.md"]
        assert load.skipped_unsafe == 1

    def test_every_source_is_relative_slashed_and_one_line(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", "# A")
        _write(tmp_path, "sub/deep/b.md", "# B")
        _write(tmp_path, "with space/c.md", "# C")
        for doc in ingest.scan_corpus(tmp_path).documents:
            assert doc.source.startswith("docs/") and not doc.source.startswith("/")
            assert "\\" not in doc.source and "\n" not in doc.source and "\r" not in doc.source
            assert ".." not in Path(doc.source).parts
            assert not os.path.isabs(doc.source)
            assert ingest.clean_source(doc.source) == doc.source


class TestWhatElseIsSkipped:
    def test_a_link_to_a_file_that_is_not_markdown_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """notes.md that points at a non-.md file inside the root (say a .env) is not a document."""
        corpus = tmp_path / "corpus"
        _write(corpus, "ok.md", "# Fine")
        alias = _write(corpus, "notes.md", "# Alias")
        target = _write(corpus, "settings.env", "API_KEY=SECRET-VALUE")
        real_resolve = Path.resolve

        def resolve(self: Path, strict: bool = False) -> Path:
            return real_resolve(target) if self == alias else real_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve)
        load = ingest.scan_corpus(corpus)
        assert [d.source for d in load.documents] == ["docs/ok.md"]
        assert load.skipped_unsafe == 1
        assert "SECRET-VALUE" not in repr(load)

    def test_hidden_files_and_directories_are_skipped(self, tmp_path: Path) -> None:
        _write(tmp_path, "ok.md", "# Fine")
        _write(tmp_path, ".hidden.md", "# Hidden")
        _write(tmp_path, ".git/notes.md", "# Git")
        _write(tmp_path, "sub/.drafts/x.md", "# Draft")
        load = ingest.scan_corpus(tmp_path)
        assert [d.source for d in load.documents] == ["docs/ok.md"]
        assert load.skipped_unsafe == 3

    def test_a_hard_link_is_skipped(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        original = _write(corpus, "a.md", "# A")
        _write(corpus, "ok.md", "# Fine")
        try:
            os.link(original, corpus / "b.md")
        except OSError, NotImplementedError:
            pytest.skip("this machine cannot create hard links")
        load = ingest.scan_corpus(corpus)
        assert [d.source for d in load.documents] == ["docs/ok.md"]
        assert load.skipped_unsafe == 2  # both names of the linked file

    def test_a_file_over_the_size_limit_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ingest, "MAX_FILE_BYTES", 10)
        _write(tmp_path, "small.md", "# S")
        _write(tmp_path, "big.md", "# Big\n" + "x" * 100)
        load = ingest.scan_corpus(tmp_path)
        assert [d.source for d in load.documents] == ["docs/small.md"]
        assert load.skipped_unsafe == 1

    def test_a_file_that_cannot_be_read_fails_the_run_with_a_one_line_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        locked = _write(tmp_path, "locked.md", "# L")
        real_read_text = Path.read_text

        def read_text(self: Path, *args: Any, **kwargs: Any) -> str:
            if self.name == locked.name:
                raise PermissionError(f"denied: {self}")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)
        with pytest.raises(ingest.CorpusUnreadable) as caught:
            ingest.scan_corpus(tmp_path)
        assert str(caught.value) == "locked.md could not be read"

    def test_a_heading_line_with_a_huge_run_of_spaces_cannot_stall_the_title(self) -> None:
        started = time.perf_counter()
        title = ingest.extract_title("# a" + " " * 300_000 + "b\n", "fallback.md")
        assert title == "fallback.md"
        assert time.perf_counter() - started < 2


# ── text: control and format characters, comments ─────────────────────────────────────────────


class TestCleanText:
    def test_control_and_format_characters_are_removed(self) -> None:
        dirty = f"a\x00b{ZWSP}c{RLO}d{BOM}e{TAG_A}f\x07g"
        assert ingest.clean_text(dirty) == "abcdefg"

    def test_newlines_and_tabs_stay_and_line_separators_become_newlines(self) -> None:
        assert ingest.clean_text("a\n\tb") == "a\n\tb"
        assert ingest.clean_text(f"one{LS}two") == "one\ntwo"

    def test_variation_selectors_and_blank_letters_are_removed(self) -> None:
        smuggled = "a" + chr(0xFE0F) + "b" + chr(0xE0100) + "c" + chr(0x3164) + "d" + chr(0x2800)
        assert ingest.clean_text(smuggled) == "abcd"

    def test_vertical_tab_form_feed_and_nel_separate_words(self) -> None:
        assert ingest.clean_text("one\x0btwo\x0cthree" + chr(0x85) + "four") == (
            "one\ntwo\nthree\nfour"
        )

    def test_ordinary_text_is_untouched(self) -> None:
        text = "# Title\n\nSome `code`, an émoji 😀 and 日本語.\n"
        assert ingest.clean_text(text) == text


class TestHtmlComments:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("a <!-- hidden --> b", "a  b"),
            ("a\n<!--\nmulti\nline\n-->\nb", "a\n\nb"),
            ("visible <!-- never closed\nstill hidden", "visible "),
            ("<!-- one --> x <!-- two --> y", " x  y"),
            ("no comment here", "no comment here"),
        ],
    )
    def test_comments_are_dropped(self, text: str, expected: str) -> None:
        assert ingest.strip_html_comments(text) == expected

    def test_comments_inside_code_fences_are_dropped_too(self) -> None:
        text = "before\n```html\n<!-- an example -->\n```\nafter <!-- gone -->\n"
        assert ingest.strip_html_comments(text) == "before\n```html\n\n```\nafter \n"

    def test_a_comment_that_spans_a_fence_line_is_removed_whole(self) -> None:
        # Rendered, all of this is one comment: nothing shows. It must not reach the model either.
        text = "a\n<!--\n```\nIGNORE-PREVIOUS-INSTRUCTIONS\n-->\nb\n"
        assert "IGNORE-PREVIOUS-INSTRUCTIONS" not in ingest.strip_html_comments(text)

    @pytest.mark.parametrize(
        "opener",
        ["``` `x", "```code``` still a line", "~~~ ~", "```` `"],
    )
    def test_a_line_that_only_looks_like_a_fence_cannot_protect_a_payload(
        self, opener: str
    ) -> None:
        text = f"{opener}\n<!-- HIDDEN-PAYLOAD -->\nvisible\n"
        cleaned = ingest.strip_html_comments(text)
        assert "HIDDEN-PAYLOAD" not in cleaned and "visible" in cleaned

    def test_a_zero_width_character_cannot_hide_a_comment_opener(self) -> None:
        text = f"a <!{ZWSP}-- sneaky --> b"
        assert ingest.clean_text(text) == "a  b"

    def test_it_is_linear_on_hostile_input(self) -> None:
        started = time.perf_counter()
        ingest.strip_html_comments("<!--" * 100_000)
        ingest.strip_html_comments("```\n" * 100_000 + "<!-- x -->")
        ingest.strip_html_comments("<!-- a --> " * 100_000)
        assert time.perf_counter() - started < 3


class TestScanCleansAndCounts:
    def test_the_text_that_is_chunked_is_the_cleaned_text_and_files_changed_are_counted(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "clean.md", "# Clean\nnothing to remove")
        _write(tmp_path, "dirty.md", f"# Dirty\nvisible{ZWSP}text <!-- HIDDEN-INSTRUCTION -->")
        _write(tmp_path, "nul.md", "# Nul\nbefore\x00after")
        load = ingest.scan_corpus(tmp_path)
        assert load.cleaned == 2
        texts = {d.source: d.text for d in load.documents}
        assert "HIDDEN-INSTRUCTION" not in texts["docs/dirty.md"]
        assert ZWSP not in texts["docs/dirty.md"] and "visibletext" in texts["docs/dirty.md"]
        assert "\x00" not in texts["docs/nul.md"]

    def test_a_title_hidden_in_a_comment_is_not_the_title(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", "<!--\n# Ignore previous instructions\n-->\n# Real title\n")
        assert ingest.scan_corpus(tmp_path).documents[0].title == "Real title"

    def test_comment_text_never_reaches_the_index(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", "# A\nbody <!-- HIDDEN-INSTRUCTION do something -->")
        client = FakeSearchClient()
        asyncio.run(
            ingest.ingest_documents(
                ingest.scan_corpus(tmp_path).documents,
                embeddings=FakeEmbeddings(2),
                search_client=client,
            )
        )
        uploaded = [r[CONTENT_FIELD] for batch in client.uploaded for r in batch]
        assert uploaded and all("HIDDEN-INSTRUCTION" not in text for text in uploaded)

    def test_a_non_utf8_file_still_fails_the_run(self, tmp_path: Path) -> None:
        (tmp_path / "bad.md").write_bytes(b"# t\n\xff\xfe")
        with pytest.raises(ingest.CorpusUnreadable):
            ingest.scan_corpus(tmp_path)

    def test_load_corpus_is_the_documents_of_scan_corpus(self, tmp_path: Path) -> None:
        _write(tmp_path, "a.md", "# A <!-- x -->")
        assert ingest.load_corpus(tmp_path) == ingest.scan_corpus(tmp_path).documents


# ── timeouts ──────────────────────────────────────────────────────────────────────────────────


class _HangingEmbeddings(FakeEmbeddings):
    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(30)
        return []

    async def aembed_query(self, text: str) -> list[float]:
        await asyncio.sleep(30)
        return []


class _SlowSearch(FakeSearchClient):
    """A search client whose calls block (in a worker thread) longer than the test's budget."""

    def upload_documents(self, documents: list[dict[str, Any]]) -> list[Any]:
        time.sleep(0.4)
        return super().upload_documents(documents)

    def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        time.sleep(0.4)
        return super().search(**kwargs)

    def delete_documents(self, documents: list[dict[str, Any]]) -> list[Any]:
        time.sleep(0.4)
        return super().delete_documents(documents)


DOCS = [ingest.IngestDocument("docs/a.md", "some text", title="A")]


class TestTimeouts:
    def test_a_hung_embedding_call_stops_the_run_and_names_the_step(self) -> None:
        client = FakeSearchClient()
        started = time.perf_counter()
        with pytest.raises(ingest.IngestTimeout, match="embedding a batch") as caught:
            asyncio.run(
                ingest.ingest_documents(
                    DOCS,
                    embeddings=_HangingEmbeddings(2),
                    search_client=client,
                    timeout_seconds=0.05,
                )
            )
        assert time.perf_counter() - started < 5
        assert client.uploaded == []
        assert caught.value.seconds == 0.05

    def test_a_hung_probe_stops_the_run(self) -> None:
        with pytest.raises(ingest.IngestTimeout, match="dimension check"):
            asyncio.run(
                ingest.check_embedding_dimensions(_HangingEmbeddings(2), 2, timeout_seconds=0.05)
            )

    def test_a_hung_upload_stops_the_run(self) -> None:
        with pytest.raises(ingest.IngestTimeout, match="uploading a batch"):
            asyncio.run(
                ingest.ingest_documents(
                    DOCS,
                    embeddings=FakeEmbeddings(2),
                    search_client=_SlowSearch(),
                    timeout_seconds=0.05,
                )
            )

    def test_a_hung_listing_and_a_hung_removal_stop_the_pass(self) -> None:
        with pytest.raises(ingest.IngestTimeout, match="listing the index"):
            asyncio.run(ingest.prune_stale(_SlowSearch(), live_ids=set(), timeout_seconds=0.05))
        rows = [{ID_FIELD: "old", SOURCE_FIELD: "docs/old.md"}]
        slow_delete = _SlowSearch(rows)
        slow_delete.search = FakeSearchClient.search.__get__(slow_delete)  # type: ignore[method-assign]
        with pytest.raises(ingest.IngestTimeout, match="removing stale chunks"):
            asyncio.run(ingest.prune_stale(slow_delete, live_ids=set(), timeout_seconds=0.05))

    def test_the_message_does_not_promise_that_nothing_changed(self) -> None:
        message = str(ingest.IngestTimeout("x", 30))
        assert "may still finish" in message and "partly updated" in message

    def test_the_default_budget_is_the_configured_retrieval_timeout(self) -> None:
        assert ingest.DEFAULT_RETRIEVAL_TIMEOUT_SECONDS == 30.0

    def test_the_index_client_is_built_with_explicit_timeouts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import azure.search.documents.indexes as indexes

        seen: dict[str, Any] = {}

        class Capture:
            def __init__(self, **kwargs: Any) -> None:
                seen.update(kwargs)

        monkeypatch.setattr(indexes, "SearchIndexClient", Capture)
        monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.invalid")
        monkeypatch.setenv("AZURE_SEARCH_INDEX", "i")
        monkeypatch.setenv("AZURE_SEARCH_API_KEY", "dev-key")
        monkeypatch.setenv("AI_RETRIEVAL_TIMEOUT_SECONDS", "7")
        ingest.build_index_client()
        assert seen["connection_timeout"] == 7 and seen["read_timeout"] == 7
        assert seen["retry_total"] == 2


# ── stale chunks ──────────────────────────────────────────────────────────────────────────────


def _row(source: str, index: int = 0) -> dict[str, Any]:
    return {ID_FIELD: ingest.chunk_id(source, index), SOURCE_FIELD: source}


class TestPruneStale:
    def test_removes_chunks_that_the_run_did_not_produce_selected_by_source(self) -> None:
        live = {ingest.chunk_id("docs/a.md", 0)}
        client = FakeSearchClient(
            [
                _row("docs/a.md", 0),  # produced by this run
                _row("docs/a.md", 3),  # the tail of a file that got shorter
                _row("docs/gone.md", 0),  # a file that is no longer in the corpus
                {ID_FIELD: "foreign", SOURCE_FIELD: ""},  # no source: not ours
                {ID_FIELD: "foreign-2"},  # no source field at all
            ]
        )
        removed, failed = asyncio.run(ingest.prune_stale(client, live_ids=live))
        assert (removed, failed) == (2, 0)
        deleted = sorted(d[ID_FIELD] for batch in client.deleted for d in batch)
        assert deleted == sorted(
            [ingest.chunk_id("docs/a.md", 3), ingest.chunk_id("docs/gone.md", 0)]
        )

    def test_only_chunks_whose_source_starts_with_docs_are_ever_removed(self) -> None:
        client = FakeSearchClient(
            [
                {ID_FIELD: "other-1", SOURCE_FIELD: "other-use-case/readme.md"},
                {ID_FIELD: "other-2", SOURCE_FIELD: "/etc/passwd"},
                _row("docs/old.md"),
            ]
        )
        removed, _ = asyncio.run(ingest.prune_stale(client, live_ids=set()))
        assert removed == 1
        assert [d[ID_FIELD] for batch in client.deleted for d in batch] == [
            ingest.chunk_id("docs/old.md", 0)
        ]

    def test_refuses_to_remove_most_of_the_index_unless_forced(self) -> None:
        rows = [_row("docs/keep.md", i) for i in range(5)] + [
            _row("docs/gone.md", i) for i in range(25)
        ]
        live = {ingest.chunk_id("docs/keep.md", i) for i in range(5)}
        client = FakeSearchClient(rows)
        with pytest.raises(ingest.PruneRefused, match="wrong corpus directory") as caught:
            asyncio.run(ingest.prune_stale(client, live_ids=live))
        assert (caught.value.stale, caught.value.total) == (25, 30)
        assert client.deleted == []
        removed, _ = asyncio.run(ingest.prune_stale(client, live_ids=live, force=True))
        assert removed == 25

    def test_a_small_index_or_a_minority_is_not_braked(self) -> None:
        small = FakeSearchClient([_row("docs/gone.md", i) for i in range(5)])
        assert asyncio.run(ingest.prune_stale(small, live_ids=set()))[0] == 5
        rows = [_row("docs/keep.md", i) for i in range(40)] + [
            _row("docs/gone.md", i) for i in range(20)
        ]
        live = {ingest.chunk_id("docs/keep.md", i) for i in range(40)}
        assert asyncio.run(ingest.prune_stale(FakeSearchClient(rows), live_ids=live))[0] == 20

    def test_warns_with_counts_when_the_listing_is_shorter_than_the_index(self) -> None:
        class Paged(list):
            def get_count(self) -> int:
                return 120  # the service says it holds more than came back

        class Search(FakeSearchClient):
            def search(self, **kwargs: Any) -> list[dict[str, Any]]:
                self.search_calls.append(kwargs)
                return Paged([_row("docs/old.md")])

        with structlog.testing.capture_logs() as logs:
            removed, _ = asyncio.run(ingest.prune_stale(Search(), live_ids=set()))
        assert removed == 1  # what it did list is still handled
        warning = next(e for e in logs if e["event"] == "ingest index listing incomplete")
        assert warning["listed"] == 1 and warning["indexed"] == 120
        assert "docs/old.md" not in repr(logs)

    def test_asks_the_service_for_its_count(self) -> None:
        client = FakeSearchClient([])
        asyncio.run(ingest.prune_stale(client, live_ids=set()))
        assert client.search_calls[-1]["include_total_count"] is True

    def test_lists_only_the_id_and_the_source(self) -> None:
        client = FakeSearchClient([])
        asyncio.run(ingest.prune_stale(client, live_ids=set()))
        assert client.search_calls[-1]["select"] == [ID_FIELD, SOURCE_FIELD]
        assert client.deleted == []

    def test_removes_in_batches(self) -> None:
        client = FakeSearchClient([_row("docs/old.md", i) for i in range(5)])
        removed, _ = asyncio.run(ingest.prune_stale(client, live_ids=set(), batch_size=2))
        assert removed == 5
        assert [len(b) for b in client.deleted] == [2, 2, 1]

    def test_counts_removals_the_service_reports_as_failed(self) -> None:
        class Refusing(FakeSearchClient):
            def delete_documents(self, documents: list[dict[str, Any]]) -> list[Any]:
                class _R:
                    succeeded = False

                return [_R() for _ in documents]

        removed, failed = asyncio.run(
            ingest.prune_stale(Refusing([_row("docs/old.md")]), live_ids=set())
        )
        assert (removed, failed) == (0, 1)


# ── the whole run ────────────────────────────────────────────────────────────────────────────


def _run_ingest(corpus: Path, **env: str) -> Any:
    return asyncio.run(ingest.run(["--corpus", str(corpus)]))


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A small corpus, settings and fakes behind the client seams."""
    import app.ai.client as client_mod

    for name in ("AZURE_AI_AUTH_MODE", "AZURE_AI_API_KEY", "AZURE_SEARCH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AZURE_AI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_AI_EMBEDDING_DEPLOYMENT", "embed")
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://search.invalid")
    monkeypatch.setenv("AZURE_SEARCH_INDEX", "team-assistant-docs")
    monkeypatch.setenv("AZURE_AI_EMBEDDING_DIMENSIONS", "8")
    corpus = tmp_path / "corpus"
    _write(corpus, "a.md", "# Alpha\n\nalpha body")
    _write(corpus, "b/c.md", "# Gamma\n\ngamma body")

    from tests.ai.fakes import FakeIndexClient

    class World:
        embeddings = FakeEmbeddings(8)
        index = FakeIndexClient()
        search = FakeSearchClient()

    monkeypatch.setattr(client_mod, "get_embeddings", lambda: World.embeddings)
    monkeypatch.setattr(ingest, "build_index_client", lambda settings=None: World.index)
    monkeypatch.setattr(ingest, "build_search_client", lambda settings=None, **kw: World.search)
    return World, corpus


def _live_rows() -> list[dict[str, Any]]:
    return [_row("docs/a.md"), _row("docs/b/c.md")]


class TestRun:
    def test_a_completed_run_removes_stale_chunks_and_reports_counts(self, world) -> None:
        w, corpus = world
        w.search.rows = [*_live_rows(), _row("docs/deleted.md", 0), _row("docs/deleted.md", 1)]
        report = _run_ingest(corpus)
        assert report.documents == 2 and report.uploaded == 2 and report.failed == 0
        assert report.removed == 2 and report.removal_failed == 0
        deleted = {d[ID_FIELD] for batch in w.search.deleted for d in batch}
        assert deleted == {
            ingest.chunk_id("docs/deleted.md", 0),
            ingest.chunk_id("docs/deleted.md", 1),
        }

    def test_removal_happens_after_every_upload(self, world) -> None:
        w, corpus = world
        w.search.rows = [*_live_rows(), _row("docs/deleted.md")]
        events: list[str] = []
        upload, delete = w.search.upload_documents, w.search.delete_documents
        w.search.upload_documents = lambda documents: (events.append("upload"), upload(documents))[
            1
        ]
        w.search.delete_documents = lambda documents: (events.append("delete"), delete(documents))[
            1
        ]
        _run_ingest(corpus)
        assert events == ["upload", "delete"]

    def test_a_run_that_fails_part_way_removes_nothing(self, world) -> None:
        w, corpus = world
        w.search.rows = [*_live_rows(), _row("docs/deleted.md")]

        class Dies(FakeEmbeddings):
            calls = 0

            def embed_documents(self, texts: list[str]) -> list[list[float]]:
                Dies.calls += 1
                if Dies.calls > 1:
                    raise RuntimeError("the embedding service went away")
                return super().embed_documents(texts)

        w.embeddings = Dies(8)
        with pytest.raises(RuntimeError):
            asyncio.run(ingest.run(["--corpus", str(corpus), "--batch-size", "1"]))
        assert w.search.deleted == []

    def test_a_run_with_failed_uploads_removes_nothing(self, world) -> None:
        w, corpus = world
        w.search.rows = [*_live_rows(), _row("docs/deleted.md")]

        class Refuses(FakeSearchClient):
            def upload_documents(self, documents: list[dict[str, Any]]) -> list[Any]:
                class _R:
                    succeeded = False

                return [_R() for _ in documents]

        w.search = Refuses(w.search.rows)
        report = _run_ingest(corpus)
        assert report.failed == 2 and report.removed == 0
        assert w.search.deleted == []

    def test_no_prune_removes_nothing(self, world) -> None:
        w, corpus = world
        w.search.rows = [*_live_rows(), _row("docs/deleted.md")]
        report = asyncio.run(ingest.run(["--corpus", str(corpus), "--no-prune"]))
        assert report.removed == 0 and w.search.deleted == []

    def test_a_hung_embedding_call_stops_the_run_before_the_index_is_touched(
        self, world, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w, corpus = world
        monkeypatch.setenv("AI_RETRIEVAL_TIMEOUT_SECONDS", "0.05")
        w.embeddings = _HangingEmbeddings(8)
        with pytest.raises(ingest.IngestTimeout):
            _run_ingest(corpus)
        assert w.index.created == [] and w.search.uploaded == [] and w.search.deleted == []

    def test_a_corpus_that_is_empty_after_cleaning_stops_before_any_call(self, world) -> None:
        w, corpus = world
        for file in corpus.rglob("*.md"):
            file.unlink()
        _write(corpus, "only.md", "<!-- everything is a comment -->")
        w.search.rows = [_row("docs/precious.md")]
        with pytest.raises(ingest.CorpusEmpty, match="after cleaning"):
            _run_ingest(corpus)
        assert w.index.created == [] and w.search.uploaded == [] and w.search.deleted == []

    def test_a_file_that_could_not_be_resolved_keeps_the_index_as_it_is(
        self, world, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w, corpus = world
        w.search.rows = [*_live_rows(), _row("docs/deleted.md")]
        broken = _write(corpus, "broken.md", "# Broken")
        real_resolve = Path.resolve

        def resolve(self: Path, strict: bool = False) -> Path:
            if self == broken:
                raise OSError("an I/O error")
            return real_resolve(self, strict=strict)

        monkeypatch.setattr(Path, "resolve", resolve)
        report = _run_ingest(corpus)
        assert report.skipped_unresolved == 1 and report.removed == 0
        assert w.search.deleted == []

    def test_a_wrong_corpus_directory_does_not_wipe_the_index(
        self, world, capsys: pytest.CaptureFixture[str]
    ) -> None:
        w, corpus = world
        w.search.rows = [_row("docs/elsewhere.md", i) for i in range(40)]
        report = _run_ingest(corpus)
        assert report.uploaded == 2 and report.removal_refused and report.removed == 0
        assert w.search.deleted == []
        assert "--force-prune" in capsys.readouterr().err

    def test_force_prune_removes_them(self, world) -> None:
        w, corpus = world
        w.search.rows = [_row("docs/elsewhere.md", i) for i in range(40)]
        report = asyncio.run(ingest.run(["--corpus", str(corpus), "--force-prune"]))
        assert report.removed == 40 and not report.removal_refused

    def test_a_refused_removal_is_an_unsuccessful_exit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def refused(argv=None):
            return ingest.IngestReport(documents=2, chunks=5, uploaded=5, removal_refused=True)

        monkeypatch.setattr(ingest, "run", refused)
        monkeypatch.setattr(ingest, "configure_logging", lambda *a, **k: None)
        monkeypatch.setattr("sys.argv", ["ingest"])
        with pytest.raises(SystemExit) as caught:
            ingest.main()
        assert caught.value.code == 1

    def test_the_report_carries_what_the_load_skipped_and_cleaned(
        self, world, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w, corpus = world
        _write(corpus, "dirty.md", f"# Dirty\nx{ZWSP}y")
        leak = _write(corpus, "leak.md", "# Outside")
        _pretend_outside(monkeypatch, leak, corpus.parent / "elsewhere" / "o.md")
        report = _run_ingest(corpus)
        assert report.documents == 3
        assert report.cleaned == 1 and report.skipped_outside_root == 1
        assert report.skipped_unsafe == 0

    def test_the_configured_timeout_reaches_the_upload_too(
        self, world, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w, corpus = world
        monkeypatch.setenv("AI_RETRIEVAL_TIMEOUT_SECONDS", "0.05")
        w.search = _SlowSearch()
        with pytest.raises(ingest.IngestTimeout, match="uploading a batch"):
            _run_ingest(corpus)
        assert w.search.deleted == []

    def test_the_reported_seconds_cover_the_whole_run(
        self, world, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, corpus = world
        ticks = iter([0.0, 100.0, 101.0, 105.0])
        last = [105.0]

        def clock() -> float:
            last[0] = next(ticks, last[0])
            return last[0]

        monkeypatch.setattr(ingest.time, "perf_counter", clock)
        assert _run_ingest(corpus).seconds == 105.0

    def test_logs_carry_counts_and_durations_only(
        self, world, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        w, corpus = world
        _write(corpus, "dirty.md", f"# Dirty-Title\nSECRET-BODY{ZWSP} <!-- SECRET-COMMENT -->")
        leak = _write(corpus, "leak.md", "# O")
        _pretend_outside(monkeypatch, leak, corpus.parent / "elsewhere" / "outside-doc.md")
        w.search.rows = [_row("docs/secret-gone-file.md")]
        with structlog.testing.capture_logs() as logs:
            _run_ingest(corpus)
        rendered = repr(logs)
        for forbidden in (
            "SECRET-BODY",
            "SECRET-COMMENT",
            "Dirty-Title",
            "dirty.md",
            "outside-doc",
            "secret-gone-file",
            "leak.md",
            str(corpus),
        ):
            assert forbidden not in rendered, forbidden
        read = next(e for e in logs if e["event"] == "ingest files read")
        assert read["documents"] == 3 and read["cleaned"] == 1 and read["skipped_outside_root"] == 1
        removed = next(e for e in logs if e["event"] == "ingest stale chunks removed")
        assert removed["removed"] == 1
