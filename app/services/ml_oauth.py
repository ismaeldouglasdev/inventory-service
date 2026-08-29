"""Persistência de tokens OAuth do Mercado Livre no ChannelState.

O token em memória (``MLTokenStore``) é perdido a cada restart. Este módulo
persiste access/refresh tokens no banco (linha ``channel_state`` com
``channel="mercadolivre"``) para que a autenticação sobreviva a restarts
sem o usuário precisar reautorizar.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database import async_session_factory
from app.models.channel_state import ChannelState

logger = logging.getLogger(__name__)

CHANNEL = "mercadolivre"


async def save_ml_tokens(data: dict[str, Any]) -> None:
    """Persiste access/refresh token (e metadados) na linha do canal."""
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")

    if not access_token:
        logger.warning("ML: save_ml_tokens sem access_token; nada persistido")
        return

    user_id = data.get("user_id")
    expires_in = data.get("expires_in", 0)
    expires_at = (
        datetime.now(timezone.utc).timestamp() + int(expires_in)
        if expires_in
        else None
    )

    async with async_session_factory() as session:
        result = await session.execute(
            select(ChannelState).where(ChannelState.channel == CHANNEL)
        )
        state = result.scalar_one_or_none()
        if state is None:
            state = ChannelState(channel=CHANNEL)
            session.add(state)

        state.access_token = access_token
        state.refresh_token = refresh_token
        if user_id is not None:
            state.ml_user_id = int(user_id)
        if expires_at is not None:
            state.token_expires_at = datetime.fromtimestamp(
                expires_at, tz=timezone.utc
            )
        state.updated_at = datetime.now(timezone.utc)

        await session.commit()
        logger.info("ML: tokens persistidos no banco (user_id=%s)", user_id)


async def load_ml_tokens() -> dict[str, Any]:
    """Lê do banco os tokens persistidos (vazios se não houver)."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(ChannelState).where(ChannelState.channel == CHANNEL)
        )
        state = result.scalar_one_or_none()
        if state is None or not state.access_token:
            return {}

        expires_at = 0.0
        if state.token_expires_at is not None:
            expires_at = state.token_expires_at.timestamp()

        return {
            "access_token": state.access_token,
            "refresh_token": state.refresh_token or "",
            "user_id": state.ml_user_id or 0,
            "expires_at": expires_at,
        }
