"""Swarm Coordination — conecta múltiplas instâncias OpenCode em um cluster.

Cada instância OpenCode se registra como um agente no swarm.
Podem trocar mensagens, receber tarefas, e coordenar trabalho via
HTTP usando o inventory-service como backbone central.

Arquitetura:
  ┌──────────┐  HTTP   ┌──────────────────┐  HTTP   ┌──────────┐
  │ Sisyphus │◄───────►│ inventory-service │◄───────►│ Worker 1 │
  │ (dev)    │         │  :8000/v1/swarm   │         │ Worker 2 │
  │          │         │                    │         │ Worker 3 │
  └──────────┘         └──────────────────┘         └──────────┘
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

from app.api.v1.observability import (
    notify_agent_registered,
    notify_agent_status,
    notify_message_sent,
    _push_event as _push_obs_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/swarm", tags=["swarm"])

# ── File-based swarm state ──────────────────────────────────────────────
SWARM_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "swarm"
SWARM_DIR.mkdir(parents=True, exist_ok=True)

AGENTS_FILE = SWARM_DIR / "agents.json"
TASKS_FILE = SWARM_DIR / "tasks.json"


def _read_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def _write_json(path: Path, data: list[dict]) -> None:
    import tempfile
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tf:
        json.dump(data, tf, ensure_ascii=False, indent=2)
        temp_name = tf.name
    os.replace(temp_name, path)


def _next_id() -> str:
    return f"{int(time.time() * 1000000)}-{os.urandom(4).hex()}"


# ── Schemas ─────────────────────────────────────────────────────────────


class SwarmRegisterRequest(BaseModel):
    name: str
    port: int
    capabilities: list[str] = []
    host: str = "127.0.0.1"
    pid: Optional[int] = None


class SwarmTaskRequest(BaseModel):
    target: str = "*"
    title: str
    description: str = ""
    priority: str = "medium"
    source: str = "unknown"


class SwarmTaskComplete(BaseModel):
    result: str = ""
    status: str = "done"
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""


class SwarmMessage(BaseModel):
    target: str = "*"
    type: str = "message"
    body: str = ""
    source: str = "unknown"


# ── Endpoints ───────────────────────────────────────────────────────────


@router.post("/register")
async def swarm_register(req: SwarmRegisterRequest):
    """Register an OpenCode instance as a swarm agent."""
    agents = _read_json(AGENTS_FILE)

    # update or append
    existing = [a for a in agents if a["name"] == req.name]
    now = time.time()
    entry = {
        "name": req.name,
        "host": req.host,
        "port": req.port,
        "pid": req.pid,
        "capabilities": req.capabilities,
        "status": "online",
        "registered_at": existing[0]["registered_at"] if existing else now,
        "last_seen": now,
        "last_heartbeat": now,
    }

    if existing:
        for i, a in enumerate(agents):
            if a["name"] == req.name:
                agents[i] = entry
                break
    else:
        agents.append(entry)

    _write_json(AGENTS_FILE, agents)
    logger.info("Swarm: agent %s registered (port=%d, caps=%s)", req.name, req.port, req.capabilities)

    # notify observability
    notify_agent_registered(req.name, req.capabilities)
    notify_agent_status(req.name, "online")

    return {"ok": True, "agent": entry, "swarm_size": len(agents)}


@router.get("/agents")
async def swarm_agents():
    """List all registered swarm agents with their status."""
    agents = _read_json(AGENTS_FILE)

    # check heartbeat freshness
    now = time.time()
    for a in agents:
        last = a.get("last_heartbeat", 0)
        if now - last > 120:
            a["status"] = "offline"
        elif now - last > 60:
            a["status"] = "idle"
        else:
            a["status"] = "online"

    return {"agents": agents, "count": len(agents), "online": sum(1 for a in agents if a["status"] == "online")}


@router.get("/heartbeat")
@router.post("/heartbeat")
async def swarm_heartbeat(name: str = Query(...)):
    """Agent sends heartbeat to show it's alive. Accepts GET or POST."""
    agents = _read_json(AGENTS_FILE)
    for a in agents:
        if a["name"] == name:
            a["last_heartbeat"] = time.time()
            a["status"] = "online"
            _write_json(AGENTS_FILE, agents)
            notify_agent_status(name, "online")
            return {"ok": True, "agent": name}
    raise HTTPException(404, f"Agent '{name}' not registered. Call /swarm/register first.")


# ── Task Management ────────────────────────────────────────────────────


@router.post("/task")
async def swarm_create_task(task: SwarmTaskRequest):
    """Assign a task to a specific agent or broadcast to all."""
    tasks = _read_json(TASKS_FILE)
    entry = {
        "id": _next_id(),
        "target": task.target,
        "title": task.title,
        "description": task.description,
        "priority": task.priority,
        "source": task.source,
        "status": "pending",
        "progress": 0,
        "steps": [],
        "created_at": time.time(),
        "assigned_at": None,
        "completed_at": None,
    }
    tasks.append(entry)
    _write_json(TASKS_FILE, tasks)
    logger.info("Swarm: task %s created for target=%s: %s", entry["id"], task.target, task.title)
    return {"ok": True, "task": entry}


class ProgressRequest(BaseModel):
    progress: int = 0  # 0-100
    message: str = ""
    step: str = ""  # current step description
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""


@router.patch("/task/{task_id}/progress")
async def swarm_task_progress(task_id: str, body: ProgressRequest):
    """Update a task's progress (0-100) and optionally add a step check."""
    tasks = _read_json(TASKS_FILE)
    for t in tasks:
        if t["id"] == task_id:
            t["progress"] = max(0, min(100, body.progress))

            # Add step to checklist
            if body.step:
                if "steps" not in t:
                    t["steps"] = []
                # Mark previous step as done, add current
                if not any(s["step"] == body.step for s in t["steps"]):
                    for s in t["steps"]:
                        if not s.get("done"):
                            s["done"] = True
                    t["steps"].append({"step": body.step, "done": body.progress >= 100, "updated_at": time.time()})
                else:
                    for s in t["steps"]:
                        if s["step"] == body.step and not s.get("done"):
                            s["done"] = body.progress >= 100
                            s["updated_at"] = time.time()
            
            if body.tokens_in: t["tokens_in"] = body.tokens_in
            if body.tokens_out: t["tokens_out"] = body.tokens_out
            if body.model: t["model"] = body.model

            if body.progress >= 100 and t.get("status") != "done":
                t["status"] = "done"
                t["completed_at"] = time.time()
            elif body.progress > 0 and t.get("status") == "pending":
                t["status"] = "in_progress"
                t["assigned_at"] = time.time()

            _write_json(TASKS_FILE, tasks)

            # Log activity
            _push_obs_event("task_progress", {
                "task_id": task_id,
                "progress": body.progress,
                "message": body.message[:100],
                "step": body.step,
            })
            return {"ok": True, "task": {k: t.get(k) for k in ("id", "title", "target", "status", "progress", "steps")}}
    raise HTTPException(404, f"Task {task_id} not found")


@router.get("/task/{task_id}")
async def swarm_get_task(task_id: str):
    """Get detailed info about a specific task including progress and steps."""
    tasks = _read_json(TASKS_FILE)
    for t in tasks:
        if t["id"] == task_id:
            return {"ok": True, "task": t}
    raise HTTPException(404, f"Task {task_id} not found")


@router.get("/tasks/{agent_name}")
async def swarm_get_tasks(
    agent_name: str,
    status: str = Query("pending", description="Filter by status"),
    limit: int = Query(10, ge=1, le=50),
):
    """Get tasks assigned to a specific agent (or '*' for all)."""
    tasks = _read_json(TASKS_FILE)
    result = []
    for t in tasks:
        if t["status"] != status:
            continue
        if t["target"] == "*" or t["target"] == agent_name:
            result.append(t)
            if len(result) >= limit:
                break
    # mark claimed tasks as in_progress
    for t in tasks:
        if t["id"] in {r["id"] for r in result}:
            t["status"] = "in_progress"
            t["assigned_at"] = time.time()
    _write_json(TASKS_FILE, tasks)
    return {"tasks": result, "count": len(result)}


@router.post("/task/{task_id}/complete")
async def swarm_complete_task(task_id: str, body: SwarmTaskComplete = None):
    """Mark a task as completed with an optional result."""
    tasks = _read_json(TASKS_FILE)
    for t in tasks:
        if t["id"] == task_id:
            t["status"] = "done"
            t["completed_at"] = time.time()
    if body and body.result:
        t["result"] = body.result
    if body:
        t["tokens_in"] = body.tokens_in
        t["tokens_out"] = body.tokens_out
        t["model"] = body.model or t.get("model", "unknown")
    _write_json(TASKS_FILE, tasks)
    # notify observability
    _push_obs_event("task_completed", {
        "task_id": task_id,
        "agent": t.get("source", "unknown"),
        "target": t.get("target"),
        "title": t.get("title"),
        "result": t.get("result", "")[:200],
    })
    return {"ok": True, "task": t}
    raise HTTPException(404, f"Task {task_id} not found")


@router.get("/tasks")
async def swarm_list_tasks(
    status: str = Query(None, description="Filter: pending, in_progress, done"),
    agent: str = Query(None, description="Filter by target agent name"),
    limit: int = Query(20, ge=1, le=100),
):
    """List all tasks with optional status/agent filters. Latest first."""
    tasks = _read_json(TASKS_FILE)
    tasks.reverse()
    if status:
        tasks = [t for t in tasks if t["status"] == status]
    if agent:
        tasks = [t for t in tasks if t["target"] == agent or t["target"] == "*"]
    return {"tasks": tasks[:limit], "count": len(tasks[:limit]), "total": len(tasks)}


@router.get("/responses/{agent_name}")
async def swarm_get_responses(
    agent_name: str = "sisyphus",
    limit: int = Query(20, ge=1, le=100),
    unread_only: bool = Query(False),
):
    """
    Get all responses directed TO a specific agent.
    Returns completed tasks + incoming messages, merged by time.
    """
    tasks = _read_json(TASKS_FILE)
    # completed tasks where source matches the agent (i.e. tasks created by sisyphus, completed by workers)
    completed = []
    for t in tasks:
        # Show tasks created BY this agent (e.g. sisyphus assigned work to workers)
        # OR tasks assigned TO this agent that were completed by others
        is_mine = (t.get("source") == agent_name) or (t.get("target") == agent_name)
        if t["status"] == "done" and is_mine:
            completed.append({
                "type": "task_completed",
                "id": t["id"],
                "title": t["title"],
                "result": t.get("result", ""),
                "source": t.get("source", "unknown"),
                "target": t.get("target"),
                "timestamp": t.get("completed_at", t.get("created_at")),
            })

    # incoming messages directed to this agent
    messages_dir = SWARM_DIR / "messages"
    incoming = []
    if messages_dir.exists():
        files = sorted(messages_dir.iterdir(), reverse=True)
        for f in files:
            if f.suffix != ".json":
                continue
            try:
                msg = json.loads(f.read_text())
                if msg.get("target") == "*" or msg.get("target") == agent_name:
                    incoming.append({
                        "type": "message",
                        "id": msg["id"],
                        "title": msg.get("type", "message"),
                        "result": msg.get("body", ""),
                        "source": msg.get("source", "unknown"),
                        "target": agent_name,
                        "read": msg.get("read", False),
                        "timestamp": msg.get("timestamp", 0),
                    })
                    if len(incoming) >= limit:
                        break
            except (json.JSONDecodeError, OSError):
                pass

    merged = sorted(completed + incoming, key=lambda x: x.get("timestamp", 0), reverse=True)
    unread_count = sum(1 for r in merged if not r.get("read", True))

    if unread_only:
        merged = [r for r in merged if not r.get("read", True)]

    return {
        "responses": merged[:limit],
        "count": len(merged[:limit]),
        "total": len(completed) + len(incoming),
        "unread": unread_count,
        "completed_tasks": len(completed),
        "incoming_messages": len(incoming),
    }


@router.get("/responses/{agent_name}/mark-read")
async def swarm_mark_read(agent_name: str = "sisyphus"):
    """Mark all responses as read for this agent."""
    messages_dir = SWARM_DIR / "messages"
    if messages_dir.exists():
        for f in messages_dir.glob("*.json"):
            try:
                msg = json.loads(f.read_text())
                if msg.get("target") == "*" or msg.get("target") == agent_name:
                    msg["read"] = True
                    f.write_text(json.dumps(msg, ensure_ascii=False))
            except (json.JSONDecodeError, OSError):
                pass
    return {"ok": True}


@router.get("/conversations")
async def swarm_conversations(
    limit: int = Query(30, ge=1, le=100),
    since: float = Query(0, description="Only messages after this timestamp"),
):
    """Get ALL inter-agent messages across the swarm — for the dashboard feed."""
    messages_dir = SWARM_DIR / "messages"
    if not messages_dir.exists():
        return {"messages": [], "count": 0, "agents": []}

    files = sorted(messages_dir.iterdir(), reverse=True)
    result = []
    seen_agents: set = set()
    for f in files:
        if f.suffix != ".json":
            continue
        try:
            msg = json.loads(f.read_text())
            ts = msg.get("timestamp", 0)
            if ts < since:
                continue
            seen_agents.add(msg.get("source", "?"))
            seen_agents.add(msg.get("target", "?"))
            result.append(msg)
            if len(result) >= limit:
                break
        except (json.JSONDecodeError, OSError):
            pass
    return {"messages": result, "count": len(result), "agents": sorted(seen_agents)}


# ── Inter-Agent Messaging ──────────────────────────────────────────────


@router.post("/message")
async def swarm_send_message(msg: SwarmMessage):
    """Send a message to one or all swarm agents. Stored for pickup."""
    messages_dir = SWARM_DIR / "messages"
    messages_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "id": _next_id(),
        "source": msg.source,
        "target": msg.target,
        "type": msg.type,
        "body": msg.body,
        "timestamp": time.time(),
        "read": False,
    }
    (messages_dir / f"{entry['id']}.json").write_text(
        json.dumps(entry, ensure_ascii=False)
    )
    notify_message_sent(to=msg.target, msg_type=msg.type, body_preview=msg.body)
    return {"ok": True, "message_id": entry["id"]}


@router.get("/messages/{agent_name}")
async def swarm_get_messages(
    agent_name: str,
    limit: int = Query(10, ge=1, le=50),
    mark_read: bool = Query(True),
):
    """Get messages for an agent."""
    messages_dir = SWARM_DIR / "messages"
    if not messages_dir.exists():
        return {"messages": [], "count": 0}

    files = sorted(messages_dir.iterdir(), reverse=True)
    result = []
    for f in files:
        if f.suffix != ".json":
            continue
        try:
            msg = json.loads(f.read_text())
            if msg["target"] == "*" or msg["target"] == agent_name:
                result.append(msg)
                if mark_read:
                    msg["read"] = True
                    f.write_text(json.dumps(msg, ensure_ascii=False))
                if len(result) >= limit:
                    break
        except (json.JSONDecodeError, OSError):
            pass
    return {"messages": result, "count": len(result)}


@router.post("/unregister")
async def swarm_unregister(name: str = Query(...)):
    """Remove an agent from the swarm."""
    agents = _read_json(AGENTS_FILE)
    agents = [a for a in agents if a["name"] != name]
    _write_json(AGENTS_FILE, agents)
    logger.info("Swarm: agent %s unregistered", name)
    notify_agent_status(name, "offline")
    return {"ok": True, "agent": name}


# ── Activity Feed (micro-level status) ─────────────────────────────────

ACTIVITY_FILE = SWARM_DIR / "activity.json"


class ActivityRequest(BaseModel):
    agent: str
    action: str = "working"  # working, thinking, completed, paused, error
    message: str = ""
    task_id: str = ""
    details: dict = {}


@router.post("/activity")
async def swarm_activity(req: ActivityRequest):
    """Log an activity update from an agent (working, thinking, completed, etc)."""
    activities = _read_activity()
    entry = {
        "id": _next_id(),
        "agent": req.agent,
        "action": req.action,
        "message": req.message,
        "task_id": req.task_id,
        "details": req.details,
        "timestamp": time.time(),
    }
    activities.append(entry)
    if len(activities) > 500:
        activities = activities[-500:]
    _write_json(ACTIVITY_FILE, activities)
    _push_obs_event("agent_activity", {
        "agent": req.agent,
        "action": req.action,
        "message": req.message[:100],
        "task_id": req.task_id,
    })
    return {"ok": True, "activity_id": entry["id"]}


@router.get("/activity")
async def swarm_get_activity(
    agent: str = Query(None, description="Filter by agent name"),
    action: str = Query(None, description="Filter by action type"),
    limit: int = Query(50, ge=1, le=200),
    since: float = Query(0, description="Only activities after this timestamp"),
):
    """Get the activity feed. Latest first."""
    activities = _read_activity()
    result = []
    for a in reversed(activities):
        ts = a.get("timestamp", 0)
        if ts < since:
            continue
        if agent and a.get("agent") != agent:
            continue
        if action and a.get("action") != action:
            continue
        result.append(a)
        if len(result) >= limit:
            break

    # Count agents currently active (activity in last 5min)
    now = time.time()
    active_agents = set()
    for a in activities:
        if now - a.get("timestamp", 0) <= 300:
            active_agents.add(a.get("agent"))

    return {
        "activities": result,
        "count": len(result),
        "active_agents": sorted(active_agents),
        "active_count": len(active_agents),
    }


def _read_activity() -> list[dict]:
    if not ACTIVITY_FILE.exists():
        return []
    try:
        return json.loads(ACTIVITY_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


# ── Rooms (Chat Channels) ──────────────────────────────────────────────

ROOMS_DIR = SWARM_DIR / "rooms"
ROOMS_INDEX = SWARM_DIR / "rooms-index.json"


def _rooms_index() -> dict:
    if ROOMS_INDEX.exists():
        try:
            return json.loads(ROOMS_INDEX.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_rooms_index(idx: dict) -> None:
    ROOMS_INDEX.write_text(json.dumps(idx, ensure_ascii=False, indent=2))


class CreateRoomRequest(BaseModel):
    name: str
    description: str = ""
    created_by: str = "unknown"


class RoomMessageRequest(BaseModel):
    sender: str
    body: str
    msg_type: str = "text"


class JoinRoomRequest(BaseModel):
    agent: str


@router.post("/room/create")
async def swarm_room_create(req: CreateRoomRequest):
    """Create a persistent chat room."""
    idx = _rooms_index()
    if req.name in idx:
        raise HTTPException(409, f"Room '{req.name}' already exists")

    now = time.time()
    entry = {
        "name": req.name,
        "description": req.description,
        "created_by": req.created_by,
        "created_at": now,
        "members": [req.created_by],
        "message_count": 0,
    }
    idx[req.name] = entry
    _save_rooms_index(idx)

    # Create room messages dir
    room_dir = ROOMS_DIR / req.name
    room_dir.mkdir(parents=True, exist_ok=True)

    # Welcome message
    welcome = {
        "id": _next_id(),
        "room": req.name,
        "sender": "system",
        "body": f"Room '{req.name}' created by {req.created_by}",
        "msg_type": "system",
        "timestamp": now,
    }
    (room_dir / f"{welcome['id']}.json").write_text(json.dumps(welcome, ensure_ascii=False))

    logger.info("Swarm: room '%s' created by %s", req.name, req.created_by)
    return {"ok": True, "room": entry}


@router.get("/rooms")
async def swarm_rooms_list():
    """List all chat rooms with member count and last activity."""
    idx = _rooms_index()
    rooms = []
    for name, data in idx.items():
        rooms.append({
            "name": name,
            "description": data.get("description", ""),
            "created_by": data.get("created_by", "?"),
            "member_count": len(data.get("members", [])),
            "message_count": data.get("message_count", 0),
            "created_at": data.get("created_at", 0),
        })
    rooms.sort(key=lambda r: r.get("created_at", 0))
    return {"rooms": rooms, "count": len(rooms)}


@router.post("/room/{room_name}/join")
async def swarm_room_join(room_name: str, req: JoinRoomRequest):
    """Join an agent to a room."""
    idx = _rooms_index()
    if room_name not in idx:
        raise HTTPException(404, f"Room '{room_name}' not found")

    if req.agent not in idx[room_name]["members"]:
        idx[room_name]["members"].append(req.agent)
        _save_rooms_index(idx)

        join_msg = {
            "id": _next_id(),
            "room": room_name,
            "sender": "system",
            "body": f"{req.agent} joined the room",
            "msg_type": "system",
            "timestamp": time.time(),
        }
        room_dir = ROOMS_DIR / room_name
        room_dir.mkdir(parents=True, exist_ok=True)
        (room_dir / f"{join_msg['id']}.json").write_text(json.dumps(join_msg, ensure_ascii=False))
        idx[room_name]["message_count"] = idx[room_name].get("message_count", 0) + 1
        _save_rooms_index(idx)

    return {"ok": True, "room": room_name, "members": idx[room_name]["members"]}


@router.get("/room/{room_name}/members")
async def swarm_room_members(room_name: str):
    """List members of a room."""
    idx = _rooms_index()
    if room_name not in idx:
        raise HTTPException(404, f"Room '{room_name}' not found")

    members = idx[room_name].get("members", [])
    # Enrich with current status from agents file
    agents = _read_json(AGENTS_FILE)
    agent_map = {a["name"]: a.get("status", "offline") for a in agents}

    enriched = []
    for m in members:
        enriched.append({"name": m, "status": agent_map.get(m, "unknown")})

    return {"room": room_name, "members": enriched, "count": len(enriched)}


@router.post("/room/{room_name}/message")
async def swarm_room_message(room_name: str, req: RoomMessageRequest):
    """Send a message to a room."""
    idx = _rooms_index()
    if room_name not in idx:
        raise HTTPException(404, f"Room '{room_name}' not found")

    room_dir = ROOMS_DIR / room_name
    room_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "id": _next_id(),
        "room": room_name,
        "sender": req.sender,
        "body": req.body,
        "msg_type": req.msg_type,
        "timestamp": time.time(),
    }
    (room_dir / f"{entry['id']}.json").write_text(json.dumps(entry, ensure_ascii=False))

    idx[room_name]["message_count"] = idx[room_name].get("message_count", 0) + 1
    _save_rooms_index(idx)

    logger.info("Swarm: %s sent message in room '%s'", req.sender, room_name)
    return {"ok": True, "message_id": entry["id"]}


@router.get("/room/{room_name}/messages")
async def swarm_room_messages(
    room_name: str,
    limit: int = Query(50, ge=1, le=200),
    since: float = Query(0, description="Only messages after this timestamp"),
):
    """Get messages from a room (latest first)."""
    idx = _rooms_index()
    if room_name not in idx:
        raise HTTPException(404, f"Room '{room_name}' not found")

    room_dir = ROOMS_DIR / room_name
    if not room_dir.exists():
        return {"messages": [], "count": 0, "room": room_name}

    files = sorted(room_dir.iterdir(), reverse=True)
    result = []
    for f in files:
        if f.suffix != ".json":
            continue
        try:
            msg = json.loads(f.read_text())
            ts = msg.get("timestamp", 0)
            if ts < since:
                continue
            result.append(msg)
            if len(result) >= limit:
                break
        except (json.JSONDecodeError, OSError):
            pass

    return {"messages": result, "count": len(result), "room": room_name}


# ── Watch / Notification ───────────────────────────────────────────────

class WatchResponse(BaseModel):
    new_messages: list[dict] = []
    new_tasks: list[dict] = []
    has_updates: bool = False
    agent_status: list[dict] = []


@router.get("/watch/{agent_name}")
async def swarm_watch(
    agent_name: str,
    since: float = Query(0, description="Check for updates since this timestamp"),
    online_only: bool = Query(False, description="Only show online agents"),
):
    """
    Watch endpoint for daemons. Returns everything new since `since`:
    - New direct messages for this agent
    - New messages in rooms this agent is a member of
    - New tasks assigned to this agent
    - Agent status changes (who came online/offline)
    """
    now = time.time()
    resp = WatchResponse()

    # 1. Direct messages
    messages_dir = SWARM_DIR / "messages"
    if messages_dir.exists():
        files = sorted(messages_dir.iterdir(), reverse=True)
        for f in files:
            if f.suffix != ".json":
                continue
            try:
                msg = json.loads(f.read_text())
                ts = msg.get("timestamp", 0)
                if ts <= since:
                    continue
                target = msg.get("target", "")
                if target == "*" or target == agent_name:
                    resp.new_messages.append(msg)
            except (json.JSONDecodeError, OSError):
                pass

    # 2. Room messages
    idx = _rooms_index()
    for room_name, room_data in idx.items():
        if agent_name not in room_data.get("members", []):
            continue
        room_dir = ROOMS_DIR / room_name
        if not room_dir.exists():
            continue
        files = sorted(room_dir.iterdir(), reverse=True)
        for f in files:
            if f.suffix != ".json":
                continue
            try:
                msg = json.loads(f.read_text())
                ts = msg.get("timestamp", 0)
                if ts <= since:
                    continue
                if msg.get("sender") != agent_name:
                    resp.new_messages.append({**msg, "room": room_name})
            except (json.JSONDecodeError, OSError):
                pass

    # 3. New tasks
    tasks = _read_json(TASKS_FILE)
    for t in tasks:
        created = t.get("created_at", 0)
        if created <= since:
            continue
        if t["target"] == "*" or t["target"] == agent_name:
            resp.new_tasks.append(t)

    # 4. Agent status changes
    agents = _read_json(AGENTS_FILE)
    for a in agents:
        last_heartbeat = a.get("last_heartbeat", 0)
        status = "offline"
        if now - last_heartbeat <= 60:
            status = "online"
        elif now - last_heartbeat <= 120:
            status = "idle"
        if online_only and status != "online":
            continue
        resp.agent_status.append({
            "name": a["name"],
            "status": status,
            "capabilities": a.get("capabilities", []),
            "pid": a.get("pid"),
        })

    resp.has_updates = bool(resp.new_messages or resp.new_tasks)
    return resp
