"""Shared API response models for OptionsAji 2.0."""

from __future__ import annotations

from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, Field

DataT = TypeVar("DataT")


class ApiError(BaseModel):
    """Structured API error payload."""

    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    details: Optional[dict[str, object]] = None
    request_id: Optional[str] = None


class ApiSuccess(BaseModel, Generic[DataT]):
    """Structured API success payload."""

    success: bool = True
    data: DataT


class ApiFailure(BaseModel):
    """Structured API failure payload."""

    success: bool = False
    error: ApiError
