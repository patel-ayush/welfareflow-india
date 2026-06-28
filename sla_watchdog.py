"""
WelfareFlow India — 14-Day SLA Watchdog (PRD Step 5).

Background monitor that enforces the state "Right to Service" SLA. It periodically
scans for HouseholdCase rows that are stuck awaiting downstream processing
(`PENDING_UIPATH` / `UNDER_REVIEW`) and whose `updated_at` is older than the SLA
threshold (default 14 days). For each breach it:

  1. Sets the case status to ESCALATED.
  2. Mocks an outbound SMS/WhatsApp appeal notification (logged, not sent).
  3. Emits an escalation alert frame to the Glass Box `event_bus`.

The loop runs as an asyncio background task started from main.py's startup hook.
A run can also be triggered on demand (with an optional day-threshold override)
via the admin endpoint, which is what makes the flow demoable without waiting
14 real days.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

import database
import event_bus
from config import get_settings
from schemas import SlaEscalationRecord, SlaWatchdogRunResult

logger: logging.Logger = logging.getLogger(__name__)
settings = get_settings()

# Statuses considered "in-flight" and therefore subject to the SLA clock.
MONITORED_STATUSES: tuple[str, ...] = ("PENDING_UIPATH", "UNDER_REVIEW")


def mock_send_appeal_notification(
    citizen_id: str,
    phone: str,
    case_id: str,
    days_stuck: float,
) -> dict[str, object]:
    """
    Build (and log) a mock SMS/WhatsApp outbound payload for an automated appeal.
    In production this would call a messaging gateway; here it is a pure mock.
    """
    message: str = (
        f"WelfareFlow: Your application (case {case_id[:8]}) has been delayed "
        f"{int(days_stuck)} days beyond the Right-to-Service SLA. We have queued an "
        f"automated appeal to the higher authority. You don't need to do anything."
    )
    payload: dict[str, object] = {
        "channel": "SMS+WhatsApp",
        "to_phone": phone,
        "citizen_id": citizen_id,
        "case_id": case_id,
        "message": message,
        "appeal_triggered": True,
        "dispatched_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    logger.info("SLA watchdog: MOCK appeal notification → %s | %s", phone, message)
    return payload


def _days_between(now: datetime, then: Optional[datetime]) -> float:
    """Defensive day-delta that tolerates naive timestamps (SQLite returns naive)."""
    if then is None:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 86400.0


async def run_sla_watchdog_once(
    sla_days_override: Optional[int] = None,
) -> SlaWatchdogRunResult:
    """
    Execute a single SLA sweep. Returns a structured result of what was escalated.

    `sla_days_override` lets a caller (e.g. the demo admin endpoint) lower the
    threshold to 0 so currently-pending cases escalate immediately.
    """
    sla_days: int = (
        sla_days_override if sla_days_override is not None else settings.sla_days_threshold
    )
    now: datetime = datetime.now(tz=timezone.utc)
    cutoff: datetime = now - timedelta(days=sla_days)

    escalations: list[SlaEscalationRecord] = []
    scanned: int = 0

    # Import models lazily so Base metadata is fully registered first.
    from models import HouseholdCase, User

    _BATCH_SIZE: int = 100

    async with database.session_scope() as session:
        # Join to User to obtain the phone number for the notification.
        # LIMIT prevents loading the entire table when case volume is large.
        result = await session.execute(
            select(HouseholdCase, User)
            .join(User, User.citizen_id == HouseholdCase.citizen_id)
            .where(HouseholdCase.status.in_(MONITORED_STATUSES))
            .limit(_BATCH_SIZE)
        )
        rows = result.all()
        scanned = len(rows)

        for db_case, db_user in rows:
            db_case: HouseholdCase
            db_user: User
            days_stuck: float = _days_between(now, db_case.updated_at)
            if db_case.updated_at is not None and db_case.updated_at.tzinfo is None:
                # normalise for the cutoff comparison
                stuck = db_case.updated_at.replace(tzinfo=timezone.utc) <= cutoff
            else:
                stuck = (db_case.updated_at is None) or (db_case.updated_at <= cutoff)
            if not stuck:
                continue

            previous_status: str = db_case.status
            db_case.status = "ESCALATED"
            db_case.current_agent = "sla_watchdog"

            notification: dict[str, object] = mock_send_appeal_notification(
                citizen_id=db_case.citizen_id,
                phone=db_user.phone,
                case_id=db_case.case_id,
                days_stuck=days_stuck,
            )

            record: SlaEscalationRecord = SlaEscalationRecord(
                case_id=db_case.case_id,
                citizen_id=db_case.citizen_id,
                previous_status=previous_status,
                new_status="ESCALATED",
                days_stuck=round(days_stuck, 2),
                notification_channel="SMS+WhatsApp",
                notification_payload=notification,
            )
            escalations.append(record)

            # Emit an escalation frame to the Glass Box (no-op if no live listener).
            await event_bus.publish_event(
                db_case.case_id,
                {
                    "event_id": str(uuid.uuid4()),
                    "case_id": db_case.case_id,
                    "event_type": "anomaly_detected",
                    "agent_name": "sla_watchdog",
                    "timestamp": now.isoformat(),
                    "data": {
                        "alert": "SLA_BREACH_ESCALATION",
                        "previous_status": previous_status,
                        "days_stuck": round(days_stuck, 2),
                        "appeal": notification,
                    },
                    "status": "ESCALATED",
                },
            )

    result_model: SlaWatchdogRunResult = SlaWatchdogRunResult(
        scanned_at=now,
        sla_days_threshold=sla_days,
        cases_scanned=scanned,
        cases_escalated=len(escalations),
        escalations=escalations,
    )
    logger.info(
        "SLA watchdog sweep: scanned=%d escalated=%d (threshold=%d days)",
        scanned,
        len(escalations),
        sla_days,
    )
    return result_model


async def sla_watchdog_loop() -> None:
    """
    Long-lived background loop. Sleeps `sla_watchdog_interval_seconds` between
    sweeps. Cancellation (on app shutdown) is handled gracefully.
    Consecutive failures are counted; a critical alert is emitted after 5 to
    prompt operator investigation (e.g. DB permanently unavailable).
    """
    _MAX_CONSECUTIVE_FAILURES: int = 5
    interval: int = settings.sla_watchdog_interval_seconds
    consecutive_failures: int = 0
    logger.info("SLA watchdog loop started — interval=%ds", interval)
    try:
        while True:
            try:
                await run_sla_watchdog_once()
                consecutive_failures = 0
            except Exception as exc:  # noqa: BLE001 — never let one bad sweep kill the loop
                consecutive_failures += 1
                logger.error(
                    "SLA watchdog sweep failed (%d/%d consecutive): %s",
                    consecutive_failures,
                    _MAX_CONSECUTIVE_FAILURES,
                    exc,
                )
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.critical(
                        "SLA watchdog: %d consecutive failures — operators should investigate. "
                        "Watchdog will keep retrying.",
                        consecutive_failures,
                    )
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SLA watchdog loop cancelled — shutting down cleanly")
        raise
