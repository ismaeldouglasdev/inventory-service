"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

# ── Engine ─────────────────────────────────────────────────────────────
engine = create_async_engine(
    settings.database_url_async,
    echo=settings.log_level.upper() == "DEBUG",
    future=True,
)

# ── Session factory ────────────────────────────────────────────────────
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# ── Dependency generator ───────────────────────────────────────────────
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session."""
    async with async_session_factory() as session:
        try:
            yield session
            if session.dirty or session.new:
                await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Declarative base ───────────────────────────────────────────────────
class Base(DeclarativeBase):
    """Base class for all mapped models."""
