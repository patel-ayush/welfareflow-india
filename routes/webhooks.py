"""
WelfareFlow India — Inbound webhook handlers.

`POST /api/webhooks/uipath/callback`
    UiPath robots process QueueItems asynchronously and report the outcome here.
    The request body is authenticated with an HMAC-SHA256 signature computed over
    the RAW request bytes using `UIPATH_WEBHOOK_SECRET`, compared in constant time
    against the `X-UiPath-Signature` header. A verified callback flips the case
    from PENDING_UIPATH to COMPLETE / SUBMISSION_FAILED and pushes a terminal
    Glass Box frame so the frontend stops spinning.

Sandbox: if `UIPATH_WEBHOOK_SECRET` is unset, signature verification is skipped
(with a loud warning) so the flow is demoable without configuring a secret.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select

import event_bus
from config import get_settings
from database import session_scope
from models import HouseholdCase
from schemas import UiPathCallbackPayload, UiPathCallbackResponse

logger: logging.Logger = logging.getLogger(__name__)
settings = get_settings()

router: APIRouter = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


def _compute_hmac_sha256(secret: str, raw_body: bytes) -> str:
    """Hex-encoded HMAC-SHA256 of the raw request body."""
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _verify_signature(raw_body: bytes, provided_signature: Optional[str]) -> None:
    """
    Validate the X-UiPath-Signature header against the HMAC of the raw body.

    Raises HTTP 401 on mismatch. If no secret is configured we are in sandbox
    mode and verification is skipped (logged as a warning).
    """
    secret: str = settings.uipath_webhook_secret
    if not secret:
        if settings.app_env == "production":
            # Never accept unauthenticated callbacks in production — they can flip
            # any case to COMPLETE/FAILED. Fail closed.
            logger.error("UIPATH_WEBHOOK_SECRET unset in production — rejecting callback")
            raise HTTPException(
                status_code=503,
                detail="Webhook signature secret is not configured on the server.",
            )
        logger.warning(
            "UIPATH_WEBHOOK_SECRET not set — skipping webhook signature verification (sandbox mode)"
        )
        return

    if not provided_signature:
        raise HTTPException(status_code=401, detail="Missing X-UiPath-Signature header.")

    # Accept both "sha256=<hex>" and bare "<hex>" header formats.
    candidate: str = provided_signature.strip()
    if candidate.lower().startswith("sha256="):
        candidate = candidate.split("=", 1)[1].strip()

    expected: str = _compute_hmac_sha256(secret, raw_body)
    if not hmac.compare_digest(expected, candidate):
        logger.warning("UiPath webhook signature mismatch — rejecting callback")
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")


@router.post(
    "/uipath/callback",
    response_model=UiPathCallbackResponse,
    summary="HMAC-verified UiPath QueueItem outcome callback",
)
async def uipath_callback(
    request: Request,
    x_uipath_signature: Optional[str] = Header(default=None, alias="X-UiPath-Signature"),
) -> UiPathCallbackResponse:
    # 1. Read the RAW bytes BEFORE parsing — HMAC must cover the exact payload.
    raw_body: bytes = await request.body()
    _verify_signature(raw_body, x_uipath_signature)

    # 2. Parse + validate the JSON payload against the schema.
    try:
        payload: UiPathCallbackPayload = UiPathCallbackPayload.model_validate_json(raw_body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json())) from exc

    now: datetime = datetime.now(tz=timezone.utc)
    new_status: str = "COMPLETE" if payload.status == "SUCCESS" else "SUBMISSION_FAILED"

    # 3. Verify the case exists and update its terminal outcome.
    async with session_scope() as session:
        result = await session.execute(
            select(HouseholdCase).where(HouseholdCase.case_id == payload.case_id)
        )
        db_case: Optional[HouseholdCase] = result.scalar_one_or_none()
        if db_case is None:
            raise HTTPException(
                status_code=404, detail=f"Case {payload.case_id!r} not found."
            )
        db_case.status = new_status
        db_case.current_agent = "uipath_callback"
        db_case.uipath_job_id = payload.uipath_tx_id

    # 4. Push a terminal Glass Box frame (re-register so a freshly-attached UI receives it).
    event_bus.register_stream(payload.case_id)
    await event_bus.publish_event(
        payload.case_id,
        {
            "event_id": str(uuid.uuid4()),
            "case_id": payload.case_id,
            "event_type": "agent_complete",
            "agent_name": "uipath_callback",
            "timestamp": now.isoformat(),
            "data": {
                "uipath_tx_id": payload.uipath_tx_id,
                "outcome": payload.status,
                "error_details": payload.error_details,
            },
            "status": new_status,
        },
    )
    await event_bus.close_stream(payload.case_id)

    logger.info(
        "UiPath callback for case %s — outcome=%s → status=%s",
        payload.case_id, payload.status, new_status,
    )

    return UiPathCallbackResponse(
        case_id=payload.case_id,
        accepted=True,
        new_status=new_status,
        message=f"Case {payload.case_id} updated to {new_status} from UiPath callback.",
    )
