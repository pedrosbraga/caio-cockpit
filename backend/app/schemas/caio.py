"""Response schemas for the read-only Caio bridges API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CaioBridgeStatus = Literal["ok", "error", "disabled", "circuit_open", "timeout"]


class CaioEventItem(BaseModel):
    """A single Caio Think Loop / Reflexion event from ``events.sqlite``."""

    event_id: str
    occurred_at: str = Field(description="ISO-8601 timestamp from the producer.")
    event_type: str
    source: str
    producer_id: str
    correlation_id: str | None = None
    thread_id: str | None = None
    payload: Any = Field(
        default=None,
        description="Decoded JSON payload. May be ``None`` if the row was unparsable.",
    )


class CaioRecentEventsResponse(BaseModel):
    """Response envelope: status + diagnostics + items."""

    status: CaioBridgeStatus
    error_class: str | None = Field(
        default=None,
        description="Set only when ``status`` is ``error`` or ``timeout``.",
    )
    latency_ms: int = 0
    items: list[CaioEventItem] = Field(default_factory=list)
