"""
In-process async event bus powering the Glass Box real-time streaming feed.

Agent nodes call `publish_event(case_id, event_dict)`.
The SSE route drains events by calling `subscribe(case_id)` async generator.
A `None` sentinel pushed by `close_stream` signals end-of-stream to the consumer.

Note: `_queues` is in-process only. Multi-worker deployments (multiple uvicorn
workers or Kubernetes replicas) must route all requests for a given case_id to
the same worker instance, or replace this with Redis Pub/Sub.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator, Optional

logger: logging.Logger = logging.getLogger(__name__)

# Maps case_id -> asyncio.Queue[Optional[dict]]
_queues: dict[str, asyncio.Queue[Optional[dict[str, object]]]] = {}

# Tracks how many events were dropped per case when the queue was full
_dropped_counts: dict[str, int] = {}


def register_stream(case_id: str) -> None:
    """Create a bounded queue for a new case stream.  Call before starting the graph."""
    if case_id not in _queues:
        _queues[case_id] = asyncio.Queue(maxsize=512)
        logger.debug("event_bus: registered stream for case %s", case_id)


def get_case_queue(case_id: str) -> Optional[asyncio.Queue[Optional[dict[str, object]]]]:
    """Return the live queue for a case, or None if no stream is registered."""
    return _queues.get(case_id)


def get_dropped_count(case_id: str) -> int:
    """Return the number of events dropped for a case (exposed via health/metrics)."""
    return _dropped_counts.get(case_id, 0)


async def publish_event(case_id: str, event: dict[str, object]) -> None:
    """Non-blocking publish. If queue is full the oldest event is dropped."""
    queue: Optional[asyncio.Queue[Optional[dict[str, object]]]] = _queues.get(case_id)
    if queue is None:
        return
    if queue.full():
        try:
            queue.get_nowait()
            _dropped_counts[case_id] = _dropped_counts.get(case_id, 0) + 1
            logger.warning(
                "event_bus: queue full for case %s — oldest event dropped (total dropped: %d)",
                case_id,
                _dropped_counts[case_id],
            )
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning(
            "event_bus: queue still full after drain for case %s — event dropped", case_id
        )


async def close_stream(case_id: str) -> None:
    """Push the sentinel None to signal the consumer to stop, then clean up."""
    queue: Optional[asyncio.Queue[Optional[dict[str, object]]]] = _queues.get(case_id)
    if queue is not None:
        await queue.put(None)
    
    async def delayed_cleanup() -> None:
        await asyncio.sleep(30.0)
        _queues.pop(case_id, None)
        _dropped_counts.pop(case_id, None)
        logger.debug("event_bus: cleaned up stream memory for case %s", case_id)

    asyncio.create_task(delayed_cleanup())
    logger.debug("event_bus: closed stream for case %s", case_id)


async def subscribe(case_id: str) -> AsyncGenerator[dict[str, object], None]:
    """Async generator that yields events for a case until the sentinel is received."""
    queue: Optional[asyncio.Queue[Optional[dict[str, object]]]] = _queues.get(case_id)
    if queue is None:
        logger.warning("event_bus: no stream registered for case %s", case_id)
        return
    while True:
        item: Optional[dict[str, object]] = await queue.get()
        if item is None:
            break
        yield item
