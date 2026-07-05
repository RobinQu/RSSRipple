"""Filter DSL Pydantic schemas for the BoolCondition/FieldCondition tree."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Combinator = Literal["and", "or"]
FieldName = Literal[
    "subtitle_group", "resolution", "source", "video_codec",
    "audio_codec", "subtitle_type", "container", "file_size",
    "episode", "season", "title_cn", "title_en", "search_title",
]
Operator = Literal[
    "eq", "ne", "contains", "fuzzy", "in", "regex",
    "gt", "gte", "lt", "lte",
]
FieldValue = str | int | float | list[str | int | float]


class FieldCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    operator: str
    value: Any = None


class BoolCondition(BaseModel):
    """Recursive boolean condition tree.

    ``conditions`` can contain either BoolCondition or FieldCondition dicts.
    """
    model_config = ConfigDict(extra="forbid")

    combinator: Combinator = "and"
    conditions: list[BoolCondition | FieldCondition] = Field(default_factory=list)
    is_not: bool = False

    @model_validator(mode="before")
    @classmethod
    def _accept_raw(cls, v: Any) -> Any:
        """Allow plain dicts from JSON payloads without extra validation."""
        return v


# Rebuild forward references
BoolCondition.model_rebuild()
