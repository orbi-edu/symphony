"""Uniform API errors: `{"error": {"code", "message"}}` with the HTTP status that fits."""

from __future__ import annotations

from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, headers: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def error_response(
    status: int, code: str, message: str, headers: dict | None = None, *, diagnostics=None
):
    detail = {"code": code, "message": message}
    if diagnostics:
        detail["diagnostics"] = diagnostics
    return JSONResponse(status_code=status, content={"error": detail}, headers=headers)


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def invalid(message: str) -> ApiError:
    return ApiError(422, "invalid_request", message)
