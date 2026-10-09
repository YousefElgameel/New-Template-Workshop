"""Fail when a corpus file holds something that looks like a credential (api 3.4, threat model G7).

Run by ``apps/api/Dockerfile`` on ``docs/**/*.md`` **before** they are copied into the image, so a
secret pasted into a document stops the build instead of being indexed and answered from::

    python scripts/check_corpus_secrets.py DIR [DIR ...]

It reads every file under each directory and prints, for each match, the **file, the line number
and the kind** of pattern, never the matched text and never the line. Exit status: ``0`` clean,
``1`` a finding, ``2`` a directory that does not exist (the build must not pass because nothing
was checked).

Standard library only: the builder stage that runs it has no project environment. The patterns
look for the shapes of real credentials with a minimum length, so the words themselves (a document
that says "keys start with ``AKIA``") do not match. Extend ``PATTERNS`` here; the tests pin each
one with a fake secret and a look-alike. Known limits: a secret wrapped over two lines, or encoded
(base64) inside a document, is not seen; ``ALLOWED_EXAMPLES`` holds the documented fake values.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

# Values that are documented examples, not credentials: matched text equal to one of these is not a
# finding. Exact strings only (never a pattern or a file), added in a reviewed pull request.
ALLOWED_EXAMPLES: frozenset[str] = frozenset({"AKIAIOSFODNN7EXAMPLE"})

# A value that has both a letter and a digit, so a word or an all-zero id is not a "secret".
_MIXED = r"(?=[A-Za-z0-9+/_-]*[A-Za-z])(?=[A-Za-z0-9+/_-]*\d)"
# Not a placeholder: `<your-key>`, `${KEY}`, `$KEY`, `{{ key }}`, `your_...`, `xxxx`, ...
_NOT_PLACEHOLDER = r"(?![<{$(*]|(?i:your|xxx|example|changeme|placeholder|redacted|dummy|sample)\b)"

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
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe_key", re.compile(r"\b[sr]k_(?:live|test)_[0-9A-Za-z]{20,}")),
    ("bearer_token", re.compile(r"\bBearer\s+" + _NOT_PLACEHOLDER + r"[A-Za-z0-9._~+/=-]{30,}")),
    (
        "url_credentials",
        re.compile(
            r"://[^/\s:@<>{}$]{1,64}:(?!(?i:password|passwd|pass|secret|changeme)\b)"
            + _NOT_PLACEHOLDER
            + r"[^/\s@<>{}$]{8,}@[\w.-]+"
        ),
    ),
    (
        "keyword_assignment",
        re.compile(
            r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|client[_-]?secret|token)"
            r"""\b["']?\s*[=:]\s*["']?""" + _NOT_PLACEHOLDER + _MIXED + r"[A-Za-z0-9+/_-]{20,}"
        ),
    ),
)

# A UTF-16 file reads as UTF-8 with a NUL between the letters, which no pattern above can match.
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
NOT_PLAIN_TEXT = "not_plain_text"


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    kind: str


def scan_text(text: str) -> Iterator[tuple[int, str]]:
    """``(line number, kind)`` of every match in ``text``; the matched text is never returned."""
    for number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in PATTERNS:
            if any(m.group(0) not in ALLOWED_EXAMPLES for m in pattern.finditer(line)):
                yield number, kind


def scan_directory(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        data = path.read_bytes()
        if b"\x00" in data or data.startswith(UTF16_BOMS):
            # Not something the patterns can read, so not something that may be indexed.
            findings.append(Finding(path, 0, NOT_PLAIN_TEXT))
            continue
        # Undecodable bytes become U+FFFD: they cannot hide a secret from a text pattern.
        text = data.decode("utf-8", errors="replace")
        findings.extend(Finding(path, number, kind) for number, kind in scan_text(text))
    return findings


def main(argv: Iterable[str] | None = None) -> int:
    roots = [Path(a) for a in (sys.argv[1:] if argv is None else argv)]
    if not roots:
        print("usage: check_corpus_secrets.py DIR [DIR ...]", file=sys.stderr)
        return 2
    missing = [r for r in roots if not r.is_dir()]
    if missing:
        for root in missing:
            print(f"corpus secret check: {root} is not a directory", file=sys.stderr)
        return 2
    total = 0
    for root in roots:
        for finding in scan_directory(root):
            total += 1
            print(
                f"corpus secret check: {finding.path.relative_to(root).as_posix()}:"
                f"{finding.line}: looks like {finding.kind}",
                file=sys.stderr,
            )
    if total:
        print(
            f"corpus secret check: {total} finding(s); the build stops. Remove the value from "
            f"the document (rotate it if it was real); the matched text is not printed.",
            file=sys.stderr,
        )
        return 1
    print("corpus secret check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
