"""
WelfareFlow India — Real-Time Glass Box Event Streaming Engine  (File 3 of 4)

Exposes:
  GET /api/cases/{case_id}/stream
    — Server-Sent Events (SSE) feed that yields every agent node's internal
      thinking, tool calls, and state transitions as structured JSON frames.

Each frame matches the AgentStreamFrame schema and is compatible with the
EventSource browser API on the frontend.

The endpoint consumes from the in-process event_bus.subscribe() async
generator and terminates cleanly when the sentinel None is received (after
the final agent calls event_bus.close_stream).
"""
from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sse_starlette.sse import EventSourceResponse

import event_bus
from database import session_scope
from schemas import AgentStreamFrame, CaseStatusResponse

logger: logging.Logger = logging.getLogger(__name__)

router: APIRouter = APIRouter(prefix="/api/cases", tags=["streaming"])


# ---------------------------------------------------------------------------
# Internal async generator: drain the event bus and yield SSE data strings
# ---------------------------------------------------------------------------

async def _stream_case_events(
    case_id: str,
    request: Request,
) -> AsyncGenerator[dict[str, str], None]:
    """
    Async generator consumed by EventSourceResponse.
    Yields dictionaries with keys 'event' and 'data' per the SSE protocol.

    The generator exits when:
      - The sentinel None arrives from the event bus (pipeline finished), OR
      - The client disconnects (request.is_disconnected()).
    """
    async for raw_event in event_bus.subscribe(case_id):
        if await request.is_disconnected():
            logger.info("SSE client disconnected for case %s", case_id)
            break

        frame: AgentStreamFrame = AgentStreamFrame(
            event_id=str(raw_event.get("event_id", "")),
            case_id=str(raw_event.get("case_id", case_id)),
            event_type=str(raw_event.get("event_type", "agent_log")),  # type: ignore[arg-type]
            agent_name=str(raw_event.get("agent_name", "")),
            timestamp=str(raw_event.get("timestamp", "")),
            data=dict(raw_event.get("data", {})),  # type: ignore[arg-type]
            status=str(raw_event.get("status", "")),
        )

        yield {
            "event": frame.event_type,
            "id": frame.event_id,
            "data": frame.model_dump_json(),
        }

    # Emit a terminal stream_end frame so the client can close EventSource
    terminal_frame: AgentStreamFrame = AgentStreamFrame(
        case_id=case_id,
        event_type="stream_end",
        agent_name="system",
        timestamp="",
        data={"message": "Pipeline complete. SSE stream closed."},
        status="CLOSED",
    )
    yield {
        "event": "stream_end",
        "id": terminal_frame.event_id,
        "data": terminal_frame.model_dump_json(),
    }


# ---------------------------------------------------------------------------
# Route: GET /api/cases/{case_id}/stream
# ---------------------------------------------------------------------------

@router.get(
    "/{case_id}/stream",
    summary="Glass Box real-time agent event stream",
    description=(
        "Server-Sent Events (SSE) endpoint that broadcasts every agent node's "
        "internal thinking, tool interactions, and state transitions in real-time. "
        "Connect with the browser EventSource API or curl --no-buffer."
    ),
    response_class=EventSourceResponse,
)
async def stream_case_events(
    case_id: str,
    request: Request,
    ping_interval: int = Query(default=15, description="SSE keepalive ping interval in seconds"),
) -> EventSourceResponse:
    """
    Opens a Server-Sent Events stream for the given case_id.

    If the case has no registered stream queue (e.g. case_id is unknown or the
    pipeline has already completed), a 404 is returned immediately.

    The stream automatically closes when:
      - The multi-agent pipeline emits its final event and calls close_stream(), OR
      - The client disconnects.
    """
    # Validate that a queue exists for this case
    if event_bus.get_case_queue(case_id) is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No active stream found for case_id={case_id!r}. "
                "The pipeline may have already completed or the case does not exist."
            ),
        )

    logger.info("SSE stream opened for case %s", case_id)

    return EventSourceResponse(
        _stream_case_events(case_id, request),
        ping=ping_interval,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering for streaming
        },
    )


# ---------------------------------------------------------------------------
# Route: GET /api/cases/{case_id}/status  (polling fallback for SSE-shy clients)
# ---------------------------------------------------------------------------

@router.get(
    "/{case_id}/status",
    response_model=CaseStatusResponse,
    summary="Poll current case pipeline status",
)
async def get_case_status(case_id: str) -> CaseStatusResponse:
    """
    Polling endpoint for clients that cannot use SSE. Reads the live, persisted
    terminal/interim state directly from the HouseholdCase row (written back by
    the agent nodes, the UiPath callback, and the SLA watchdog).
    """
    from models import HouseholdCase  # lazy import to keep Base metadata intact

    async with session_scope() as session:
        result = await session.execute(
            select(HouseholdCase).where(HouseholdCase.case_id == case_id)
        )
        db_case: HouseholdCase | None = result.scalar_one_or_none()

    if db_case is None:
        raise HTTPException(
            status_code=404, detail=f"Case {case_id!r} not found."
        )

    def _safe_list(raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            return [str(x) for x in parsed] if isinstance(parsed, list) else []
        except (json.JSONDecodeError, TypeError):
            return []

    return CaseStatusResponse(
        case_id=db_case.case_id,
        status=db_case.status,
        current_agent=db_case.current_agent,
        schemes_eligible=_safe_list(db_case.eligible_schemes),
        anomalies=_safe_list(db_case.anomaly_summary),
        uipath_job_id=db_case.uipath_job_id,
        last_updated=db_case.updated_at,
    )
