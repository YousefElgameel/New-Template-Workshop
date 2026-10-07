"""AI runtime configuration (ADR-0009) — read from environment variables.

Nothing here is a secret except ``AZURE_AI_API_KEY`` / ``AZURE_SEARCH_API_KEY``, which exist for
LOCAL DEV ONLY: deployed apps authenticate with their managed identity
(``AZURE_AI_AUTH_MODE=managed_identity``; ``DefaultAzureCredential`` picks the user-assigned
identity from ``AZURE_CLIENT_ID``, which the use-case Bicep sets on the Container App).

Like ``app.config``, this is a snapshot: call ``get_ai_settings()`` when you need it, never
cache it at import (tests monkeypatch the environment).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

AUTH_MODES = ("managed_identity", "api_key")
DEFAULT_API_VERSION = "2024-10-21"
DEFAULT_TIMEOUT_SECONDS = 60.0
# One retry, not two: with the model timeout above and the retrieval steps below, one answer's worst
# case is then 180 s, inside the ingress request timeout (see ``AISettings.worst_case_seconds``).
DEFAULT_MAX_RETRIES = 1
# Azure Container Apps ingress ends a request after this long and the setting cannot be changed
# (https://learn.microsoft.com/en-us/azure/container-apps/ingress-overview).
INGRESS_REQUEST_TIMEOUT_SECONDS = 240.0
# What one answer's worst case may add up to: the ingress limit less a margin for what the sum does
# not count: reading the history and storing the question before the model is called, and storing
# the answer and sending ``done`` after it. A configuration above this logs a warning at startup.
ASK_WORST_CASE_LIMIT_SECONDS = 220.0
# The longest answer the model may write, in tokens (api 4.2). ~4 characters per token in
# English, so 1024 fits the 4,000-character cap on a stored answer.
DEFAULT_MAX_OUTPUT_TOKENS = 1024
# Each retrieval step (the query embedding, then the search) gets this long, in seconds.
DEFAULT_RETRIEVAL_TIMEOUT_SECONDS = 30.0
# Asks that may run at once in this process; one more is refused with 429 (api 4.2).
DEFAULT_MAX_CONCURRENT_ASKS = 4
# text-embedding-3-small / ada-002 produce 1536-dimensional vectors; large = 3072.
DEFAULT_EMBEDDING_DIMENSIONS = 1536


class AINotConfigured(RuntimeError):
    """A required AI setting is missing — the feature cannot run in this deployment."""


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _float_env(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _positive_int_env(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def _bool_env(name: str) -> bool:
    return (_env(name) or "").lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AISettings:
    """Immutable snapshot of the AI-related environment."""

    endpoint: str | None
    chat_deployment: str | None
    embedding_deployment: str | None
    # Vector size of the embedding deployment (index definition + query vectors).
    embedding_dimensions: int
    api_version: str
    auth_mode: str
    api_key: str | None
    search_endpoint: str | None
    search_index: str | None
    search_api_key: str | None
    request_timeout_seconds: float
    max_retries: int
    allow_text_to_sql: bool
    # Directory of the `.md` corpus to ingest; None = the repository's docs/ (decision 1).
    corpus_dir: str | None = None
    # Output bound for the chat model, retrieval step timeout, concurrent-ask cap (api 4.2).
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    retrieval_timeout_seconds: float = DEFAULT_RETRIEVAL_TIMEOUT_SECONDS
    max_concurrent_asks: int = DEFAULT_MAX_CONCURRENT_ASKS

    @property
    def worst_case_seconds(self) -> float:
        """The longest one answer can take: the model's timeout for every attempt (the first and
        each retry) plus the two retrieval steps (the query embedding, then the search). The one
        place this is worked out — the stream's deadline, the JSON route's deadline, the startup
        check and the tests use it. Each retrieval step has its own asyncio timeout, so retries
        inside the embedding client cannot stretch the ``2 *`` term. It assumes the answer graph is
        retrieve then one model call (no tool loop): add tools and it is a lower bound."""
        attempts = self.max_retries + 1
        return self.request_timeout_seconds * attempts + 2 * self.retrieval_timeout_seconds

    @property
    def fits_the_ingress_limit(self) -> bool:
        return self.worst_case_seconds <= ASK_WORST_CASE_LIMIT_SECONDS

    @property
    def has_model(self) -> bool:
        return bool(self.endpoint and self.chat_deployment)

    @property
    def has_embeddings(self) -> bool:
        return bool(self.endpoint and self.embedding_deployment)

    @property
    def has_search(self) -> bool:
        return bool(self.search_endpoint and self.search_index)

    def require_model(self) -> None:
        if not self.has_model:
            raise AINotConfigured(
                "AZURE_AI_ENDPOINT and AZURE_AI_DEPLOYMENT must be set to use the chat model"
            )

    def require_embeddings(self) -> None:
        if not self.has_embeddings:
            raise AINotConfigured(
                "AZURE_AI_ENDPOINT and AZURE_AI_EMBEDDING_DEPLOYMENT must be set to use embeddings"
            )

    def require_search(self) -> None:
        if not self.has_search:
            raise AINotConfigured(
                "AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_INDEX must be set to use retrieval"
            )


def get_ai_settings() -> AISettings:
    """Build a settings snapshot from the current environment.

    Raises ``ValueError`` for an unknown ``AZURE_AI_AUTH_MODE`` — a mistyped value must fail
    visibly, never fall back to a key or to anonymous access.
    """
    auth_mode = (_env("AZURE_AI_AUTH_MODE") or "managed_identity").lower()
    if auth_mode not in AUTH_MODES:
        raise ValueError(
            f'invalid AZURE_AI_AUTH_MODE "{auth_mode}" — expected one of {", ".join(AUTH_MODES)}'
        )
    max_retries_raw = _env("AI_MAX_RETRIES")
    try:
        max_retries = int(max_retries_raw) if max_retries_raw else DEFAULT_MAX_RETRIES
    except ValueError:
        max_retries = DEFAULT_MAX_RETRIES
    dims_raw = _env("AZURE_AI_EMBEDDING_DIMENSIONS")
    try:
        dims = int(dims_raw) if dims_raw else DEFAULT_EMBEDDING_DIMENSIONS
    except ValueError:
        dims = DEFAULT_EMBEDDING_DIMENSIONS
    return AISettings(
        endpoint=_env("AZURE_AI_ENDPOINT"),
        chat_deployment=_env("AZURE_AI_DEPLOYMENT"),
        embedding_deployment=_env("AZURE_AI_EMBEDDING_DEPLOYMENT"),
        embedding_dimensions=dims if dims > 0 else DEFAULT_EMBEDDING_DIMENSIONS,
        api_version=_env("AZURE_AI_API_VERSION") or DEFAULT_API_VERSION,
        auth_mode=auth_mode,
        api_key=_env("AZURE_AI_API_KEY"),
        search_endpoint=_env("AZURE_SEARCH_ENDPOINT"),
        search_index=_env("AZURE_SEARCH_INDEX"),
        search_api_key=_env("AZURE_SEARCH_API_KEY"),
        request_timeout_seconds=_float_env("AI_REQUEST_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
        max_retries=max(0, max_retries),
        allow_text_to_sql=_bool_env("AI_ALLOW_TEXT_TO_SQL"),
        corpus_dir=_env("CORPUS_DIR"),
        max_output_tokens=_positive_int_env("AI_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS),
        retrieval_timeout_seconds=_float_env(
            "AI_RETRIEVAL_TIMEOUT_SECONDS", DEFAULT_RETRIEVAL_TIMEOUT_SECONDS
        ),
        max_concurrent_asks=_positive_int_env(
            "AI_MAX_CONCURRENT_ASKS", DEFAULT_MAX_CONCURRENT_ASKS
        ),
    )
