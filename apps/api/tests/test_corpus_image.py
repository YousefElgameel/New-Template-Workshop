"""The corpus baked into the api image (api roadmap 3.4, ADR-0013) — offline.

Docker is not needed: the build-time secret check is a stdlib script that is tested directly, the
corpus directory the ingest job picks is tested with a temporary directory standing in for
``/app/corpus``, and the Dockerfile, compose file, ignore file and workflows are read as text for
the properties the acceptance names (build context, non-root runtime, no ``.env``, the check
before the copy).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.ai import ingest
from app.ai.config import get_ai_settings

API_DIR = Path(__file__).resolve().parents[1]
REPO = API_DIR.parents[1]
DOCKERFILE = API_DIR / "Dockerfile"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_corpus_secrets", API_DIR / "scripts" / "check_corpus_secrets.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


# ── which directory the ingest job reads ─────────────────────────────────────


class TestCorpusDirectory:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("CORPUS_DIR", raising=False)
        monkeypatch.delenv("APP_ENV", raising=False)
        image = tmp_path / "app-corpus"
        image.mkdir()
        (image / "a.md").write_text("# A", encoding="utf-8")
        monkeypatch.setattr(ingest, "IMAGE_CORPUS_DIR", image)
        self.image = image

    def _resolved(self) -> Path:
        return ingest.resolve_corpus_dir(get_ai_settings())

    @pytest.mark.parametrize("env", ["prod", "dev", "staging", "Production", " PROD "])
    def test_outside_local_the_image_corpus_is_read(
        self, monkeypatch: pytest.MonkeyPatch, env: str
    ) -> None:
        monkeypatch.setenv("APP_ENV", env)
        assert self._resolved() == self.image

    @pytest.mark.parametrize("env", [None, "", "  ", "local", "LOCAL"])
    def test_local_reads_the_repository_docs_even_when_an_image_corpus_exists(
        self, monkeypatch: pytest.MonkeyPatch, env: str | None
    ) -> None:
        if env is not None:
            monkeypatch.setenv("APP_ENV", env)
        resolved = self._resolved()
        assert resolved != self.image
        assert resolved.name == "docs" and (resolved / "architecture").is_dir()

    def test_without_an_image_corpus_even_a_deployed_environment_reads_docs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("APP_ENV", "prod")
        monkeypatch.setattr(ingest, "IMAGE_CORPUS_DIR", tmp_path / "missing")
        resolved = self._resolved()
        assert resolved.name == "docs" and (resolved / "architecture").is_dir()

    def test_the_setting_wins_over_everything(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        other = tmp_path / "other"
        monkeypatch.setenv("APP_ENV", "prod")
        monkeypatch.setenv("CORPUS_DIR", str(other))
        assert self._resolved() == other

    def test_the_image_path_is_where_the_dockerfile_puts_it(self) -> None:
        assert Path("/app/corpus") == Path(
            re.search(r"COPY --from=corpus\b.* (/app/corpus)\s*$", _dockerfile(), re.M).group(1)  # type: ignore[union-attr]
        )
        source = (API_DIR / "app" / "ai" / "ingest.py").read_text(encoding="utf-8")
        assert 'IMAGE_CORPUS_DIR = Path("/app/corpus")' in source


# ── the secret check ─────────────────────────────────────────────────────────

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
    "slack_token": "xox" + "b-1234567890-abcdefghij",
    "google_api_key": "AI" + "za" + "Sy" + "A" * 33,
    "stripe_key": "sk" + "_live_" + "a" * 24,
    "bearer_token": "Authorization: Bearer " + "a1B2c3D4" * 5,
    "url_credentials": "postgres://app:" + "S3cr3tPassw0rd" + "@db.example.com",
    "keyword_assignment": "api_key = " + "a1b2c3d4e5f6a7b8c9d0e1f2",
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
    "xoxo hugs and kisses",
    "AIzaShort",
    "sk_live is the prefix of a Stripe live key",
]


def _kinds(text: str) -> list[str]:
    return [kind for _, kind in checker.scan_text(text)]


class TestSecretPatterns:
    @pytest.mark.parametrize(("kind", "secret"), sorted(FAKE_SECRETS.items()))
    def test_each_kind_is_found(self, kind: str, secret: str) -> None:
        assert _kinds(f"some text {secret} more text") == [kind]

    def test_all_the_kinds_are_covered_here(self) -> None:
        assert {kind for kind, _ in checker.PATTERNS} == set(FAKE_SECRETS)

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

    def test_a_pgp_private_key_block_is_found(self) -> None:
        assert _kinds("-----BEGIN PGP " + "PRIVATE KEY BLOCK-----") == ["private_key_block"]

    def test_a_documented_example_is_allowed_but_only_that_exact_value(self) -> None:
        assert _kinds("the AWS docs use AKIAIOSFODNN7EXAMPLE") == []
        other = FAKE_SECRETS["aws_access_key_id"]
        assert _kinds(f"AKIAIOSFODNN7EXAMPLE and {other}") == ["aws_access_key_id"]
        assert checker.ALLOWED_EXAMPLES == {"AKIAIOSFODNN7EXAMPLE"}

    def test_one_line_with_two_kinds_reports_both(self) -> None:
        line = f"{FAKE_SECRETS['aws_access_key_id']} {FAKE_SECRETS['jwt']}"
        assert sorted(_kinds(line)) == ["aws_access_key_id", "jwt"]


class TestSecretCheckCommand:
    def _corpus(self, tmp_path: Path, **files: str) -> Path:
        root = tmp_path / "corpus"
        for name, text in files.items():
            path = root / name.replace("__", "/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
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
        assert "ok.md" not in out.replace("notes.md", "")
        # never the matched text, never any part of it, never the line around it
        assert secret not in out and secret[:8] not in out and "the token" not in out

    def test_every_finding_of_every_file_is_listed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = self._corpus(
            tmp_path,
            **{
                "a.md": FAKE_SECRETS["jwt"],
                "b.md": FAKE_SECRETS["private_key_block"],
            },
        )
        assert checker.main([str(root)]) == 1
        err = capsys.readouterr().err
        assert "a.md:1: looks like jwt" in err and "b.md:1: looks like private_key_block" in err
        assert "2 finding(s)" in err

    def test_bytes_that_are_not_utf8_are_read_without_failing(self, tmp_path: Path) -> None:
        root = self._corpus(tmp_path)
        root.mkdir(parents=True, exist_ok=True)
        (root / "latin1.md").write_bytes(b"caf\xe9 and \x80\x81 text")
        assert checker.main([str(root)]) == 0

    def test_a_secret_hidden_in_utf8_garbage_is_still_found(self, tmp_path: Path) -> None:
        root = self._corpus(tmp_path)
        root.mkdir(parents=True, exist_ok=True)
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
        root.mkdir(parents=True, exist_ok=True)
        (root / "wide.md").write_bytes(data)
        assert checker.main([str(root)]) == 1
        assert "wide.md:0: looks like not_plain_text" in capsys.readouterr().err

    def test_a_missing_directory_fails_the_build_instead_of_passing_it(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert checker.main([str(tmp_path / "nowhere")]) == 2
        assert "is not a directory" in capsys.readouterr().err

    def test_no_argument_fails(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert checker.main([]) == 2
        assert "usage" in capsys.readouterr().err

    def test_it_runs_as_a_script_with_the_standard_library_only(self, tmp_path: Path) -> None:
        import subprocess

        root = self._corpus(tmp_path, **{"a.md": FAKE_SECRETS["aws_access_key_id"]})
        script = API_DIR / "scripts" / "check_corpus_secrets.py"
        run = subprocess.run(
            [sys.executable, "-I", str(script), str(root)], capture_output=True, text=True
        )
        assert run.returncode == 1 and "a.md:1: looks like aws_access_key_id" in run.stderr
        imports = re.findall(r"^(?:from|import) (\w+)", script.read_text(encoding="utf-8"), re.M)
        assert set(imports) <= set(sys.stdlib_module_names) | {"__future__"}

    def test_the_repositorys_own_docs_pass(self) -> None:
        """What the image build would check: a secret pasted into a document fails here first.

        Only the files the image keeps: visible ``.md`` files (``openapi.json`` and the wireframe
        ``.html`` are not shipped)."""
        shipped = [
            p
            for p in sorted((REPO / "docs").rglob("*.md"))
            if p.is_file()
            and not any(part.startswith(".") for part in p.relative_to(REPO / "docs").parts)
        ]
        assert shipped, "no markdown found: is the docs directory where the test expects it?"
        for path in shipped:
            assert list(checker.scan_text(path.read_bytes().decode("utf-8", "replace"))) == [], path
            assert b"\x00" not in path.read_bytes() and not path.read_bytes().startswith(
                checker.UTF16_BOMS
            ), path


# ── the image, as text ───────────────────────────────────────────────────────


def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _stages() -> dict[str, str]:
    """Stage name → its instructions, in order (``FROM … AS name`` starts one)."""
    stages: dict[str, list[str]] = {}
    current = ""
    for raw in _dockerfile().replace("\\\n", " ").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"FROM\s+\S+\s+AS\s+(\w+)", line, re.I)
        if match:
            current = match.group(1)
            stages[current] = []
        elif current:
            stages[current].append(line)
    return {name: "\n".join(lines) for name, lines in stages.items()}


class TestDockerfile:
    def test_the_stages_are_builder_corpus_runtime_in_that_order(self) -> None:
        assert list(_stages()) == ["builder", "corpus", "runtime"]

    def test_the_build_context_is_the_repository_root(self) -> None:
        text = _dockerfile()
        assert "BUILD CONTEXT = THE REPOSITORY ROOT" in text
        for line in re.findall(r"^COPY .*$", text, re.M):
            if "--from=" in line:
                continue  # from another stage, not from the context
            sources = [t for t in line.split()[1:-1] if not t.startswith("--")]
            assert sources and all(s == "docs" or s.startswith("apps/api") for s in sources), line
        assert "source=apps/api/uv.lock" in text and "source=apps/api/pyproject.toml" in text

    def test_docs_are_copied_only_into_the_corpus_stage(self) -> None:
        stages = _stages()
        assert "COPY docs /work/docs" in stages["corpus"]
        for name in ("builder", "runtime"):
            assert not re.search(r"^COPY (?!--from)\S*\s*docs\b", stages[name], re.M)

    def test_only_markdown_files_that_are_not_hidden_survive_into_the_corpus(self) -> None:
        corpus = _stages()["corpus"]
        assert "-type f" in corpus and "-name '*.md'" in corpus and "-not -path '*/.*'" in corpus
        # `+`, not `\;`: with `+` a failing cp makes find (and so the build) fail
        assert "-exec cp --parents -t /corpus {} +" in corpus and "\\;" not in corpus

    @pytest.mark.skipif(sys.platform == "win32", reason="needs GNU find and cp, as the image has")
    def test_the_find_command_keeps_exactly_the_visible_markdown_files(
        self, tmp_path: Path
    ) -> None:
        import subprocess

        step = next(line for line in _stages()["corpus"].splitlines() if line.startswith("RUN"))
        command = re.search(r"(find \. .*? \{\} \+)", step).group(1)  # type: ignore[union-attr]
        docs = tmp_path / "docs"
        keep = ["a.md", "sub/b.md", "sub/deep/c d.md", "UPPER.md"]
        drop = [
            ".hidden.md",
            ".dir/x.md",
            "sub/.y.md",
            "n.txt",
            "i.json",
            "p.html",
            "sub/note.markdown",
        ]
        for name in keep + drop:
            path = docs / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# t", encoding="utf-8")
        (docs / "link.md").symlink_to(docs / "a.md")
        out = tmp_path / "corpus"
        out.mkdir()
        run = subprocess.run(
            ["sh", "-c", f"cd {docs} && {command.replace('/corpus', str(out))}"],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stderr
        kept = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
        assert kept == sorted(keep)

    def test_the_secret_check_runs_in_the_corpus_stage_after_the_copy_and_fails_the_build(
        self,
    ) -> None:
        run = next(line for line in _stages()["corpus"].splitlines() if line.startswith("RUN"))
        assert run.index("cp --parents") < run.index("check_corpus_secrets.py")
        # the check is the last command of a && chain: its failure is the step's failure
        assert run.rstrip().endswith("/corpus")
        assert "|| true" not in run and "; python" not in run
        assert "COPY apps/api/scripts/check_corpus_secrets.py" in _stages()["corpus"]

    def test_an_empty_corpus_fails_the_build_too(self) -> None:
        assert 'test -n "$(ls -A /corpus)"' in _stages()["corpus"]

    def test_the_runtime_gets_the_corpus_only_from_the_checked_stage(self) -> None:
        runtime = _stages()["runtime"]
        assert "COPY --from=corpus /corpus /app/corpus" in runtime
        # root-owned: the service reads the corpus and cannot rewrite what the next run embeds
        assert "--chown" not in next(line for line in runtime.splitlines() if "/app/corpus" in line)
        assert "/work/docs" not in runtime

    def test_the_runtime_still_runs_as_a_non_root_user(self) -> None:
        runtime = _stages()["runtime"].splitlines()
        user_lines = [i for i, line in enumerate(runtime) if line.startswith("USER ")]
        assert user_lines and runtime[user_lines[-1]] == "USER appuser"
        # nothing after USER changes who runs the service, and the user is created before it
        assert not any(line.startswith("USER ") for line in runtime[user_lines[-1] + 1 :])
        assert any("useradd" in line for line in runtime[: user_lines[-1]])
        assert runtime[-1].startswith("CMD")

    def test_no_env_file_is_ever_copied_into_the_image(self) -> None:
        for line in re.findall(r"^COPY .*$", _dockerfile(), re.M):
            assert ".env" not in line, line

    def test_the_image_does_not_set_the_corpus_directory_itself(self) -> None:
        # APP_ENV decides (app/ai/ingest.py): a CORPUS_DIR baked in would also apply under compose.
        joined = _dockerfile().replace("\\\n", " ")  # an ENV may continue over several lines
        assert not re.search(r"^\s*(?:ENV|ARG)\b.*(?<![A-Z_])CORPUS_DIR\b", joined, re.M)

    def test_the_deployed_default_environment_is_not_local(self) -> None:
        assert "APP_ENV=prod" in _stages()["runtime"]


class TestBuildWiring:
    def test_the_root_dockerignore_keeps_env_files_and_the_rest_out(self) -> None:
        lines = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
        for pattern in (
            "**/.env",
            "**/.env.*",
            ".git/",
            "**/node_modules/",
            "**/.venv/",
            "apps/web/",
        ):
            assert pattern in lines
        # nothing re-includes a real env file or the docs the corpus needs
        negations = [line for line in lines if line.startswith("!")]
        assert negations == ["!**/.env.example"]
        assert "docs/" not in lines and "docs" not in lines and "**/docs/" not in lines

    def test_compose_builds_the_api_from_the_repository_root(self) -> None:
        compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
        api = compose.split("\n  api:\n", 1)[1].split("\n  web:\n", 1)[0]
        assert re.search(r"^\s+context: \.\s*$", api, re.M)
        assert re.search(r"^\s+dockerfile: apps/api/Dockerfile\s*$", api, re.M)
        assert "context: ./apps/api" not in compose

    @pytest.mark.parametrize("workflow", ["ci.yml", "infra.yml"])
    def test_the_workflows_build_with_the_same_dockerfile_and_context(self, workflow: str) -> None:
        text = (REPO / ".github" / "workflows" / workflow).read_text(encoding="utf-8")
        builds = [
            line
            for line in text.splitlines()
            if "docker build" in line and "apps/web" not in line and ("api" in line)
        ]
        assert builds, workflow
        for line in builds:
            assert "--file apps/api/Dockerfile" in line and line.rstrip().endswith(" .")
        assert not re.search(r"docker build [^\n]* apps/api\s*$", text, re.M)
