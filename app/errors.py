"""Application error types mapped to the `{error:{code,message}}` envelope."""

from __future__ import annotations


class AppError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class NotFound(AppError):
    status_code = 404
    code = "NOT_FOUND"

    def __init__(self, message: str = "resource not found") -> None:
        super().__init__(message)


class Unauthorized(AppError):
    status_code = 401
    code = "UNAUTHORIZED"

    def __init__(self, message: str = "authentication required") -> None:
        super().__init__(message)


class Forbidden(AppError):
    status_code = 403
    code = "FORBIDDEN"

    def __init__(self, message: str = "insufficient permissions") -> None:
        super().__init__(message)


class Conflict(AppError):
    status_code = 409
    code = "CONFLICT"


class Unprocessable(AppError):
    status_code = 422
    code = "UNPROCESSABLE"


class RateLimited(AppError):
    status_code = 429
    code = "RATE_LIMITED"


class UpstreamError(AppError):
    status_code = 502
    code = "UPSTREAM_ERROR"
