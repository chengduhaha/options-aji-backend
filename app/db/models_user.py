"""User accounts — email/password + JWT session."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    email: Mapped[str] = mapped_column(String(256), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=True,
    )
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verification_token: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)


class UserEmailVerificationRow(Base):
    __tablename__ = "user_email_verifications"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=True,
    )
