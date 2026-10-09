"""The admin key that guards the operational routes (ADR-0015; api 3.3; threat model S5, G8).

The platform has no user auth yet, so an admin route is guarded by a shared secret: the
``x-admin-key`` header must equal ``INGEST_ADMIN_KEY``. The guard **fails closed**: a key that is
not set, or shorter than ``ADMIN_KEY_MIN_LENGTH`` characters, is treated as not set and every call
is ``401``. The comparison is constant-time (both sides are hashed first, so a different length
does not show either). The key — and its length — is never logged: a refusal logs one reason kind.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Annotated, Literal

from fastapi import Header

from app.errors import ApiError
from app.logging_config import get_logger

ADMIN_KEY_ENV = "INGEST_ADMIN_KEY"
# A short key is guessable; this is a default chosen with the threat model (G8), not a standard.
ADMIN_KEY_MIN_LENGTH = 32

KeyProblem = Literal["not_set", "too_short"]

_log = get_logger("app.admin")
# Each configuration problem is warned about once per process, not on every refused call.
_warned: set[str] = set()
# A refused call is logged at most once per reason per this many seconds: a caller without the key
# must not be able to fill the log (and Azure Monitor's bill) with a line per request. The count of
# the ones left out is kept and reported on the next line.
REFUSAL_LOG_INTERVAL_SECONDS = 60.0
_last_refusal_log: dict[str, float] = {}
_suppressed: dict[str, int] = {}


def _configured_key() -> tuple[str | None, KeyProblem | None]:
    key = (os.getenv(ADMIN_KEY_ENV) or "").strip()
    if not key:
        return None, "not_set"
    if len(key) < ADMIN_KEY_MIN_LENGTH:
        return None, "too_short"
    return key, None


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def _log_refusal(reason: str) -> None:
    now = time.monotonic()
    last = _last_refusal_log.get(reason)
    if last is not None and now - last < REFUSAL_LOG_INTERVAL_SECONDS:
        _suppressed[reason] = _suppressed.get(reason, 0) + 1
        return
    _last_refusal_log[reason] = now
    _log.info("admin call refused", reason=reason, also_refused=_suppressed.pop(reason, 0))


def _unauthorized() -> ApiError:
    return ApiError("unauthorized", status_code=401)


async def require_admin_key(
    x_admin_key: Annotated[str | None, Header(include_in_schema=False)] = None,
) -> None:
    """Dependency: ``401 unauthorized`` unless the header carries the configured admin key."""
    key, problem = _configured_key()
    if key is None:
        if problem is not None and problem not in _warned:
            _warned.add(problem)
            # The reason kind only: never the key, never its length.
            _log.warning(
                "admin key is not usable, so admin routes refuse every call", reason=problem
            )
        raise _unauthorized()
    if x_admin_key is None:
        _log_refusal("no_key")
        raise _unauthorized()
    if not hmac.compare_digest(_digest(x_admin_key), _digest(key)):
        _log_refusal("wrong_key")
        raise _unauthorized()


def _reset_warnings_for_tests() -> None:
    """TEST-ONLY: forget which configuration problems were already warned about."""
    _warned.clear()
    _last_refusal_log.clear()
    _suppressed.clear()
