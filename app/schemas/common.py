"""Common Pydantic schemas for API responses and pagination."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict | None = None


class APIResponse(BaseModel, Generic[T]):  # noqa: UP046 — Pydantic v2 doesn't support PEP-695 generics
    success: bool = True
    data: T | None = None
    error: ErrorDetail | None = None
    meta: dict[str, Any] = {}


class PaginatedMeta(BaseModel):
    page: int
    page_size: int
    total: int


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


def success_response(data: Any = None, meta: dict | None = None) -> dict:
    return {"success": True, "data": data, "error": None, "meta": meta or {}}


def error_response_dict(code: str, message: str, details: dict | None = None) -> dict:
    return {"success": False, "data": None, "error": {"code": code, "message": message, "details": details}, "meta": {}}


def paginated_response(items: list, total: int, page: int, page_size: int) -> dict:
    return {
        "success": True,
        "data": items,
        "error": None,
        "meta": {"page": page, "page_size": page_size, "total": total},
    }
