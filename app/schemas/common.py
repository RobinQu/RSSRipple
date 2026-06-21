"""Common Pydantic schemas for API responses and pagination."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str


class APIResponse(BaseModel, Generic[T]):
    success: bool
    data: T | None = None
    error: ErrorDetail | None = None
    meta: dict[str, Any] | None = None


class PaginationParams(BaseModel):
    page: int = 1
    page_size: int = 20


class PaginatedMeta(BaseModel):
    page: int
    page_size: int
    total: int


def success_response(data: Any = None, meta: dict | None = None) -> dict:
    return {"success": True, "data": data, "error": None, "meta": meta}


def error_response(code: str, message: str, status_code: int = 400) -> tuple:
    return (
        status_code,
        {"success": False, "data": None, "error": {"code": code, "message": message}},
    )


def paginated_response(
    items: list, total: int, page: int, page_size: int
) -> dict:
    return {
        "success": True,
        "data": items,
        "error": None,
        "meta": {"page": page, "page_size": page_size, "total": total},
    }
