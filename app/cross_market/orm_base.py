"""Shared SQLAlchemy declarative base for cross-market async models."""
from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class OntologyBase(DeclarativeBase):
    pass
