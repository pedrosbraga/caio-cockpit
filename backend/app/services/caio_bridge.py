"""Read-only bridges to the Caio operational data sources.

Caio (Pedro's autonomous WhatsApp/Discord agent) writes structured events into a
few local stores on the same host as the OpenClaw gateway:

- ``events.sqlite``: append-only event log for the Think Loop (proposals,
  policy decisions, ticks, dispatches, advisor consults, reflexion critiques).
- ``critiques.sqlite``: Reflexion loop weekly critiques of past approvals.
- A Postgres database used by the WhatsApp webhook V3 pipeline (approval_log
  with the contact/draft/final response history).

The Cockpit only ever *reads* from these stores. Writes belong to Caio's own
processes; mutating these databases from the Cockpit would break Caio's
invariants and the #wa-aprovacoes pipeline.

Resilience guarantees per plano canônico V1.1 (CRITICAL #2):

- SQLite is opened in strict read-only URI mode
  (``file:<path>?mode=ro&uri=true``). The reader **never** sets
  ``PRAGMA journal_mode``; that would mutate the WAL file shared with the
  upstream writer.
- Every call has a hard wall-clock timeout (default 2 s). Timeouts and
  recognized I/O errors trip the per-bridge circuit breaker (open after 3
  failures within 60 s).
- Each bridge has an ``enabled`` feature flag (env var, see settings). When
  disabled, ``safe_read`` returns ``{"data": None, "status": "disabled"}``
  immediately — endpoints can degrade gracefully without raising.
- Callers ALWAYS receive ``BridgeResult`` (data may be ``None``); they never
  observe ``OperationalError`` directly.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from app.core.logging import get_logger

logger = get_logger(__name__)

BridgeStatus = Literal["ok", "error", "disabled", "circuit_open", "timeout"]


@dataclass(slots=True)
class BridgeResult:
    """Outcome of a single read through a bridge.

    ``status="ok"`` is the only case with usable ``data``. Other statuses carry
    diagnostic info so endpoints can render a "stale data" UI hint without
    leaking internals.
    """

    status: BridgeStatus
    data: Any = None
    error_class: str | None = None
    latency_ms: int = 0


class BridgeBase:
    """Common envelope: feature flag, timeout, circuit breaker, structlog."""

    name: str = "bridge"
    default_timeout_s: float = 2.0
    circuit_failure_window_s: float = 60.0
    circuit_failure_threshold: int = 3

    def __init__(self, *, enabled: bool, timeout_s: float | None = None) -> None:
        self.enabled = enabled
        self.timeout_s = timeout_s or self.default_timeout_s
        self._failure_times: deque[float] = deque()
        self._circuit_open_until: float = 0.0

    # ------------------------------------------------------------------ helpers

    def _now(self) -> float:
        return time.monotonic()

    def _circuit_is_open(self) -> bool:
        return self._now() < self._circuit_open_until

    def _record_failure(self) -> None:
        now = self._now()
        cutoff = now - self.circuit_failure_window_s
        while self._failure_times and self._failure_times[0] < cutoff:
            self._failure_times.popleft()
        self._failure_times.append(now)
        if len(self._failure_times) >= self.circuit_failure_threshold:
            # Trip the breaker for 60s, then close again.
            self._circuit_open_until = now + self.circuit_failure_window_s
            logger.warning(
                "caio_bridge.circuit_open bridge=%s recent_failures=%s window_s=%s",
                self.name,
                len(self._failure_times),
                self.circuit_failure_window_s,
            )

    def _record_success(self) -> None:
        self._failure_times.clear()
        self._circuit_open_until = 0.0

    # ------------------------------------------------------------------ public

    async def safe_read(
        self,
        query: Callable[[], Awaitable[Any]],
    ) -> BridgeResult:
        """Run ``query`` under timeout + circuit breaker; never raises."""
        if not self.enabled:
            return BridgeResult(status="disabled")
        if self._circuit_is_open():
            return BridgeResult(status="circuit_open")
        started = time.perf_counter()
        try:
            data = await asyncio.wait_for(query(), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            self._record_failure()
            return BridgeResult(
                status="timeout",
                error_class="TimeoutError",
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        except (sqlite3.Error, OSError) as exc:
            self._record_failure()
            logger.warning(
                "caio_bridge.read_error bridge=%s error_class=%s",
                self.name,
                type(exc).__name__,
            )
            return BridgeResult(
                status="error",
                error_class=type(exc).__name__,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )
        self._record_success()
        return BridgeResult(
            status="ok",
            data=data,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


class EventsSqliteReader(BridgeBase):
    """Reads Caio's Think Loop events from ``events.sqlite`` (strict read-only).

    The events table is the canonical append-only log written by Caio's
    Think Loop runtime (``~/.openclaw/state/events.sqlite`` on the host). We
    surface the subset of event types that map naturally to a Cockpit
    "approval/decision" view: ``think_loop.proposal``,
    ``think_loop.policy_decision``, ``think_loop.dispatched``,
    ``advisor.consult_requested``, ``reflexion.critique_generated``.
    """

    name = "events_sqlite"

    DEFAULT_EVENT_TYPES: tuple[str, ...] = (
        "think_loop.proposal",
        "think_loop.policy_decision",
        "think_loop.dispatched",
        "advisor.consult_requested",
        "reflexion.critique_generated",
    )

    def __init__(
        self,
        *,
        db_path: Path,
        enabled: bool,
        timeout_s: float | None = None,
    ) -> None:
        super().__init__(enabled=enabled, timeout_s=timeout_s)
        # Resolve to an absolute path so the read-only URI we hand to SQLite
        # never contains "..". The mount itself is :ro at the docker layer, so
        # this is defense-in-depth (Codex round 1, HIGH #1).
        self._db_path = db_path.expanduser().resolve(strict=False)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _build_uri(self) -> str:
        # Strict read-only URI: the reader must never mutate the WAL or trigger
        # a journal_mode change. See plano V1.1 CRITICAL #2 (Codex round 2).
        return f"file:{self._db_path.as_posix()}?mode=ro&uri=true"

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._build_uri(),
            uri=True,
            timeout=self.timeout_s,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        # busy_timeout is the inner-loop budget for SQLite-level lock waits.
        # Our outer asyncio.wait_for(...) is the hard cap.
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout_s * 1000)}")
        return conn

    def _sync_recent_events(
        self,
        limit: int,
        event_types: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        if not self._db_path.exists():
            raise sqlite3.OperationalError(f"events DB not found: {self._db_path}")
        placeholders = ",".join("?" * len(event_types))
        query = (
            "SELECT event_id, occurred_at, event_type, source, producer_id, "
            "correlation_id, thread_id, payload_json "
            "FROM events "
            f"WHERE event_type IN ({placeholders}) "
            "AND deleted_at IS NULL "
            "ORDER BY occurred_at DESC "
            "LIMIT ?"
        )
        with self._open_connection() as conn:
            rows = conn.execute(query, (*event_types, limit)).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                payload = None
            out.append(
                {
                    "event_id": row["event_id"],
                    "occurred_at": row["occurred_at"],
                    "event_type": row["event_type"],
                    "source": row["source"],
                    "producer_id": row["producer_id"],
                    "correlation_id": row["correlation_id"],
                    "thread_id": row["thread_id"],
                    "payload": payload,
                },
            )
        return out

    async def recent_events(
        self,
        *,
        limit: int = 20,
        event_types: tuple[str, ...] | None = None,
    ) -> BridgeResult:
        """Return the latest Caio events as a ``BridgeResult`` (never raises)."""
        types = event_types or self.DEFAULT_EVENT_TYPES
        bounded_limit = max(1, min(int(limit), 200))

        async def _q() -> list[dict[str, Any]]:
            # SQLite is sync; offload to a thread so the event loop stays free.
            return await asyncio.to_thread(self._sync_recent_events, bounded_limit, types)

        return await self.safe_read(_q)


class CritiquesSqliteReader(BridgeBase):
    """Reads Caio's Reflexion-loop critiques from ``critiques.sqlite``.

    The Reflexion loop runs weekly (cron ``ai.openclaw.reflexion``, Sundays
    18:00 SP) and self-reviews a window of past WhatsApp approvals (replaced /
    rejected / manual_override). For each action it emits a structured
    critique: what Caio's suggestion missed (``miss``), what Pedro did better
    in the actual response (``hit``), the generalizable rule that closes the
    gap (``pattern``), plus a self-rated ``confidence`` 0-1.

    This bridge surfaces those critiques to the Cockpit so Pedro can see
    "Caio aprendendo" — patterns growing over time. It is read-only: patterns
    are insight, not actionable decisions, so there is no mark_only on top.

    ``raw_llm_response`` is **never** returned: that column is the raw LLM
    output Caio's reflexion-tick.sh persists for forensic replay and may be
    large; the curated fields above are everything the UI needs.
    """

    name = "critiques_sqlite"

    def __init__(
        self,
        *,
        db_path: Path,
        enabled: bool,
        timeout_s: float | None = None,
    ) -> None:
        super().__init__(enabled=enabled, timeout_s=timeout_s)
        # Defense-in-depth: resolve to abs path so the URI we hand SQLite has no
        # ".." segments. The bind mount is :ro at the docker layer.
        self._db_path = db_path.expanduser().resolve(strict=False)

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _build_uri(self) -> str:
        # Strict read-only URI **plus** ``immutable=1``. Why immutable here but
        # not in EventsSqliteReader: events.sqlite is journal_mode=WAL (carries
        # -wal/-shm sidecars), so ``mode=ro`` is enough — SQLite uses the WAL
        # as the consistency anchor without ever needing to write a journal.
        # critiques.sqlite is journal_mode=DELETE (default rollback journal),
        # which forces SQLite to *probe* for a hot journal on open; on a Docker
        # ``:ro`` bind mount that probe trips EROFS and SQLite surfaces it as
        # "unable to open database file". ``immutable=1`` skips the probe.
        # Trade-off: the connection assumes the file does not change while open
        # — fine here because Caio's reflexion-tick.sh writes once a week and
        # each Cockpit request opens its own short-lived connection.
        return f"file:{self._db_path.as_posix()}?mode=ro&immutable=1"

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self._build_uri(),
            uri=True,
            timeout=self.timeout_s,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout_s * 1000)}")
        return conn

    def _sync_recent_critiques(
        self,
        limit: int,
        since_iso: str | None,
    ) -> list[dict[str, Any]]:
        if not self._db_path.exists():
            raise sqlite3.OperationalError(
                f"critiques DB not found: {self._db_path}"
            )
        params: list[Any] = []
        where_clause = ""
        if since_iso:
            where_clause = "WHERE generated_at >= ?"
            params.append(since_iso)
        query = (
            "SELECT id, generated_at, approval_log_id, jid, action, "
            "contact_message, caio_suggestion, final_response, "
            "miss, hit, pattern, confidence "
            f"FROM critiques {where_clause} "
            "ORDER BY generated_at DESC "
            "LIMIT ?"
        )
        params.append(limit)
        with self._open_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": row["id"],
                "generated_at": row["generated_at"],
                "approval_log_id": row["approval_log_id"],
                "jid": row["jid"],
                "action": row["action"],
                "contact_message": row["contact_message"],
                "caio_suggestion": row["caio_suggestion"],
                "final_response": row["final_response"],
                "miss": row["miss"],
                "hit": row["hit"],
                "pattern": row["pattern"],
                "confidence": row["confidence"],
            }
            for row in rows
        ]

    async def recent_critiques(
        self,
        *,
        limit: int = 50,
        since_iso: str | None = None,
    ) -> BridgeResult:
        """Return the latest critiques as a ``BridgeResult`` (never raises)."""
        bounded_limit = max(1, min(int(limit), 500))

        async def _q() -> list[dict[str, Any]]:
            return await asyncio.to_thread(
                self._sync_recent_critiques, bounded_limit, since_iso
            )

        return await self.safe_read(_q)
