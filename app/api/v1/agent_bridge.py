"""Agent Bridge — comunicação entre agentes OpenCode via HTTP.

Permite que dois agentes OpenCode (ex: Sisyphus nesta máquina e
outro agente no PC da loja) troquem mensagens, arquivos e instruções
usando uma fila simples em memória + disco.

Arquitetura:
  ┌──────────────┐   HTTP    ┌──────────────────┐   HTTP    ┌──────────────┐
  │  Sisyphus     │◄────────►│  Agent Bridge    │◄────────►│  PC da Loja  │
  │  192.168.15.41│          │  :8000/v1/agent  │          │  192.168.15.6│
  └──────────────┘           └──────────────────┘           └──────────────┘

Uso pelo agente remoto:
  curl -s http://192.168.15.41:8000/v1/agent/pending   # ler instruções
  curl -s http://192.168.15.41:8000/v1/agent/send \\     # enviar resposta
    -X POST -H 'Content-Type: application/json' \
    -d '{"to":"sisyphus","type":"response","body":"..."}'
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

# ── Observability hooks (optional) ─────────────────────────────────────
try:
    from app.api.v1.observability import (
        notify_message_sent,
        notify_message_pending,
        notify_agent_status,
        notify_agent_registered,
    )
    _OBSERVABILITY_ENABLED = True
except ImportError:
    _OBSERVABILITY_ENABLED = False
    logger.warning("AgentBridge: observability module not available — metrics/SSE disabled")

# ── Message queue (file-backed for persistence) ──────────────────────────
MESSAGES_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "agent_messages"
MESSAGES_DIR.mkdir(parents=True, exist_ok=True)


def _next_id() -> str:
    return f"{int(time.time() * 1000000)}-{os.urandom(4).hex()}"


def _save_message(msg: dict) -> str:
    msg_id = _next_id()
    msg["id"] = msg_id
    msg["timestamp"] = time.time()
    (MESSAGES_DIR / f"{msg_id}.json").write_text(json.dumps(msg, ensure_ascii=False))
    logger.info("AgentBridge: message %s saved (to=%s, type=%s)", msg_id, msg.get("to"), msg.get("type"))
    return msg_id


def _load_messages(target: str, limit: int = 10) -> list[dict]:
    files = sorted(MESSAGES_DIR.iterdir(), reverse=True) if MESSAGES_DIR.exists() else []
    result = []
    for f in files:
        if f.suffix != ".json":
            continue
        try:
            msg = json.loads(f.read_text())
            if msg.get("to") == target or target == "*":
                result.append(msg)
                if len(result) >= limit:
                    break
        except (json.JSONDecodeError, OSError):
            pass
    return result


def _ack_message(msg_id: str) -> bool:
    path = MESSAGES_DIR / f"{msg_id}.json"
    if path.exists():
        path.unlink()
        logger.info("AgentBridge: message %s acknowledged and removed", msg_id)
        return True
    return False


# ── Schemas ──────────────────────────────────────────────────────────────


class AgentMessage(BaseModel):
    to: str = "sisyphus"  # destinatário: "sisyphus" | "store-agent"
    type: str = "message"  # "message" | "response" | "image_uploaded" | "sync_complete" | "error"
    body: str = ""
    payload: Optional[dict[str, Any]] = None


class AgentStatus(BaseModel):
    status: str = "online"
    agent: str = "sisyphus"
    version: str = "1.0"
    last_ping: float = 0.0


# ── In-memory agent registry ────────────────────────────────────────────
_remote_status: dict[str, Any] = {"status": "unknown", "last_seen": 0}


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/ping")
async def agent_ping():
    """Health check — usado pelo agente remoto pra ver se a ponte está no ar."""
    return {"ok": True, "agent": "sisyphus-bridge", "time": time.time()}


@router.post("/send")
async def agent_send(msg: AgentMessage):
    """Envia uma mensagem para a fila do agente destino."""
    msg_id = _save_message(msg.model_dump())
    if _OBSERVABILITY_ENABLED:
        notify_message_sent(to=msg.to, msg_type=msg.type, body_preview=msg.body)
    return {"ok": True, "message_id": msg_id}


@router.get("/pending")
async def agent_pending(
    target: str = Query("sisyphus", description="Quem vai ler"),
    limit: int = Query(10, ge=1, le=50),
):
    """Lê mensagens pendentes para um agente (e as remove da fila)."""
    messages = _load_messages(target, limit=limit)
    ids = [m["id"] for m in messages]
    for mid in ids:
        _ack_message(mid)
    if _OBSERVABILITY_ENABLED and messages:
        notify_message_pending(target)
    return {"messages": messages, "count": len(messages)}


@router.post("/status")
async def agent_status(status: AgentStatus):
    """Agente remoto reporta seu status."""
    _remote_status.update(status.model_dump())
    _remote_status["last_seen"] = time.time()
    logger.info("AgentBridge: remote agent status=%s last_seen=%s", status.status, _remote_status["last_seen"])
    return {"ok": True}


@router.get("/status")
async def get_agent_status():
    """Consulta o status do agente remoto."""
    return {
        "local": {"agent": "sisyphus", "status": "online"},
        "remote": _remote_status,
    }
