"""Shared API schemas."""

from typing import Literal

from pydantic import BaseModel, Field


class ApiErrorDetail(BaseModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str


class ApiErrorEnvelope(BaseModel):
    success: Literal[False] = False
    error: ApiErrorDetail
