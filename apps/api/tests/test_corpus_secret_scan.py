"""The corpus secret scan (api roadmap 3.4 and 5.7, threat model G7) — offline.

``apps/api/scripts/check_corpus_secrets.py`` runs in the api image build and in the ``Corpus
secret scan`` job of CI. Every kind has a fake secret and look-alikes that must not match; the
document is scanned as written and as the ingest job will index it; the allow-list is exact and
tied to a kind; the output never holds a value.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

API_DIR = Path(__file__).resolve().parents[1]
REPO = API_DIR.parents[1]
SCRIPT = API_DIR / "scripts" / "check_corpus_secrets.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_corpus_secrets", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()

RANDOM32 = "qZkWmXvBnLpTrYsHdGfJcQaVuEoIxNeD"  # 32 different letters: no digit, high entropy
MIXED24 = "a1b2c3d4e5f6a7b8c9d0e1f2"

# Built from pieces so this file itself holds no secret-looking string.
FAKE_SECRETS = {
    "aws_access_key_id": "AKIA" + "ABCDEFGHIJKLMNOP",
    "secret_key_sk": "sk-" + "proj-abcdefghijklmnopqrstuvwx",
    "github_token": "ghp_" + "a" * 36,
    "jwt": "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
    "private_key_block": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "azure_storage_key": "AccountKey=" + "A" * 44 + "==",
    "azure_shared_access_key": "SharedAccessKey=" + "B" * 30 + "==",
    "azure_sas_signature": "https://acct.blob.example.net/c?sv=2024&sig=" + "Z" * 34,
    "azure_ad_client_secret": "abc8Q~" + "Zk3Xp9LmN2vB7cD4eF5gH6jK8nM1qR0sT",
    "slack_token": "xox" + "b-1234567890-abcdefghij",
    "google_api_key": "AI" + "za" + "Sy" + "A" * 33,
    "stripe_key": "sk" + "_live_" + "a" * 24,
    "bearer_token": "Authorization: Bearer " + "a1B2c3D4" * 5,
    "url_credentials": "postgres://app:" + "S3cr3tPassw0rd" + "@db.example.com",
    "connection_string_password": "Server=tcp:db.example.net;User ID=app;Password=hunter2hunter2;",
    "keyword_assignment": "api_key = " + MIXED24,
    "high_entropy_value": "client_secret: " + RANDOM32,
}

LOOK_ALIKES = [
    "keys start with AKIA and then sixteen characters",
    "AKIA is the prefix of an AWS access key id",
    "OpenAI keys look like sk- followed by letters",
    "the task-based disk-usage-and-risk-assessment-report-generator",
    "a github token begins with ghp_ and is 40 characters long",
    "a JWT starts with eyJ... and has three parts",
    "AKIAABCDEFGHIJKL is one character short",
    "-----BEGIN CERTIFICATE-----",
    "mssql+aioodbc://USER:PASSWORD@your-server.database.windows.net:1433/db",
    "AccountKey=<your-key>",
    "Authorization: Bearer <token>",
    "00000000-0000-0000-0000-000000000000",
    "Authorization: Bearer ${TOKEN}",
    "api_key = <your-key>",
    "password: changeme",
    "token: your_token_here",
    "secret: trace-id-propagation-header-name",
    "token = 00000000-0000-0000-0000-000000000000",
    "SharedAccessKey=<key>",
    "https://host.example.net/p?sig=<signature>",
    "postgres://USER:PASSWORD@host:5432/db",
    "mssql://app:${DB_PASSWORD}@host/db",
    "mssql://app:$DB_PASSWORD@host/db",
    "xoxo hugs and kisses",
    "AIzaShort",
    "sk_live is the prefix of a Stripe live key",
    "the password = secret (a sentence, not a connection string)",
    "assignments written `password=`-style or `Password=` in prose",
    "Server=tcp:db.example.net;User ID=app;Password=<your password>;",
    "Server=tcp:db;Password=${DB_PASSWORD};Encrypt=yes",
    "Server=tcp:db;Pwd='<your password>';",
    "Password=changeme",
    "pwd=***",
    "token: team-assistant-orchestration-service-name-for-the-api",
    "secret: trace-id-propagation-header-name-value-here-again",
    "api_key: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "password: " + "ab" * 20,
    "key: the-name-of-the-route-handler-module-file-in-the-api",
    "monkey: " + RANDOM32,
    "the id is " + RANDOM32,
    "secret: " + RANDOM32[:24],
    "client_secret: <" + RANDOM32 + ">",
    "client_secret: ${" + RANDOM32 + "}",
    "SECRET: your",
    "token: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
]


def _kinds(text: str, allowed: Any = None) -> list[str]:
    return sorted({kind for _, kind in checker.scan_text(text, allowed)})


class TestPatterns:
    @pytest.mark.parametrize(("kind", "secret"), sorted(FAKE_SECRETS.items()))
    def test_each_kind_is_found(self, kind: str, secret: str) -> None:
        assert _kinds(f"some text {secret} more text") == [kind]

    def test_all_the_kinds_are_covered_here(self) -> None:
        kinds = {kind for kind, _ in checker.PATTERNS} | {checker.HIGH_ENTROPY_KIND}
        assert kinds == set(FAKE_SECRETS)

    @pytest.mark.parametrize("line", LOOK_ALIKES)
    def test_a_look_alike_is_not_a_finding(self, line: str) -> None:
        assert _kinds(line) == []

    def test_a_secret_in_a_code_fence_or_a_table_is_found(self) -> None:
        secret = FAKE_SECRETS["github_token"]
        assert _kinds(f"```\nexport T={secret}\n```") == ["github_token"]
        assert _kinds(f"| token | `{secret}` |") == ["github_token"]

    def test_finding_lines_are_numbered_from_one(self) -> None:
        found = list(checker.scan_text("clean\n\n" + FAKE_SECRETS["aws_access_key_id"]))
        assert found == [(3, "aws_access_key_id")]

    def test_one_line_with_two_kinds_reports_both(self) -> None:
        line = f"{FAKE_SECRETS['aws_access_key_id']} {FAKE_SECRETS['jwt']}"
        assert _kinds(line) == ["aws_access_key_id", "jwt"]

    def test_a_pgp_private_key_block_is_found(self) -> None:
        assert _kinds("-----BEGIN PGP " + "PRIVATE KEY BLOCK-----") == ["private_key_block"]

    def test_the_aws_documentation_example_is_allowed_by_the_shipped_list(self) -> None:
        assert _kinds("the AWS docs use AKIAIOSFODNN7EXAMPLE") == []
        assert _kinds(f"AKIAIOSFODNN7EXAMPLE and {FAKE_SECRETS['aws_access_key_id']}") == [
            "aws_access_key_id"
        ]


class TestNamesAndDecoration:
    """The shapes secrets really have in documents: prefixed names, markdown, quotes."""

    @pytest.mark.parametrize(
        "name",
        [
            "DB_PASSWORD",
            "OPENAI_API_KEY",
            "SECRET_KEY",
            "AZURE_CLIENT_SECRET",
            "export TOKEN",
            "AZURE_AI_API_KEY",
            "signing_key",
            "STORAGE_ACCESS_KEY",
            "secret_value",
        ],
    )
    def test_a_prefixed_or_suffixed_name_is_a_name(self, name: str) -> None:
        # a name that ends in password or pwd is also a connection-string password
        assert "keyword_assignment" in _kinds(f"{name}={MIXED24}"), name

    @pytest.mark.parametrize(
        "line",
        [
            f"`password`: `{MIXED24}`",
            f"**api_key**: {MIXED24}",
            f"| api_key | {MIXED24} |",
            f"| `DB_PASSWORD` | `{MIXED24}` |",
            f'"password": "{MIXED24}"',
            f"password = '{MIXED24}'",
            f"*token*   :   {MIXED24}",
        ],
    )
    def test_markdown_decoration_round_the_name_and_value_does_not_hide_it(self, line: str) -> None:
        assert _kinds(line) == ["keyword_assignment"], line

    def test_a_value_with_tilde_or_dot_is_a_value(self) -> None:
        azure = "abc8Q~" + "Zk3Xp9LmN2vB7cD4eF5gH6jK8nM1qR0sT"
        assert {"azure_ad_client_secret", "keyword_assignment"} <= set(
            _kinds(f"AZURE_CLIENT_SECRET = {azure}")
        )
        assert _kinds("token: a1b2c3d4.e5f6a7b8.c9d0e1f2.g3h4") == ["keyword_assignment"]

    @pytest.mark.parametrize(
        "line",
        [
            "Server=tcp:db;User Id=app;Password='hunter2hunter2';",
            'Server=tcp:db;Password="hunter2hunter2";',
            "Driver={ODBC Driver 18};PWD={hunter2hunter2};",
            "Server=tcp:db;pwd=hunter2hunter2",
        ],
    )
    def test_quoted_and_braced_connection_string_passwords(self, line: str) -> None:
        assert _kinds(line) == ["connection_string_password"], line

    def test_a_dollar_sign_in_a_url_password_is_not_a_placeholder(self) -> None:
        assert _kinds("https://user:pa$$word123@host.example.com/x") == ["url_credentials"]

    @pytest.mark.parametrize(
        "prefix", ["your-", "example-", "dummy.", "sample_", "changeme-", "xxx-"]
    )
    def test_a_real_value_after_a_placeholder_looking_prefix_is_still_found(
        self, prefix: str
    ) -> None:
        assert _kinds(f"password: {prefix}{RANDOM32}") == ["high_entropy_value"], prefix
        assert _kinds(f"Authorization: Bearer {prefix}a1B2c3D4a1B2c3D4a1B2c3D4a1B2c3D4") == [
            "bearer_token"
        ], prefix


class TestHiddenText:
    """A secret split by something ingestion removes is put back together by ingestion."""

    def _split(self, glue: str) -> str:
        return f"api_key={MIXED24[:8]}{glue}{MIXED24[8:]}"

    @pytest.mark.parametrize(
        "glue",
        [
            "\u200b",  # zero width space
            "\u2060",  # word joiner
            "\ufeff",  # byte-order mark
            "\u202e",  # right-to-left override
            "\U000e0041",  # a tag character
            "️",  # variation selector
            "\x01",  # a control character
            "<!-- hidden -->",
            "<!---->",
        ],
    )
    def test_a_secret_split_by_a_removed_character_or_a_comment_is_found(self, glue: str) -> None:
        assert _kinds(self._split(glue)) == ["keyword_assignment"], repr(glue)

    def test_a_secret_split_by_a_multiline_comment_is_found(self) -> None:
        assert _kinds(f"api_key={MIXED24[:8]}<!--\nmore\ntext\n-->{MIXED24[8:]}") == [
            "keyword_assignment"
        ]

    def test_a_unicode_line_separator_does_not_split_a_value(self) -> None:
        # ``str.splitlines`` would cut the line here; ingestion turns it into a newline, a real
        # line break, so a value that holds one is two lines to the model but one to a reader
        assert _kinds(f"api_key={MIXED24}\u2028") == ["keyword_assignment"]
        assert _kinds(f"first\u2028api_key={MIXED24}") == ["keyword_assignment"]

    def test_a_unicode_line_separator_does_not_shift_the_reported_line_number(self) -> None:
        text = "one\u2028still line one\n" + FAKE_SECRETS["github_token"]
        assert (2, "github_token") in list(checker.scan_text(text))

    def test_the_line_numbers_of_the_visible_view_are_the_lines_of_the_file(self) -> None:
        text = "one\ntwo\n" + FAKE_SECRETS["github_token"] + "\nfour"
        assert list(checker.scan_text(text)) == [(3, "github_token")]

    def test_a_secret_hidden_in_a_comment_is_found_where_it_is(self) -> None:
        text = "text\n<!-- " + FAKE_SECRETS["aws_access_key_id"] + " -->\nmore"
        assert (2, "aws_access_key_id") in list(checker.scan_text(text))

    def test_the_indexed_view_matches_what_ingest_does(self) -> None:
        from app.ai import ingest

        for text in (
            "a\u200bb",
            "x<!-- c -->y",
            "p\x0bq",
            "r\u2028s",
            "t\x01u",
            "v️w",
            "line\n\ttab",
        ):
            assert checker.as_indexed(text) == ingest.clean_text(text), repr(text)


class TestBoundedWork:
    @pytest.mark.parametrize(
        "unit", ["pwd", "api_key", "secret_", "token", "key", "password=", "a1b2", "Q~", "://a:"]
    )
    def test_a_hostile_line_is_scanned_in_linear_time(self, unit: str) -> None:
        line = (unit * (19_000 // len(unit) + 1))[:19_000]
        started = time.perf_counter()
        list(checker.scan_text("x " + line))
        assert time.perf_counter() - started < 2.0, unit

    def test_a_line_over_the_limit_is_refused_not_scanned(self) -> None:
        started = time.perf_counter()
        found = list(checker.scan_text("a" * (checker.MAX_LINE_LENGTH + 1)))
        assert found == [(1, "line_too_long")]
        assert time.perf_counter() - started < 1.0

    def test_a_line_at_the_limit_is_scanned(self) -> None:
        assert list(checker.scan_text("a" * checker.MAX_LINE_LENGTH)) == []

    def test_the_longest_real_doc_line_is_far_from_the_limit(self) -> None:
        longest = max(
            len(line)
            for path in (REPO / "docs").rglob("*.md")
            for line in path.read_text(encoding="utf-8").splitlines()
        )
        assert longest < checker.MAX_LINE_LENGTH // 4


class TestEntropy:
    def test_random_looking_text_is_high_and_words_are_low(self) -> None:
        assert checker.entropy_bits(RANDOM32) >= 4.9
        assert checker.entropy_bits("a" * 40) == 0
        assert checker.entropy_bits("trace-id-propagation-header-name-value-here") < 4.0

    def test_a_long_random_value_next_to_a_secret_word_is_found_even_without_digits(self) -> None:
        for line in (
            f"DB_SECRET_VALUE={RANDOM32}",
            f'"token": "{RANDOM32}"',
            f"password: {RANDOM32}",
            f"key: {RANDOM32}",
            f"storage_key = {RANDOM32}",
        ):
            assert _kinds(line) == ["high_entropy_value"], line

    def test_a_short_random_value_is_left_to_the_other_rules(self) -> None:
        assert _kinds("secret: " + RANDOM32[:24]) == []


ALLOW_AWS = "aws_access_key_id AKIAIOSFODNN7EXAMPLE\n"


class TestAllowlist:
    def _file(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "allow.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def test_it_reads_kind_and_exact_value_and_skips_comments_and_blank_lines(
        self, tmp_path: Path
    ) -> None:
        path = self._file(tmp_path, "# a comment\n\n  " + ALLOW_AWS + "# another\n")
        assert checker.load_allowlist(path) == {("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE")}

    @pytest.mark.parametrize(
        ("line", "reason"),
        [
            ("AKIAIOSFODNN7EXAMPLE", "unknown kind"),
            ("nonsense AKIAIOSFODNN7EXAMPLE", "unknown kind"),
            ("aws_access_key_id", "at least"),
            ("aws_access_key_id short", "at least"),
            ("private_key_block -----BEGIN PRIVATE KEY-----", "switch the kind off"),
            ("not_plain_text something-long", "switch the kind off"),
            ("line_too_long something-long", "switch the kind off"),
            ("symlink something-long", "switch the kind off"),
            ("aws_access_key_id AKIAZZZZZZZZZZZZZZZZZZZZ", "stale or typo"),
            ("high_entropy_value tooshort1234", "stale or typo"),
            ("high_entropy_value " + "a" * 40, "stale or typo"),
        ],
    )
    def test_an_entry_that_would_allow_too_much_or_is_wrong_is_refused(
        self, tmp_path: Path, line: str, reason: str
    ) -> None:
        with pytest.raises(checker.AllowlistError, match=f"allow.txt:1.*{reason}"):
            checker.load_allowlist(self._file(tmp_path, line + "\n"))

    def test_the_shipped_list_holds_only_the_aws_example_and_is_valid(self) -> None:
        shipped = checker.load_allowlist(checker.DEFAULT_ALLOWLIST)
        assert shipped == {("aws_access_key_id", "AKIAIOSFODNN7EXAMPLE")}
        assert checker.ALLOWED_EXAMPLES == shipped
        assert checker.DEFAULT_ALLOWLIST_ERROR is None

    def test_an_entry_silences_its_own_kind_for_its_own_value_only(self, tmp_path: Path) -> None:
        fake = "AKIA" + "ZZZZZZZZZZZZZZZZ"
        other = "AKIA" + "YYYYYYYYYYYYYYYY"
        allow = checker.load_allowlist(self._file(tmp_path, f"aws_access_key_id {fake}\n"))
        assert _kinds(f"key {fake}", allow) == []
        assert _kinds(f"key {other}", allow) == ["aws_access_key_id"]

    def test_an_entry_of_one_kind_does_not_silence_another_kind(self, tmp_path: Path) -> None:
        # the same text is a finding of a different kind when another rule reads it
        fake = "AKIA" + "ZZZZZZZZZZZZZZZZ"
        allow = checker.load_allowlist(self._file(tmp_path, f"aws_access_key_id {fake}\n"))
        assert _kinds(f"api_key = {fake}1", allow) != []  # a longer value is not the entry
        assert _kinds(f"secret_key = {fake}a1", allow) != []

    def test_a_second_kind_matching_the_same_text_is_not_silenced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        twin = ("jwt_twin", re.compile(r"\beyJ[A-Za-z0-9_-]{20,}"))
        monkeypatch.setattr(checker, "PATTERNS", (*checker.PATTERNS, twin))
        value = FAKE_SECRETS["jwt"]
        allow = frozenset({("jwt", value)})
        assert _kinds(f"x {value} y", allow) == ["jwt_twin"]  # `jwt` is silenced, the twin is not

    def test_an_entropy_value_is_allowed_by_its_value(self, tmp_path: Path) -> None:
        allow = checker.load_allowlist(self._file(tmp_path, f"high_entropy_value {RANDOM32}\n"))
        assert _kinds(f"secret: {RANDOM32}", allow) == []
        assert _kinds(f"secret: {RANDOM32}", None) == ["high_entropy_value"]

    def test_an_allowed_value_embedded_in_a_longer_one_is_still_a_finding(self) -> None:
        assert _kinds("the key AKIAIOSFODNN7EXAMPLE1 is not that key") == []  # not an AWS shape
        assert _kinds("secret: AKIAIOSFODNN7EXAMPLE" + "ZZZZZZZZZZZZZZZZZ9") != []


class TestCommand:
    def _corpus(self, tmp_path: Path, **files: str) -> Path:
        root = tmp_path / "corpus"
        for name, text in files.items():
            path = root / name.replace("__", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        root.mkdir(parents=True, exist_ok=True)
        return root

    def test_a_clean_corpus_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = self._corpus(tmp_path, **{"a.md": "# Fine\n", "sub__b.md": "text " + LOOK_ALIKES[0]})
        assert checker.main([str(root)]) == 0
        assert "clean" in capsys.readouterr().out

    def test_a_finding_fails_and_prints_the_file_the_line_and_the_kind_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        secret = FAKE_SECRETS["github_token"]
        root = self._corpus(
            tmp_path, **{"ok.md": "fine", "deep__notes.md": f"x\nthe token {secret}\n"}
        )
        assert checker.main([str(root)]) == 1
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert "deep/notes.md:2: looks like github_token" in out
        assert "ok.md:" not in out
        # never the matched text, never any part of it, never the line around it
        assert secret not in out and secret[:8] not in out and "the token" not in out

    @pytest.mark.parametrize(("kind", "secret"), sorted(FAKE_SECRETS.items()))
    def test_no_kind_ever_prints_any_of_its_value(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str, secret: str
    ) -> None:
        root = self._corpus(tmp_path, **{"a.md": f"before {secret} after\n"})
        assert checker.main([str(root)]) == 1
        captured = capsys.readouterr()
        out = captured.out + captured.err
        assert f"a.md:1: looks like {kind}" in out
        for piece in re.findall(r"[A-Za-z0-9+/_~.-]{6,}", secret):
            assert piece not in out, (kind, piece)

    def test_every_finding_of_every_file_is_listed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = self._corpus(
            tmp_path, **{"a.md": FAKE_SECRETS["jwt"], "b.md": FAKE_SECRETS["private_key_block"]}
        )
        assert checker.main([str(root)]) == 1
        err = capsys.readouterr().err
        assert "a.md:1: looks like jwt" in err and "b.md:1: looks like private_key_block" in err
        assert "2 finding(s)" in err

    def test_bytes_that_are_not_utf8_are_read_without_failing(self, tmp_path: Path) -> None:
        root = self._corpus(tmp_path)
        (root / "latin1.md").write_bytes(b"caf\xe9 and \x80\x81 text")
        assert checker.main([str(root)]) == 0

    def test_a_secret_hidden_in_utf8_garbage_is_still_found(self, tmp_path: Path) -> None:
        root = self._corpus(tmp_path)
        (root / "mixed.md").write_bytes(b"\x80\x81 " + FAKE_SECRETS["jwt"].encode() + b" \xe9")
        assert checker.main([str(root)]) == 1

    @pytest.mark.parametrize(
        "data",
        [
            "token text".encode("utf-16"),
            "token text".encode("utf-16-le"),
            "token text".encode("utf-16-be"),
            b"plain\x00text",
            b"\xff\xfe",
            b"\xfe\xff",
        ],
        ids=[
            "utf-16 with BOM",
            "utf-16-le",
            "utf-16-be",
            "a NUL byte",
            "a BOM alone",
            "a BE BOM alone",
        ],
    )
    def test_text_a_pattern_cannot_read_is_a_finding_by_itself(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], data: bytes
    ) -> None:
        root = self._corpus(tmp_path)
        (root / "wide.md").write_bytes(data)
        assert checker.main([str(root)]) == 1
        assert "wide.md:0: looks like not_plain_text" in capsys.readouterr().err

    def test_a_symlink_in_the_corpus_is_a_finding(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = self._corpus(tmp_path, **{"real.md": "fine"})
        try:
            (root / "link.md").symlink_to(root / "real.md")
        except OSError, NotImplementedError:
            pytest.skip("this platform cannot create a symbolic link here")
        assert checker.main(["--include", "*.md", str(root)]) == 1
        assert "link.md:0: looks like symlink" in capsys.readouterr().err

    def test_a_path_that_is_a_link_is_a_finding_whatever_it_points_at(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = self._corpus(tmp_path, **{"real.md": "fine", "link.md": "fine"})
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink", lambda self: self.name == "link.md" or real_is_symlink(self)
        )
        assert checker.main(["--include", "*.md", str(root)]) == 1
        err = capsys.readouterr().err
        assert "link.md:0: looks like symlink" in err and "real.md" not in err

    def test_a_line_too_long_to_be_prose_is_a_finding(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = self._corpus(tmp_path, **{"big.md": "a" * (checker.MAX_LINE_LENGTH + 1)})
        assert checker.main([str(root)]) == 1
        assert "big.md:1: looks like line_too_long" in capsys.readouterr().err

    def test_a_missing_directory_fails_the_build_instead_of_passing_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert checker.main([str(tmp_path / "nowhere")]) == 2
        assert "is not a directory" in capsys.readouterr().err

    def test_no_argument_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert checker.main([]) == 2
        assert "usage" in capsys.readouterr().err

    def test_a_mistyped_option_never_echoes_what_was_typed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        pasted = "SECRETVALUE" + "12345abcdef"
        assert checker.main([f"--bogus={pasted}", str(tmp_path)]) == 2
        captured = capsys.readouterr()
        assert pasted not in captured.out + captured.err and "usage" in captured.err

    def test_an_abbreviated_option_is_not_accepted(self, tmp_path: Path) -> None:
        assert checker.main(["--incl", "*.md", str(tmp_path)]) == 2
        assert checker.main(["--allow", str(tmp_path / "x"), str(tmp_path)]) == 2

    def test_help_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert checker.main(["--help"]) == 0
        capsys.readouterr()

    def test_a_path_is_printed_as_one_safe_line(self) -> None:
        nasty = Path("docs") / "x\n::warning::injected\x1b[31m.md"
        printed = checker.safe_path(nasty)
        assert "\n" not in printed and "\x1b" not in printed and printed.isascii()
        assert printed.startswith("docs/x")
        assert checker.safe_path(Path("docs/é.md")).isascii()

    def test_it_runs_as_a_script_with_the_standard_library_only(self, tmp_path: Path) -> None:
        root = self._corpus(tmp_path, **{"a.md": FAKE_SECRETS["aws_access_key_id"]})
        run = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), str(root)], capture_output=True, text=True
        )
        assert run.returncode == 1 and "a.md:1: looks like aws_access_key_id" in run.stderr
        imports = re.findall(r"^(?:from|import) (\w+)", SCRIPT.read_text(encoding="utf-8"), re.M)
        assert set(imports) <= set(sys.stdlib_module_names) | {"__future__"}

    def test_the_repositorys_own_docs_pass(self) -> None:
        """What the image build and the CI job check: a secret pasted into a document fails here
        first. Only the files the image keeps: visible ``.md`` files."""
        shipped = [
            p
            for p in sorted((REPO / "docs").rglob("*.md"))
            if p.is_file()
            and not any(part.startswith(".") for part in p.relative_to(REPO / "docs").parts)
        ]
        assert shipped, "no markdown found: is the docs directory where the test expects it?"
        assert checker.main(["--include", "*.md", str(REPO / "docs")]) == 0

    def test_the_command_takes_an_allowlist_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fake = "AKIA" + "ZZZZZZZZZZZZZZZZ"
        root = self._corpus(tmp_path, **{"a.md": f"example key {fake}\n"})
        allow = tmp_path / "allow.txt"
        allow.write_text(f"aws_access_key_id {fake}\n", encoding="utf-8")
        assert checker.main([str(root)]) == 1
        assert checker.main(["--allowlist", str(allow), str(root)]) == 0
        capsys.readouterr()

    def test_a_missing_or_bad_allowlist_fails_instead_of_allowing_everything(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = self._corpus(tmp_path)
        assert checker.main(["--allowlist", str(tmp_path / "nowhere.txt"), str(root)]) == 2
        assert "not a file" in capsys.readouterr().err
        bad = tmp_path / "bad.txt"
        bad.write_text("aws_access_key_id tiny\n", encoding="utf-8")
        assert checker.main(["--allowlist", str(bad), str(root)]) == 2
        assert "at least" in capsys.readouterr().err
        unreadable = tmp_path / "bytes.txt"
        unreadable.write_bytes(b"\xff\xfe\x00\x80 not utf-8")
        assert checker.main(["--allowlist", str(unreadable), str(root)]) == 2
        assert "could not be read" in capsys.readouterr().err

    def test_a_bad_default_allowlist_is_exit_two_not_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            checker, "DEFAULT_ALLOWLIST_ERROR", "corpus-secrets-allowlist.txt:3: bad"
        )
        assert checker.main([str(self._corpus(tmp_path))]) == 2
        assert "allowlist.txt:3: bad" in capsys.readouterr().err

    def test_the_allowlist_error_never_prints_the_value_of_an_entry(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        secretish = "AKIA" + "QQQQQQQQQQQQQQQQ"
        bad = tmp_path / "bad.txt"
        bad.write_text(f"aws_access_key_id {secretish}\nnonsense {secretish}\n", encoding="utf-8")
        assert checker.main(["--allowlist", str(bad), str(self._corpus(tmp_path))]) == 2
        captured = capsys.readouterr()
        assert secretish not in captured.out + captured.err


class TestInclude:
    def test_only_the_matching_files_are_read(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "ok.md").write_text("fine\n", encoding="utf-8")
        (docs / "spec.json").write_text(
            json.dumps({"k": FAKE_SECRETS["aws_access_key_id"]}), encoding="utf-8"
        )
        assert checker.main(["--include", "*.md", str(docs)]) == 0
        assert checker.main([str(docs)]) == 1  # without the filter, every file is read
        assert checker.main(["--include", "*.md", "--include", "*.json", str(docs)]) == 1

    def test_a_markdown_file_in_a_subdirectory_is_found(self, tmp_path: Path) -> None:
        deep = tmp_path / "docs" / "a" / "b"
        deep.mkdir(parents=True)
        (deep / "c.md").write_text(FAKE_SECRETS["github_token"], encoding="utf-8")
        assert checker.main(["--include", "*.md", str(tmp_path / "docs")]) == 1

    def test_the_match_is_case_sensitive_as_the_ingest_job_is(self, tmp_path: Path) -> None:
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "X.MD").write_text(FAKE_SECRETS["github_token"], encoding="utf-8")
        assert checker.main(["--include", "*.md", str(docs)]) == 0  # not a corpus file


class TestCiJob:
    @staticmethod
    def _workflow() -> str:
        return (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    def _job(self) -> str:
        text = self._workflow()
        start = text.index("\n  corpus-secrets:\n")
        end = text.index("\n  # --", start + 1)  # the next job's heading comment
        job = text[start:end]
        assert "\n  test:" not in job and "\n  build:" not in job  # it is one job
        return job

    def test_the_job_runs_the_script_on_the_markdown_docs(self) -> None:
        job = self._job()
        assert "name: Corpus secret scan" in job
        assert re.search(
            r"^\s+run: python apps/api/scripts/check_corpus_secrets\.py --include '\*\.md' docs$",
            job,
            re.M,
        )
        assert "actions/checkout@" in job and "actions/setup-python@" in job

    def test_the_job_cannot_be_skipped_or_made_advisory(self) -> None:
        job = self._job()
        assert "continue-on-error" not in job and "\n    if:" not in job
        assert "|| true" not in job

    def test_the_job_has_a_time_limit_and_keeps_no_git_credential(self) -> None:
        job = self._job()
        assert re.search(r"^    timeout-minutes: \d+$", job, re.M)
        assert "persist-credentials: false" in job

    def test_the_workflow_runs_on_every_pull_request_into_the_protected_branches(self) -> None:
        text = self._workflow()
        head = text[: text.index("\njobs:")]
        assert re.search(r"^  pull_request:\n    branches: \[[^\]]*\bdevelopment\b", head, re.M)
        assert "paths:" not in head  # no path filter can skip the scan

    def test_the_dockerfile_scans_every_file_it_copies(self) -> None:
        dockerfile = (API_DIR / "Dockerfile").read_text(encoding="utf-8")
        line = next(ln for ln in dockerfile.splitlines() if "check_corpus_secrets.py /corpus" in ln)
        assert "--include" not in line  # the image's corpus is md only, and all of it is checked

    def test_the_scanner_and_its_allowlist_have_owners(self) -> None:
        owners = (REPO / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        for path in (
            "/apps/api/scripts/check_corpus_secrets.py",
            "/apps/api/scripts/corpus-secrets-allowlist.txt",
            "/.github/workflows/",
        ):
            assert re.search(rf"^{re.escape(path)}\s+@\S+", owners, re.M), path
