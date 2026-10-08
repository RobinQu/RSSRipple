"""Common Pydantic schemas for API responses and pagination."""

from datetime import datetime
from typing import Annotated, Any, Generic, TypeVar

from fastapi.encoders import jsonable_encoder
from pydantic import AfterValidator, BaseModel, ConfigDict

from app.utils.time import naive_utc, utc_isoformat

T = TypeVar("T")
NaiveUTCDateTime = Annotated[datetime, AfterValidator(naive_utc)]


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


def api_json(data: Any) -> Any:
    """Encode typed timestamps before model JSON dumping loses their type.

    Existing strings, including frozen snapshots and user content, are opaque.
    Keep FastAPI's standard encoding for dates, secrets and other JSON types.
    """
    return jsonable_encoder(data, custom_encoder={
        datetime: utc_isoformat,
        BaseModel: lambda model: api_json(model.model_dump(mode="python", by_alias=True)),
    })


def success_response(data: Any = None, meta: dict | None = None) -> dict:
    return {"success": True, "data": api_json(data), "error": None, "meta": api_json(meta or {})}


def error_response_dict(code: str, message: str, details: dict | None = None) -> dict:
    return {"success": False, "data": None,
            "error": {"code": code, "message": message, "details": api_json(details)}, "meta": {}}


def paginated_response(items: list, total: int, page: int, page_size: int) -> dict:
    return success_response(items, meta={"page": page, "page_size": page_size, "total": total})
