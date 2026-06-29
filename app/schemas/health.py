from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
    version: str
    database: str
    adapters: list[str]


class ChannelHealthDetail(BaseModel):
    channel: str
    status: str
    active: bool
    failure_count: int
    circuit_state: str
    daily_requests: int = 0
    last_error: str | None = None


class HealthDetailResponse(BaseModel):
    status: str
    version: str
    database: str
    database_latency_ms: float | None = None
    uptime_seconds: float | None = None
    channels: list[ChannelHealthDetail]
