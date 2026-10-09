"""The corpus baked into the api image (api roadmap 3.4, ADR-0013) — offline.

Docker is not needed: the build-time secret check is tested in test_corpus_secret_scan.py, the
corpus directory the ingest job picks is tested with a temporary directory standing in for
``/app/corpus``, and the Dockerfile, compose file, ignore file and workflows are read as text for
the properties the acceptance names (build context, non-root runtime, no ``.env``, the check
before the copy).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from app.ai import ingest
from app.ai.config import get_ai_settings

API_DIR = Path(__file__).resolve().parents[1]
REPO = API_DIR.parents[1]
DOCKERFILE = API_DIR / "Dockerfile"


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
        # and the allow-list is copied next to it, where the script looks for it
        assert "apps/api/scripts/corpus-secrets-allowlist.txt /work/" in _stages()["corpus"]

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
