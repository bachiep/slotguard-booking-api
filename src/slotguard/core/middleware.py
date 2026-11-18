"""ASGI request correlation middleware and structured request_id log binding."""

import logging
import time
import uuid
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from slotguard.core.metrics import SlotGuardMetrics

logger = logging.getLogger("slotguard.http")

_request_id_ctx: ContextVar[str | None] = ContextVar("slotguard_request_id", default=None)
_log_factory_installed = False


def get_request_id() -> str | None:
    """Return the active request correlation ID from contextvars, if any."""
    return _request_id_ctx.get()


class RequestIdLogFilter(logging.Filter):
    """Attach the active request_id to every LogRecord emitted under slotguard.*."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


def configure_structured_logging(log_level: str) -> None:
    """Ensure slotguard loggers bind request_id cleanly without duplicating handlers."""
    global _log_factory_installed
    pkg_logger = logging.getLogger("slotguard")
    level = getattr(logging, log_level.upper(), logging.INFO)
    pkg_logger.disabled = False
    pkg_logger.setLevel(level)
    logger.disabled = False

    if not any(isinstance(flt, RequestIdLogFilter) for flt in pkg_logger.filters):
        pkg_logger.addFilter(RequestIdLogFilter())

    if not _log_factory_installed:
        previous_factory = logging.getLogRecordFactory()

        def _request_id_record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
            record = previous_factory(*args, **kwargs)
            if not hasattr(record, "request_id"):
                record.request_id = get_request_id() or "-"
            return record

        logging.setLogRecordFactory(_request_id_record_factory)
        _log_factory_installed = True


def _resolve_request_id(headers: Headers) -> str:
    candidate = headers.get("x-request-id")
    if candidate is not None:
        cleaned = candidate.strip()
        if 1 <= len(cleaned) <= 128 and all(32 <= ord(ch) < 127 for ch in cleaned):
            return cleaned
    return str(uuid.uuid4())


class RequestContextMiddleware:
    """Pure ASGI middleware propagating X-Request-Id, timing headers, and request metrics."""

    def __init__(self, app: ASGIApp, *, metrics: SlotGuardMetrics | None = None) -> None:
        self.app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(Headers(scope=scope))
        state = scope.setdefault("state", {})
        state["request_id"] = request_id

        token = _request_id_ctx.set(request_id)
        start_perf = time.perf_counter()
        status_code = 500
        response_started = False
        method = scope.get("method", "GET")
        path = scope.get("path", "/")

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                elapsed_ms = (time.perf_counter() - start_perf) * 1000.0
                mutable_headers = MutableHeaders(scope=message)
                mutable_headers["X-Request-Id"] = request_id
                mutable_headers["X-Response-Time-Ms"] = f"{elapsed_ms:.2f}"
                if self._metrics is not None:
                    self._metrics.record_request(status_code, elapsed_ms)
                logger.info(
                    "http_request method=%s path=%s status=%s duration_ms=%.2f request_id=%s",
                    method,
                    path,
                    status_code,
                    elapsed_ms,
                    request_id,
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            if not response_started:
                elapsed_ms = (time.perf_counter() - start_perf) * 1000.0
                if self._metrics is not None:
                    self._metrics.record_request(500, elapsed_ms)
                logger.exception(
                    "http_request_error method=%s path=%s duration_ms=%.2f request_id=%s",
                    method,
                    path,
                    elapsed_ms,
                    request_id,
                )
            raise
        finally:
            _request_id_ctx.reset(token)
