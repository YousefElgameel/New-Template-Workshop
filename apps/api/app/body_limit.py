"""Request-size cap for the ``/v1`` surface (roadmap api 5.5; threat model S3, D-class).

FastAPI reads and parses a request body in full before a route's own check runs, so a ``413`` for a
too-long question would arrive only after the body sat in memory several times over. This
pure-ASGI middleware refuses an oversized body *before* it is buffered:

* a ``Content-Length`` above the limit is answered ``413 payload_too_large`` at once, without
  reading the body;
* a body that declares no length (chunked) is counted as it streams; the read that crosses the limit
  raises ``BodyTooLarge``, an ``HTTPException`` that FastAPI re-raises and the error contract turns
  into the same ``413`` (with the request's trace id in the body and the echoed header).

Every route is limited except the operational ones (``/ping``, ``/info``, ``/health``), which take
no body — so ``/v1``, the next API version and anything added later are covered without a list to
maintain, and a server started with a ``root_path`` cannot slip past it. The limit is
``MAX_REQUEST_BODY_BYTES`` (default 64 KB; the largest legitimate body is a 4,000-character
question, about 48 KB in the worst JSON-escaped case; the web BFF caps its own requests at the same
64 KiB). A mistyped value, or one above 1 MiB, is the default: a typo must never switch the cap off.
A repeated ``Content-Length`` counts as its largest value.

The cap bounds size, not speed: a client that sends bytes slowly is bounded only by the ingress's
request timeout (uvicorn has no read deadline).

Logs carry the kind of refusal (declared or streamed) at info level, never the size, the path or
any content; the refusal is also a 413 in the request metrics.
"""

from __future__ import annotations

from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import DEFAULT_MAX_REQUEST_BODY_BYTES
from app.errors import error_response
from app.logging_config import get_logger

# The operational routes take no body: they are the only ones left out. Everything else — every
# /v1 route, the next API version, anything added later — is capped.
UNLIMITED_PATHS = frozenset({"/ping", "/info", "/health"})
CODE = "payload_too_large"

_log = get_logger("app.body_limit")


class BodyTooLarge(StarletteHTTPException):
    """Raised by the receive wrapper when a streamed body crosses the limit."""

    def __init__(self) -> None:
        super().__init__(status_code=413, detail=CODE)


def _route_path(scope: Scope) -> str:
    """The path the router matches on: ``scope['path']`` without the server's ``root_path``
    (uvicorn puts the two together, and a prefix deployment must not slip past the cap)."""
    path = str(scope.get("path", ""))
    root = str(scope.get("root_path", ""))
    if root and path.startswith(root):
        path = path[len(root) :]
    return path or "/"


def _declared_length(scope: Scope) -> int | None:
    """The largest valid ``Content-Length`` of the request (a repeated header must not hide one)."""
    declared: int | None = None
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                number = int(value)
            except ValueError:
                continue
            declared = number if declared is None else max(declared, number)
    return declared


class BodyLimitMiddleware:
    """Refuse a ``/v1`` request body larger than ``max_bytes`` without buffering it."""

    def __init__(self, app: ASGIApp, max_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _route_path(scope) in UNLIMITED_PATHS:
            await self.app(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and declared > self.max_bytes:
            _log.info("request body refused", kind="declared")
            # The body is not read: tell the client the connection ends with this answer.
            response = error_response(CODE, 413, headers={"connection": "close"})
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    _log.info("request body refused", kind="streamed")
                    raise BodyTooLarge()
            return message

        await self.app(scope, limited_receive, send)
