"""Read-only Cockpit endpoints surfacing Caio operational data.

These endpoints expose snapshots of Caio's append-only event logs (Think Loop
proposals, policy decisions, reflexion critiques, etc) so the Cockpit UI can
list "what Caio decided / proposed recently".

Per plano canônico V1.1 the Cockpit is **read-only** here. Approve/reject is
``mark_only`` and does **not** mutate Caio's pipelines or the
``#wa-aprovacoes`` Discord channel.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from app.core.auth import AuthContext, get_auth_context
from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.caio import CaioRecentEventsResponse
from app.services.caio_bridge import EventsSqliteReader

logger = get_logger(__name__)
router = APIRouter(prefix="/caio", tags=["caio"])

AUTH_CONTEXT_DEP = Depends(get_auth_context)
LIMIT_QUERY = Query(default=20, ge=1, le=200)


@lru_cache(maxsize=1)
def _events_reader() -> EventsSqliteReader:
    state_dir = settings.caio_state_dir.strip()
    enabled = settings.caio_bridge_events_enabled and bool(state_dir)
    # When disabled or unconfigured we still construct the reader so the
    # endpoint can report a consistent "disabled" status without 500s.
    db_path = Path(state_dir) / "events.sqlite" if state_dir else Path("/dev/null/events.sqlite")
    return EventsSqliteReader(
        db_path=db_path,
        enabled=enabled,
        timeout_s=settings.caio_bridge_timeout_s,
    )


@router.get(
    "/think-loop/recent",
    response_model=CaioRecentEventsResponse,
    summary="Recent Caio Think Loop events",
    description=(
        "Return the most recent Caio events (Think Loop proposals, policy "
        "decisions, advisor consults, reflexion critiques). Read-only; the "
        "underlying SQLite is opened in strict `mode=ro` URI mode and the "
        "endpoint degrades to `{status: 'disabled'|'circuit_open'|'error'}` "
        "instead of raising."
    ),
)
async def recent_think_loop_events(
    limit: int = LIMIT_QUERY,
    _auth: AuthContext = AUTH_CONTEXT_DEP,
) -> CaioRecentEventsResponse:
    reader = _events_reader()
    result = await reader.recent_events(limit=limit)
    return CaioRecentEventsResponse(
        status=result.status,
        error_class=result.error_class,
        latency_ms=result.latency_ms,
        items=list(result.data) if result.status == "ok" and result.data else [],
    )
