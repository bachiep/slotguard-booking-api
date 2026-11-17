"""RFC 7807 Problem Details schema and FastAPI exception handlers."""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from slotguard.core.clock import Clock
from slotguard.core.exceptions import AppError

logger = logging.getLogger(__name__)


class ProblemDetail(BaseModel):
    """RFC 7807 standardized error payload."""

    type: str
    title: str
    status: int
    detail: str
    instance: str
    timestamp: str
    errors: list[dict[str, Any]] | None = None


def _build_problem_response(
    *,
    request: Request,
    clock: Clock,
    status_code: int,
    title: str,
    detail: str,
    error_type: str,
    headers: dict[str, str] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    payload = ProblemDetail(
        type=error_type,
        title=title,
        status=status_code,
        detail=detail,
        instance=request.url.path,
        timestamp=clock.now().isoformat(),
        errors=errors,
    )
    return JSONResponse(
        status_code=status_code,
        content=payload.model_dump(exclude_none=True),
        media_type="application/problem+json",
        headers=headers,
    )


def register_exception_handlers(app: FastAPI, clock: Clock) -> None:
    """Attach consistent RFC 7807 exception handlers to the FastAPI application."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return _build_problem_response(
            request=request,
            clock=clock,
            status_code=exc.status_code,
            title=exc.title,
            detail=exc.detail,
            error_type=exc.error_type,
            headers=exc.headers,
            errors=exc.errors,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        sanitized_errors = [
            {
                "loc": list(err.get("loc", ())),
                "msg": str(err.get("msg", "Invalid value")),
                "type": str(err.get("type", "value_error")),
            }
            for err in exc.errors()
        ]
        return _build_problem_response(
            request=request,
            clock=clock,
            status_code=422,
            title="Validation Error",
            detail="Request validation failed.",
            error_type="https://slotguard.dev/errors/validation-error",
            errors=sanitized_errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request,
        exc: StarletteHTTPException,
    ) -> JSONResponse:
        try:
            title = HTTPStatus(exc.status_code).phrase
        except ValueError:
            title = "HTTP Error"
        detail = exc.detail if isinstance(exc.detail, str) else title
        return _build_problem_response(
            request=request,
            clock=clock,
            status_code=exc.status_code,
            title=title,
            detail=detail,
            error_type=f"https://slotguard.dev/errors/http-{exc.status_code}",
            headers=dict(exc.headers) if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return _build_problem_response(
            request=request,
            clock=clock,
            status_code=500,
            title="Internal Server Error",
            detail="An unexpected internal error occurred.",
            error_type="https://slotguard.dev/errors/internal-error",
        )
