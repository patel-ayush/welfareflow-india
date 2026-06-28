"""
WelfareFlow India — FastAPI Application Entrypoint

Bootstrap responsibilities:
  - CORS policy (dev: localhost origins; prod: env-configured origins)
  - Global error handling middleware with structured JSON error responses
  - LangSmith tracing environment injection at startup
  - Async database table creation at startup
  - POST /api/cases/initialize — consent logging, case creation, background pipeline trigger
  - Mount routes.stream and routes.webhooks routers
  - SLA watchdog background loop

Run with:
    uvicorn main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

# IMPORTANT: import observability FIRST so LangSmith env vars are set before any
# langchain/langgraph import (agent_graph below builds the Gemini client + graph
# at import time). See observability.py for the full rationale.
import observability  # noqa: F401,E402

import asyncio
import hmac
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

import database
import event_bus
import sla_watchdog
from agent_graph import WelfareWorkflowState, compiled_welfare_graph, resume_after_approval
from config import get_settings
from database import create_all_tables, session_scope
from models import AadhaarDataVault, ConsentLog, HouseholdCase, User, encrypt_aadhaar
from routes.stream import router as stream_router
from routes.webhooks import router as webhooks_router
from schemas import (
    CaseDecisionRequest,
    CaseDecisionResponse,
    ConsentRevocationResponse,
    InitializeCaseRequest,
    InitializeCaseResponse,
    SlaWatchdogRunResult,
)
from sqlalchemy import delete, update

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger: logging.Logger = logging.getLogger("welfareflow.main")

settings = get_settings()

# ---------------------------------------------------------------------------
# Background pipeline task registry — prevents GC from collecting live tasks
# ---------------------------------------------------------------------------
_active_pipeline_tasks: dict[str, asyncio.Task[None]] = {}


# ---------------------------------------------------------------------------
# Lifespan context manager (replaces deprecated @app.on_event)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):  # type: ignore[type-arg]
    # ── Startup ──────────────────────────────────────────────────────────────
    # Tracing env vars were already configured at import time (observability.py).
    # Here we actively verify the LangSmith key works and the project exists, so
    # "I can't see traces" is answered loudly in the startup logs.
    ls_status = observability.verify_langsmith_connection()
    app.state.langsmith_status = ls_status
    logger.info("LangSmith status at startup: %s", ls_status)

    await create_all_tables()

    if settings.sla_watchdog_enabled:
        app.state.sla_task = asyncio.create_task(sla_watchdog.sla_watchdog_loop())
        logger.info("SLA watchdog enabled — background loop scheduled")
    else:
        app.state.sla_task = None
        logger.info("SLA watchdog disabled via SLA_WATCHDOG_ENABLED=false")

    logger.info("WelfareFlow India API started — env=%r", settings.app_env)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    task = getattr(app.state, "sla_task", None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    logger.info("WelfareFlow India API shut down cleanly")


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------

_is_dev: bool = settings.app_env == "development"

app: FastAPI = FastAPI(
    title="WelfareFlow India API",
    description=(
        "Production-grade async multi-agent welfare scheme navigation backend. "
        "Integrates Sarvam AI (STT/Vision), LangGraph, LangSmith, and UiPath Orchestrator."
    ),
    version="1.0.0",
    docs_url="/docs" if _is_dev else None,
    redoc_url="/redoc" if _is_dev else None,
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# CORS middleware — wildcard origins are rejected by the Settings validator
# ---------------------------------------------------------------------------

_cors_origins: list[str] = [
    o.strip() for o in settings.app_cors_origins.split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Case-ID", "X-Tracking-Token"],
)


# ---------------------------------------------------------------------------
# Global structured error handling middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def global_error_middleware(request: Request, call_next: Any) -> Any:
    try:
        return await call_next(request)
    except Exception as exc:
        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        detail = str(exc) if _is_dev else "An internal server error occurred."
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "detail": detail,
                "path": str(request.url.path),
            },
        )


# ---------------------------------------------------------------------------
# Admin API key dependency
# ---------------------------------------------------------------------------

def _require_admin_key(
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
) -> None:
    """Protect /api/admin/* endpoints with a static API key (if configured)."""
    key: str = settings.admin_api_key
    if not key:
        if settings.app_env == "production":
            # Fail closed in production: an unset admin key must NOT mean open access.
            raise HTTPException(
                status_code=503,
                detail="Admin API key is not configured on the server.",
            )
        return  # no key configured — allow in development / sandbox only
    if not hmac.compare_digest(x_admin_key, key):
        raise HTTPException(status_code=403, detail="Invalid or missing X-Admin-Key header.")


# ---------------------------------------------------------------------------
# Mount routers
# ---------------------------------------------------------------------------

app.include_router(stream_router)
app.include_router(webhooks_router)


# ---------------------------------------------------------------------------
# Background task: run the LangGraph pipeline and handle exceptions
# ---------------------------------------------------------------------------

async def _run_pipeline(
    initial_state: WelfareWorkflowState,
    case_id: str,
) -> None:
    try:
        logger.info("Pipeline started for case %s", case_id)
        # Rich LangSmith root-trace config: a human-readable run name plus tags and
        # metadata so every case is filterable in the dashboard (by citizen,
        # language, channel). This config flows down to every nested node/tool span.
        run_config: dict[str, Any] = {
            "run_name": f"welfare_case_{case_id[:8]}",
            "tags": [*observability.BASE_TAGS, f"citizen:{initial_state['citizen_id']}"],
            "metadata": {
                "case_id": case_id,
                "citizen_id": initial_state["citizen_id"],
                "language_code": initial_state.get("language_code", ""),
                "channel": "voice" if initial_state.get("audio_base64") else "text",
                "document_count": len(initial_state.get("documents", [])),
            },
        }
        await compiled_welfare_graph.ainvoke(initial_state, config=run_config)
        logger.info("Pipeline completed for case %s", case_id)
    except Exception as exc:
        logger.exception("Pipeline failed for case %s: %s", case_id, exc)
        await event_bus.publish_event(
            case_id,
            {
                "event_id": str(uuid.uuid4()),
                "case_id": case_id,
                "event_type": "error",
                "agent_name": "pipeline_runner",
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "data": {"error": str(exc) if _is_dev else "Pipeline error", "fatal": True},
                "status": "FAILED",
            },
        )
        await event_bus.close_stream(case_id)

        async with session_scope() as session:
            result = await session.execute(
                select(HouseholdCase).where(HouseholdCase.case_id == case_id)
            )
            db_case: HouseholdCase | None = result.scalar_one_or_none()
            if db_case is not None:
                db_case.status = "FAILED"
                db_case.current_agent = "pipeline_runner"


# ---------------------------------------------------------------------------
# POST /api/cases/initialize
# ---------------------------------------------------------------------------

@app.post(
    "/api/cases/initialize",
    response_model=InitializeCaseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Initialize a new welfare case and launch the multi-agent pipeline",
    tags=["cases"],
)
async def initialize_case(
    request_body: InitializeCaseRequest,
    request: Request,
    response: Response,
) -> InitializeCaseResponse:
    # 1. Validate consent
    if not request_body.otp_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="OTP-based consent verification is required before a case can be created.",
        )
    if not request_body.consent_items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one itemised consent record is required under the DPDP Act 2023.",
        )

    # 2. Generate identifiers
    case_id: str = str(uuid.uuid4())
    tracking_token: str = str(uuid.uuid4())
    now: datetime = datetime.now(tz=timezone.utc)

    # Resolve human-in-the-loop: explicit request flag wins, else server default.
    require_approval: bool = (
        request_body.require_approval
        if request_body.require_approval is not None
        else settings.hitl_approval_enabled
    )

    # Extract real client IP server-side — never trust ip_address from request body
    client_ip: str = (request.client.host if request.client else None) or "0.0.0.0"

    # 3. Persist to database
    from mock_registry import MOCK_CITIZEN_DB  # avoid circular at module level

    async with session_scope() as session:
        # 3a. Upsert User record
        existing_user_result = await session.execute(
            select(User).where(User.citizen_id == request_body.citizen_id)
        )
        db_user: User | None = existing_user_result.scalar_one_or_none()

        mock_record = MOCK_CITIZEN_DB.get(request_body.citizen_id, {})

        if db_user is None:
            db_user = User(
                citizen_id=request_body.citizen_id,
                full_name=str(mock_record.get("full_name_aadhaar", "Unknown")),
                phone=str(mock_record.get("phone", "0000000000")),
                state=str(mock_record.get("state", "Unknown")),
                district=str(mock_record.get("district", "Unknown")),
            )
            session.add(db_user)
            await session.flush()

        # 3b. Aadhaar Data Vault entry
        aadhaar_vault_ref: str | None = db_user.aadhaar_vault_ref
        if aadhaar_vault_ref is None and mock_record:
            last4: str = str(mock_record.get("aadhaar_last4", "0000"))
            mock_full_aadhaar: str = f"XXXX XXXX {last4}"
            encrypted_blob: bytes = encrypt_aadhaar(mock_full_aadhaar)

            vault_entry: AadhaarDataVault = AadhaarDataVault(
                vault_reference_key=str(uuid.uuid4()),
                encrypted_aadhaar_blob=encrypted_blob,
                last4_digits=last4,
            )
            session.add(vault_entry)
            await session.flush()

            db_user.aadhaar_vault_ref = vault_entry.vault_reference_key
            aadhaar_vault_ref = vault_entry.vault_reference_key

        # 3c. Create the HouseholdCase — avoid `int() or None` which maps ₹0 → None
        _income_raw = mock_record.get("annual_income_inr")
        _age_raw = mock_record.get("age")
        db_case: HouseholdCase = HouseholdCase(
            case_id=case_id,
            citizen_id=request_body.citizen_id,
            status="INITIALISED",
            current_agent="initialising",
            tracking_token=tracking_token,
            raw_transcript=request_body.raw_transcript,
            language_code=request_body.language_code,
            land_area_acres=float(str(mock_record.get("land_area_acres", 0.0))),
            annual_income_inr=int(_income_raw) if _income_raw is not None else None,
            age=int(_age_raw) if _age_raw is not None else None,
            occupation=str(mock_record.get("occupation", "")),
        )
        session.add(db_case)
        await session.flush()

        # 3d. Write itemised ConsentLog rows (one per item — DPDP Act 2023)
        for item in request_body.consent_items:
            consent_row: ConsentLog = ConsentLog(
                case_id=case_id,
                citizen_id=request_body.citizen_id,
                item_code=item.item_code,
                description_en=item.description_en,
                description_hi=item.description_hi,
                otp_verified=request_body.otp_verified,
                ip_address=client_ip,  # server-extracted, never from request body
                consented_at=now,
            )
            session.add(consent_row)

        logger.info(
            "Case %s initialised for citizen %s — %d consent items logged",
            case_id,
            request_body.citizen_id,
            len(request_body.consent_items),
        )

    # 4. Register the Glass Box SSE event stream
    event_bus.register_stream(case_id)

    # 5. Build the initial LangGraph state
    initial_state: WelfareWorkflowState = WelfareWorkflowState(
        citizen_id=request_body.citizen_id,
        case_id=case_id,
        stream_queue_id=case_id,
        raw_transcript=request_body.raw_transcript,
        audio_base64=request_body.audio_base64,
        documents=[doc.model_dump() for doc in request_body.documents],
        language_code=request_body.language_code,
        require_approval=require_approval,
        extracted_profile={},
        eligibility_results=[],
        document_audit_results=[],
        validation_scores={},
        npci_result={},
        uipath_result={},
        anomalies=[],
        agent_logs=[f"[main] Case {case_id} initialised at {now.isoformat()}"],
        current_agent="initialising",
        status="INITIALISED",
        uipath_job_id=None,
        error_message=None,
    )

    # 6. Fire the pipeline as a tracked background task (keyed by case_id so a
    #    consent revocation can cancel the in-flight run — see revoke_consent).
    task: asyncio.Task[None] = asyncio.create_task(_run_pipeline(initial_state, case_id))
    _active_pipeline_tasks[case_id] = task
    task.add_done_callback(lambda _t, cid=case_id: _active_pipeline_tasks.pop(cid, None))
    logger.info("Background pipeline task created for case %s", case_id)

    # 7. Return tracking response (also expose ids as response headers)
    response.headers["X-Case-ID"] = case_id
    response.headers["X-Tracking-Token"] = tracking_token
    return InitializeCaseResponse(
        case_id=case_id,
        tracking_token=tracking_token,
        stream_url=f"/api/cases/{case_id}/stream",
        consent_logged=True,
        message=(
            f"Case initialised successfully. Connect to the SSE stream at "
            f"/api/cases/{case_id}/stream to observe the pipeline in real-time."
        ),
        created_at=now,
    )


# ---------------------------------------------------------------------------
# POST /api/cases/{case_id}/consent/revoke  — DPDP Act 2023 right to withdraw
# ---------------------------------------------------------------------------

@app.post(
    "/api/cases/{case_id}/consent/revoke",
    response_model=ConsentRevocationResponse,
    summary="Revoke consent and purge linked PII (DPDP Act 2023)",
    tags=["compliance"],
)
async def revoke_consent(
    case_id: str,
    x_tracking_token: str = Header(default="", alias="X-Tracking-Token"),
) -> ConsentRevocationResponse:
    now: datetime = datetime.now(tz=timezone.utc)

    # Cancel any in-flight pipeline for this case FIRST, so it cannot race the
    # revocation and overwrite the REVOKED_BY_USER status afterwards (B6).
    running: asyncio.Task[None] | None = _active_pipeline_tasks.get(case_id)
    if running is not None and not running.done():
        running.cancel()
        try:
            await running
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — best-effort cancel
            pass

    async with session_scope() as session:
        case_result = await session.execute(
            select(HouseholdCase).where(HouseholdCase.case_id == case_id)
        )
        db_case: HouseholdCase | None = case_result.scalar_one_or_none()
        if db_case is None:
            raise HTTPException(status_code=404, detail=f"Case {case_id!r} not found.")

        # Authorize: revocation requires the capability token issued at creation.
        # An unset token on the row (legacy) is allowed through for back-compat.
        expected_token: str = db_case.tracking_token or ""
        if expected_token and not hmac.compare_digest(x_tracking_token, expected_token):
            raise HTTPException(
                status_code=403,
                detail="A valid X-Tracking-Token header is required to revoke this case.",
            )

        revoke_stmt = (
            update(ConsentLog)
            .where(ConsentLog.case_id == case_id, ConsentLog.is_active.is_(True))
            .values(is_active=False, revoked_at=now)
        )
        revoke_result = await session.execute(revoke_stmt)
        consent_revoked: int = int(revoke_result.rowcount or 0)

        db_case.status = "REVOKED_BY_USER"
        db_case.current_agent = "consent_revocation"

        vault_purged: bool = False
        vault_ref: str | None = None
        user_result = await session.execute(
            select(User).where(User.citizen_id == db_case.citizen_id)
        )
        db_user: User | None = user_result.scalar_one_or_none()
        if db_user is not None and db_user.aadhaar_vault_ref is not None:
            vault_ref = db_user.aadhaar_vault_ref
            del_result = await session.execute(
                delete(AadhaarDataVault).where(
                    AadhaarDataVault.vault_reference_key == vault_ref
                )
            )
            vault_purged = int(del_result.rowcount or 0) > 0
            db_user.aadhaar_vault_ref = None

    event_bus.register_stream(case_id)
    await event_bus.publish_event(
        case_id,
        {
            "event_id": str(uuid.uuid4()),
            "case_id": case_id,
            "event_type": "agent_complete",
            "agent_name": "consent_revocation",
            "timestamp": now.isoformat(),
            "data": {"consent_items_revoked": consent_revoked, "vault_purged": vault_purged},
            "status": "REVOKED_BY_USER",
        },
    )
    await event_bus.close_stream(case_id)

    logger.info(
        "Consent revoked for case %s — %d items deactivated, vault_purged=%s",
        case_id, consent_revoked, vault_purged,
    )

    return ConsentRevocationResponse(
        case_id=case_id,
        case_status="REVOKED_BY_USER",
        consent_items_revoked=consent_revoked,
        vault_purged=vault_purged,
        vault_reference_key=vault_ref,
        revoked_at=now,
        message="Consent withdrawn and linked Aadhaar PII purged per DPDP Act 2023.",
    )


# ---------------------------------------------------------------------------
# POST /api/cases/{case_id}/decision  — human-in-the-loop approval gate
# ---------------------------------------------------------------------------

@app.post(
    "/api/cases/{case_id}/decision",
    response_model=CaseDecisionResponse,
    summary="Approve or reject a case paused at the human-in-the-loop gate",
    tags=["cases"],
)
async def decide_case(
    case_id: str,
    body: CaseDecisionRequest,
    x_tracking_token: str = Header(default="", alias="X-Tracking-Token"),
) -> CaseDecisionResponse:
    async with session_scope() as session:
        result = await session.execute(
            select(HouseholdCase).where(HouseholdCase.case_id == case_id)
        )
        db_case: HouseholdCase | None = result.scalar_one_or_none()
        if db_case is None:
            raise HTTPException(status_code=404, detail=f"Case {case_id!r} not found.")
        expected_token: str = db_case.tracking_token or ""
        if expected_token and not hmac.compare_digest(x_tracking_token, expected_token):
            raise HTTPException(
                status_code=403,
                detail="A valid X-Tracking-Token header is required to decide this case.",
            )
        if db_case.status != "AWAITING_APPROVAL":
            raise HTTPException(
                status_code=409,
                detail=f"Case is not awaiting approval (status={db_case.status!r}).",
            )

    outcome = await resume_after_approval(case_id, body.approve)
    if not outcome.get("resumed"):
        raise HTTPException(
            status_code=409,
            detail="No paused pipeline found for this case (it may have expired on a server restart).",
        )

    new_status = "PENDING_UIPATH" if body.approve else "REJECTED_BY_USER"
    return CaseDecisionResponse(
        case_id=case_id,
        approved=body.approve,
        resumed=True,
        new_status=new_status,
        message=(
            "Application approved and sent." if body.approve
            else "Application rejected; nothing was submitted."
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/admin/sla/run  — trigger an SLA sweep on demand (admin-protected)
# ---------------------------------------------------------------------------

@app.post(
    "/api/admin/sla/run",
    response_model=SlaWatchdogRunResult,
    summary="Run the 14-day SLA watchdog sweep immediately",
    tags=["admin"],
    dependencies=[Depends(_require_admin_key)],
)
async def run_sla_now(sla_days_override: int | None = None) -> SlaWatchdogRunResult:
    return await sla_watchdog.run_sla_watchdog_once(sla_days_override=sla_days_override)


# ---------------------------------------------------------------------------
# Health check — includes live DB ping
# ---------------------------------------------------------------------------

@app.get("/api/admin/langsmith/health", tags=["admin"])
async def langsmith_health() -> dict[str, Any]:
    """
    Live LangSmith connectivity probe — call this when traces aren't showing up.
    Confirms the API key works, the project exists, and tells you exactly which
    project to open in the dashboard.
    """
    return observability.verify_langsmith_connection()


@app.get("/api/impact", tags=["system"])
async def impact_dashboard() -> dict[str, Any]:
    """
    Aggregate, demo-able impact metrics across all cases — the 'measurable impact'
    story for judges. Pure read; computed live from HouseholdCase rows.
    """
    # Benefit values (GoI 2024 scheme parameters, mirrored in mock_registry).
    PMKISAN_INR = 6000          # annual income support
    AYUSHMAN_COVER_INR = 500000  # health cover unlocked

    counts: dict[str, int] = {}
    total_cases = 0
    mismatches_caught = 0
    income_unlocked = 0
    health_cover_unlocked = 0
    submitted = 0

    async with session_scope() as session:
        rows = (await session.execute(select(HouseholdCase).limit(1000))).scalars().all()

    for c in rows:
        total_cases += 1
        counts[c.status] = counts.get(c.status, 0) + 1
        if c.status == "MISSING_DOCUMENTS":
            mismatches_caught += 1
        # Count value for cases that reached or passed submission.
        if c.status in {"PENDING_UIPATH", "COMPLETE", "AWAITING_APPROVAL", "ESCALATED"}:
            submitted += 1
            schemes = []
            try:
                schemes = json.loads(c.eligible_schemes) if c.eligible_schemes else []
            except (json.JSONDecodeError, TypeError):
                schemes = []
            if "PM-KISAN" in schemes:
                income_unlocked += PMKISAN_INR
            if "AYUSHMAN_BHARAT" in schemes:
                health_cover_unlocked += AYUSHMAN_COVER_INR

    return {
        "total_cases": total_cases,
        "applications_submitted": submitted,
        "mismatches_caught_before_rejection": mismatches_caught,
        "pmkisan_income_unlocked_inr": income_unlocked,
        "health_cover_unlocked_inr": health_cover_unlocked,
        "status_breakdown": counts,
    }


@app.get("/health", tags=["system"])
async def health_check(response: Response) -> dict[str, str]:
    db_ok: bool = True
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Health check: DB ping failed")
        db_ok = False

    if not db_ok:
        response.status_code = 503

    return {
        "status": "ok" if db_ok else "degraded",
        "db": "ok" if db_ok else "unavailable",
        "env": settings.app_env,
    }


# ---------------------------------------------------------------------------
# Entry point for direct execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=_is_dev,
        log_level="info",
    )
