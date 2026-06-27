"""Alembic environment configuration — async-compatible.

Uses the application's async engine to run migrations, but falls back
to a sync URL for the offline ``stamp`` / ``current`` commands.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.database import Base

# Import ALL models so Alembic's autogenerate can detect them
from app.models import (  # noqa: F401  isort:skip
    ProductMapping,
    ChannelProductMapping,
    ChannelVariantMapping,
    ChannelFeeConfig,
    ChannelState,
    EventStore,
    OnboardingSession,
    OnboardingImage,
    StoreProduct,
    InventoryState,
    ProcessedAction,
)

# ── Alembic Config ─────────────────────────────────────────────────────
config = context.config

# Override sqlalchemy.url with the async URL from application settings
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

# Set up Python logging from the INI file
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Target metadata for autogenerate
target_metadata = Base.metadata


# ── Sync (offline) runner ──────────────────────────────────────────────
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (e.g. ``--sql``).

    Configures the context with just a URL and not an Engine, though an
    Engine is acceptable here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# ── Async (online) runner ──────────────────────────────────────────────
def do_run_migrations(connection):
    """Inner sync migration runner bound to a connection."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations."""
    connectable = create_async_engine(
        settings.database_url_async,
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — delegates to the async runner."""
    asyncio.run(run_async_migrations())


# ── Dispatch ───────────────────────────────────────────────────────────
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
