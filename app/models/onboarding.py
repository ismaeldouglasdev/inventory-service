"""Placeholder models for AI-powered onboarding (Phase 4).

These are basic stubs. Full logic (image upload, LLM classification,
attribute extraction) will be implemented in Phase 4.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OnboardingSession(Base):
    """Tracks a batch onboarding process for one or more products.

    TODO: implement in Phase 4 — AI Onboarding & Enrichment
    """

    __tablename__ = "onboarding_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )  # pending|processing|completed|failed
    images_processed: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )  # JSON blob with extracted attributes
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<OnboardingSession id={self.id} sku={self.sku!r} "
            f"status={self.status!r}>"
        )


class OnboardingImage(Base):
    """Individual image uploaded as part of an onboarding session.

    TODO: implement in Phase 4 — AI Onboarding & Enrichment
    """

    __tablename__ = "onboarding_images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), default="image/jpeg")
    analysis_result: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )  # JSON from LLM
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<OnboardingImage id={self.id} "
            f"filename={self.filename!r}>"
        )
