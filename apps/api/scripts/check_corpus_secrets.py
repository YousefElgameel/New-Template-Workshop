"""Fail when a document holds something that looks like a credential (api 3.4 and 5.7, G7).

Run in two places, so a secret pasted into a document is stopped twice, before it can be indexed
and answered from: by ``apps/api/Dockerfile`` on ``docs/**/*.md`` before they are copied into the
image, and by the ``Corpus secret scan`` job of ``.github/workflows/ci.yml`` on every pull
request::

    python apps/api/scripts/check_corpus_secrets.py [--include GLOB] [--allowlist FILE] DIR...

It reads every file under each directory (only those matching ``--include``, when given) and
prints, for each match, the **file, the line number and the kind** of pattern, never the matched
text and never the line. Exit status: ``0`` clean, ``1`` a finding, ``2`` a directory or an
allow-list that does not exist or is bad (a check that checked nothing must not pass).

Each document is scanned twice: as written, and as the ingest job will index it (control and
format characters and ``<!-- comments -->`` removed, see ``app/ai/ingest.py``), so a secret that
is split by a zero-width character or a comment, and put back together by ingestion, is still
seen. Line numbers of a finding in the second view count after that removal.

Known fake values (the AWS documentation's example key) are listed in
``corpus-secrets-allowlist.txt`` next to this script as ``<kind> <exact value>`` lines: exact
strings, each tied to the kind it silences, at least 8 characters, never a pattern or a file, and
never for a fixed-header kind; reviewed in the pull request that adds them (CODEOWNERS).

Standard library only: the stages that run it have no project environment. The patterns look for
the shapes of real credentials with a minimum length, so the words themselves (a document that
says "keys start with ``AKIA``") do not match. Extend ``PATTERNS`` here; the tests pin each one
with a fake secret and a look-alike. Known limits: a secret wrapped over two lines, or encoded
(base64) inside a document, is not seen, and ``Password = value`` with spaces is read as prose;
this is a safety net with hand-written patterns, not a replacement for not pasting secrets (a
maintained scanner would be a new tool and an ADR).
"""

from __future__ import annotations

import argparse
import fnmatch
import math
import re
import sys
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ALLOWLIST = Path(__file__).with_name("corpus-secrets-allowlist.txt")
MIN_ALLOWLIST_ENTRY = 8
# A line this long in a document is not prose: it is refused rather than scanned (and it bounds the
# time every pattern below can take).
MAX_LINE_LENGTH = 20_000

# --- the patterns ---

# What may sit in a secret value, and what may sit between a name and its value in a document:
# markdown decoration (backticks, bold, a table pipe), quotes, spaces.
_VALUE = r"[A-Za-z0-9+/_~.-]"
_SEP = r"""[\s`*'"]*(?:[=:]|\|)[\s`*'"|]*"""
# A name is preceded by anything but a letter or digit, so `DB_PASSWORD` and `OPENAI_API_KEY` count.
_NAME_BEFORE = r"(?<![A-Za-z0-9])"
_NAME_SUFFIX = r"[A-Za-z0-9_]{0,30}"
# Not a placeholder: a whole `<your-key>`, `${KEY}`, `$KEY`, `{{ key }}`, `***`, or a value that IS
# one of the words (`your`, `xxxx`, `changeme`...). A real value after such a prefix is not skipped.
_NOT_PLACEHOLDER = (
    r"(?!<[^>]*>|\$\{|\$[A-Za-z_]|\{\{|\*{2,}"
    r"|(?i:your|xxx+|example|changeme|placeholder|redacted|dummy|sample)(?![A-Za-z0-9_.~/+-]))"
)
# A value that has both a letter and a digit, so a word or an all-zero id is not a "secret".
_MIXED = r"(?=[A-Za-z0-9+/_~.-]*[A-Za-z])(?=[A-Za-z0-9+/_~.-]*\d)"
_KEYWORD = (
    r"(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|private[_-]?key"
    r"|client[_-]?secret|token|key)"
)

# (kind, pattern). Kinds are the only thing printed about a match.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("secret_key_sk", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})"),
    ),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{20,}")),
    ("private_key_block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")),
    ("azure_storage_key", re.compile(r"\bAccountKey=[A-Za-z0-9+/]{40,}={0,2}")),
    ("azure_shared_access_key", re.compile(r"\bSharedAccessKey=[A-Za-z0-9+/]{20,}={0,2}")),
    ("azure_sas_signature", re.compile(r"[?&;]sig=[A-Za-z0-9%+/]{30,}")),
    (
        "azure_ad_client_secret",
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9_.-]{3}\dQ~[A-Za-z0-9_.~-]{31,}"),
    ),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{20,}")),
    ("bearer_token", re.compile(r"\bBearer\s+" + _NOT_PLACEHOLDER + r"[A-Za-z0-9._~+/=-]{30,}")),
    (
        "url_credentials",
        re.compile(
            r"://[^/\s:@<>{}$]{1,64}:(?!(?i:password|passwd|pass|secret|changeme)\b)"
            + _NOT_PLACEHOLDER
            + r"[^/\s@<>{}]{8,}@[\w.-]+"
        ),
    ),
    (
        # `Password=value`, `Pwd='value'` and `PWD={value}` as a connection string writes them (no
        # spaces round the `=`): prose such as "password = secret" is not one.
        "connection_string_password",
        re.compile(
            r"(?i)"
            + _NAME_BEFORE
            + r"(?:password|pwd)="
            + _NOT_PLACEHOLDER
            + r"""['"{]?[^;\s'"`<>{}$,)]{6,}"""
        ),
    ),
    (
        "keyword_assignment",
        re.compile(
            r"(?i)"
            + _NAME_BEFORE
            + _KEYWORD
            + _NAME_SUFFIX
            + _SEP
            + _NOT_PLACEHOLDER
            + _MIXED
            + r"[A-Za-z0-9+/_~.-]{20,}"
        ),
    ),
)

# A long value next to a secret-like word whose characters look random (letters only, so the
# letters-and-digits rule above misses it).
_ENTROPY_CANDIDATE = re.compile(
    r"(?i)"
    + _NAME_BEFORE
    + r"(?:password|passwd|pwd|secret|token|key)"
    + _NAME_SUFFIX
    + _SEP
    + _NOT_PLACEHOLDER
    + r"([A-Za-z0-9+/=_~.-]{32,})"
)
HIGH_ENTROPY_KIND = "high_entropy_value"
MIN_ENTROPY_BITS = 4.0

# Kinds that are not a pattern's match: a document that cannot be read as text, a line too long to
# scan, a link (the corpus is files only).
NOT_PLAIN_TEXT = "not_plain_text"
LINE_TOO_LONG = "line_too_long"
SYMLINK = "symlink"
# A UTF-16 file reads as UTF-8 with a NUL between the letters, which no pattern above can match.
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")

# --- the allow-list ---

# Kinds whose whole match is a fixed header or marker: one entry would switch the kind off for
# every document. They cannot be allow-listed.
NOT_ALLOWLISTABLE = frozenset({"private_key_block", NOT_PLAIN_TEXT, LINE_TOO_LONG, SYMLINK})
Allowance = frozenset[tuple[str, str]]


class AllowlistError(ValueError):
    """The allow-list holds an entry that is malformed or would allow too much."""


def _known_kinds() -> frozenset[str]:
    return frozenset(kind for kind, _ in PATTERNS) | {HIGH_ENTROPY_KIND} | NOT_ALLOWLISTABLE


def load_allowlist(path: Path) -> Allowance:
    """``(kind, exact value)`` of each ``<kind> <value>`` line; ``#`` starts a comment."""
    entries: set[tuple[str, str]] = set()
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{path.name}:{number}"
        kind, _, value = line.partition(" ")
        value = value.strip()
        if kind not in _known_kinds():
            raise AllowlistError(f"{where}: unknown kind (each line is '<kind> <exact value>')")
        if kind in NOT_ALLOWLISTABLE:
            raise AllowlistError(f"{where}: a {kind} entry would switch the kind off everywhere")
        if len(value) < MIN_ALLOWLIST_ENTRY:
            raise AllowlistError(
                f"{where}: a value must be at least {MIN_ALLOWLIST_ENTRY} characters "
                f"(a short value would allow a whole class of text)"
            )
        if not _matches_its_kind(kind, value):
            raise AllowlistError(f"{where}: the value is not what its kind matches (stale or typo)")
        entries.add((kind, value))
    return frozenset(entries)


def _matches_its_kind(kind: str, value: str) -> bool:
    if kind == HIGH_ENTROPY_KIND:
        return len(value) >= 32 and entropy_bits(value) >= MIN_ENTROPY_BITS
    pattern = dict(PATTERNS)[kind]
    return any(m.group(0) == value for m in pattern.finditer(value))


# Values that are documented examples, not credentials, from the allow-list file next to this
# script. A bad default file is reported by ``main`` (exit 2), not raised at import.
DEFAULT_ALLOWLIST_ERROR: str | None = None
try:
    ALLOWED_EXAMPLES: Allowance = (
        load_allowlist(DEFAULT_ALLOWLIST) if DEFAULT_ALLOWLIST.is_file() else frozenset()
    )
except (AllowlistError, OSError, UnicodeDecodeError) as _exc:
    ALLOWED_EXAMPLES = frozenset()
    DEFAULT_ALLOWLIST_ERROR = str(_exc)

# --- the scan ---

# Control characters that separate words become newlines, and the "invisible" ones vanish, exactly
# as the ingest job does it (app/ai/ingest.py `_clean_characters`).
_SEPARATORS = "\x0b\x0c\x85\u2028\u2029"
_INVISIBLE = frozenset(
    [*range(0xFE00, 0xFE10), *range(0xE0100, 0xE01F0), 0x3164, 0xFFA0, 0x115F, 0x1160, 0x2800]
)


def entropy_bits(value: str) -> float:
    """Shannon entropy of ``value`` in bits per character (random base64 is about 4.5 to 5.5)."""
    counts = Counter(value)
    total = len(value)
    return -sum(n / total * math.log2(n / total) for n in counts.values())


def _strip_html_comments(text: str) -> str:
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


def as_indexed(text: str) -> str:
    """The text as the ingest job will chunk it: separators as newlines, control, format and
    invisible characters dropped, HTML comments removed."""
    cleaned: list[str] = []
    for ch in text:
        if ch in "\n\t":
            cleaned.append(ch)
        elif ch in _SEPARATORS:
            cleaned.append("\n")
        elif ord(ch) in _INVISIBLE or unicodedata.category(ch) in ("Cc", "Cf"):
            continue
        else:
            cleaned.append(ch)
    return _strip_html_comments("".join(cleaned))


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str


def _lines(text: str) -> list[str]:
    """Lines split on ``\\n`` only (a Unicode line separator inside a value must not split it)."""
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _scan_view(text: str, allow: Allowance) -> Iterator[tuple[int, str]]:
    for number, line in enumerate(_lines(text), start=1):
        if len(line) > MAX_LINE_LENGTH:
            yield number, LINE_TOO_LONG
            continue
        for kind, pattern in PATTERNS:
            if any((kind, m.group(0)) not in allow for m in pattern.finditer(line)):
                yield number, kind
        if any(
            (HIGH_ENTROPY_KIND, m.group(1)) not in allow
            and entropy_bits(m.group(1)) >= MIN_ENTROPY_BITS
            for m in _ENTROPY_CANDIDATE.finditer(line)
        ):
            yield number, HIGH_ENTROPY_KIND


def scan_text(text: str, allowed: Allowance | None = None) -> Iterator[tuple[int, str]]:
    """``(line number, kind)`` of every match in ``text`` as written and as indexed, once each;
    the matched text is never returned. ``allowed`` is the allow-list (default: the file next to
    this script)."""
    allow = ALLOWED_EXAMPLES if allowed is None else allowed
    found = set(_scan_view(text, allow))
    indexed = as_indexed(text)
    if indexed != text:
        found |= set(_scan_view(indexed, allow))
    yield from sorted(found)


def safe_path(path: Path) -> str:
    """A path as one printable line: a file name can hold a newline or an escape sequence, and a
    log line that starts with ``::`` is a workflow command to the CI runner."""
    return path.as_posix().encode("unicode_escape").decode("ascii")


def scan_directory(
    root: Path, includes: Iterable[str] = (), allowed: Allowance | None = None
) -> list[Finding]:
    patterns = list(includes)
    findings: list[Finding] = []
    for path in sorted(p for p in root.rglob("*") if p.is_symlink() or p.is_file()):
        if patterns and not any(fnmatch.fnmatchcase(path.name, pattern) for pattern in patterns):
            continue
        if path.is_symlink():
            findings.append(Finding(path, 0, SYMLINK))
            continue
        data = path.read_bytes()
        if b"\x00" in data or data.startswith(UTF16_BOMS):
            # Not something the patterns can read, so not something that may be indexed.
            findings.append(Finding(path, 0, NOT_PLAIN_TEXT))
            continue
        # Undecodable bytes become U+FFFD: they cannot hide a secret from a text pattern.
        text = data.decode("utf-8", errors="replace")
        findings.extend(Finding(path, number, kind) for number, kind in scan_text(text, allowed))
    return findings


USAGE = "usage: check_corpus_secrets.py [--include GLOB] [--allowlist FILE] DIR [DIR ...]"


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # never echo what was typed: it may be a pasted value
        raise _UsageError


def main(argv: Iterable[str] | None = None) -> int:
    parser = _Parser(prog="check_corpus_secrets.py", description=__doc__, allow_abbrev=False)
    parser.add_argument("directories", nargs="*", type=Path, metavar="DIR")
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="only files whose name matches (repeatable, case-sensitive), e.g. '*.md'",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=None,
        metavar="FILE",
        help="'<kind> <exact value>' lines to allow (default: corpus-secrets-allowlist.txt)",
    )
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    except _UsageError:
        print(USAGE, file=sys.stderr)
        return 2
    except SystemExit as exit_:  # --help
        return 2 if exit_.code else 0
    if not args.directories:
        print(USAGE, file=sys.stderr)
        return 2
    missing = [r for r in args.directories if not r.is_dir()]
    if missing:
        for root in missing:
            print(f"corpus secret check: {safe_path(root)} is not a directory", file=sys.stderr)
        return 2
    allowed: Allowance | None = None
    try:
        if args.allowlist is not None:
            if not args.allowlist.is_file():
                print("corpus secret check: the allow-list is not a file", file=sys.stderr)
                return 2
            allowed = load_allowlist(args.allowlist)
        elif DEFAULT_ALLOWLIST_ERROR is not None:
            print(f"corpus secret check: {DEFAULT_ALLOWLIST_ERROR}", file=sys.stderr)
            return 2
    except AllowlistError as exc:
        print(f"corpus secret check: {exc}", file=sys.stderr)
        return 2
    except OSError, UnicodeDecodeError:
        print("corpus secret check: the allow-list could not be read", file=sys.stderr)
        return 2
    total = 0
    try:
        for root in args.directories:
            for finding in scan_directory(root, args.include, allowed):
                total += 1
                print(
                    f"corpus secret check: {safe_path(finding.path.relative_to(root))}:"
                    f"{finding.line}: looks like {finding.kind}",
                    file=sys.stderr,
                )
    except OSError:
        print("corpus secret check: a file could not be read", file=sys.stderr)
        return 2
    if total:
        print(
            f"corpus secret check: {total} finding(s); the build stops. Remove the value from "
            f"the document (rotate it if it was real); the matched text is not printed. A known "
            f"fake value goes in corpus-secrets-allowlist.txt, in a reviewed change.",
            file=sys.stderr,
        )
        return 1
    print("corpus secret check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
