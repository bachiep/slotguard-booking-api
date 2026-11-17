"""Domain exception hierarchy mapped to RFC 7807 Problem Details."""

from typing import Any


class AppError(Exception):
    """Base application exception rendered as RFC 7807 ProblemDetail."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int = 400,
        title: str = "Bad Request",
        error_type: str = "https://slotguard.dev/errors/bad-request",
        headers: dict[str, str] | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.title = title
        self.error_type = error_type
        self.headers = headers
        self.errors = errors


class UnauthorizedError(AppError):
    """Authentication failure or missing/invalid credentials."""

    def __init__(self, detail: str = "Authentication credentials are invalid or missing.") -> None:
        super().__init__(
            detail,
            status_code=401,
            title="Unauthorized",
            error_type="https://slotguard.dev/errors/unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


class ForbiddenError(AppError):
    """Authenticated caller lacks permission for the requested action."""

    def __init__(self, detail: str = "You do not have permission to perform this action.") -> None:
        super().__init__(
            detail,
            status_code=403,
            title="Forbidden",
            error_type="https://slotguard.dev/errors/forbidden",
        )


class NotFoundError(AppError):
    """Requested domain entity does not exist."""

    def __init__(self, detail: str = "Requested resource was not found.") -> None:
        super().__init__(
            detail,
            status_code=404,
            title="Not Found",
            error_type="https://slotguard.dev/errors/not-found",
        )


class ConflictError(AppError):
    """State conflict such as overlapping booking interval or duplicate record."""

    def __init__(self, detail: str = "The request conflicts with current state.") -> None:
        super().__init__(
            detail,
            status_code=409,
            title="Conflict",
            error_type="https://slotguard.dev/errors/conflict",
        )


class BusinessRuleError(AppError):
    """Input passes schema syntax but violates domain invariants."""

    def __init__(self, detail: str) -> None:
        super().__init__(
            detail,
            status_code=422,
            title="Unprocessable Entity",
            error_type="https://slotguard.dev/errors/business-rule-violation",
        )


class RateLimitExceededError(AppError):
    """Caller exceeded the sliding-window request budget."""

    def __init__(
        self,
        detail: str = "Rate limit exceeded. Please retry later.",
        *,
        limit: int,
        remaining: int,
        retry_after_seconds: int,
    ) -> None:
        super().__init__(
            detail,
            status_code=429,
            title="Too Many Requests",
            error_type="https://slotguard.dev/errors/rate-limit-exceeded",
            headers={
                "Retry-After": str(retry_after_seconds),
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": str(remaining),
            },
        )
