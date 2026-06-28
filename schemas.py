"""
Pydantic v2 request / response schemas for all API surface areas.

DPDP Act 2023 compliance: any schema that surfaces identity data carries an
`aadhaar_display` field whose @field_validator rewrites the raw number to the
masked form "xxxx-xxxx-1234" before serialisation.  Raw Aadhaar numbers are
never written to or read from these public schemas.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

class DocumentUpload(BaseModel):
    document_type: Literal["aadhaar", "ration_card", "bank_passbook", "land_record"]
    image_base64: str = Field(description="Base64-encoded JPEG / PNG image bytes")
    filename: str = Field(default="document.jpg")


class ConsentItem(BaseModel):
    item_code: str = Field(description="Machine-readable consent item code e.g. IDENTITY_SHARE")
    description_en: str = Field(description="Plain-English description shown to citizen")
    description_hi: str = Field(description="Hindi description shown to citizen")


# ---------------------------------------------------------------------------
# POST /api/cases/initialize  —  request & response
# ---------------------------------------------------------------------------

class InitializeCaseRequest(BaseModel):
    citizen_id: str = Field(
        description="Internal citizen identifier from citizen onboarding record"
    )
    raw_transcript: str = Field(
        default="",
        description=(
            "Raw Sarvam Saaras ASR transcript — code-mixed natural language. "
            "Optional: leave empty and supply `audio_base64` to have the backend "
            "transcribe the citizen's voice via Sarvam Saaras STT."
        ),
    )
    audio_base64: Optional[str] = Field(
        default=None,
        description=(
            "Base64-encoded audio (webm/ogg/wav/mp3) of the citizen speaking. "
            "When provided and `raw_transcript` is empty, the voice_intent agent "
            "runs Sarvam Saaras speech-to-text first."
        ),
    )
    documents: list[DocumentUpload] = Field(
        default_factory=list,
        description="Uploaded document images to be audited by Sarvam Vision",
    )
    consent_items: list[ConsentItem] = Field(
        description="Itemized DPDP Act consent items the citizen has agreed to"
    )
    otp_verified: bool = Field(description="True only if OTP-based consent confirmation succeeded")
    # NOTE: client IP is extracted server-side from the request; never trusted from
    # the body. (Removed the old `ip_address` body field — it was dead and spoofable.)
    require_approval: Optional[bool] = Field(
        default=None,
        description=(
            "Human-in-the-loop: when true, the pipeline pauses before the "
            "irreversible UiPath submission and waits for an explicit approve/reject "
            "decision. When omitted, the server default (hitl_approval_enabled) applies."
        ),
    )
    language_code: str = Field(
        default="kn-IN",
        description="BCP-47 language tag for TTS responses e.g. kn-IN, hi-IN, ta-IN",
    )


class InitializeCaseResponse(BaseModel):
    case_id: str
    tracking_token: str
    stream_url: str
    consent_logged: bool
    message: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Extracted citizen profile (internal agent state payload)
# ---------------------------------------------------------------------------

class ExtractedCitizenProfile(BaseModel):
    full_name: str = Field(default="")
    location_state: str = Field(default="")
    location_district: str = Field(default="")
    land_area_acres: float = Field(default=0.0)
    age: Optional[int] = Field(default=None)
    health_issues: list[str] = Field(default_factory=list)
    occupation: str = Field(default="")
    annual_income_inr: Optional[int] = Field(default=None)
    language_detected: str = Field(default="hi-IN")


# ---------------------------------------------------------------------------
# Eligibility result schema
# ---------------------------------------------------------------------------

class SchemeEligibilityResult(BaseModel):
    scheme_code: str
    scheme_name: str
    is_eligible: bool
    reasons: list[str] = Field(default_factory=list)
    annual_benefit_inr: Optional[int] = Field(default=None)
    coverage_inr: Optional[int] = Field(default=None)


# ---------------------------------------------------------------------------
# Document audit result schema
# ---------------------------------------------------------------------------

class DocumentFieldMatch(BaseModel):
    field_name: str
    source_doc_type: str
    target_doc_type: str
    source_value: str
    target_value: str
    preprocessed_source: str
    preprocessed_target: str
    jaro_winkler_score: float
    passes_threshold: bool


class DocumentAuditResult(BaseModel):
    document_type: str
    ocr_fields: dict[str, str] = Field(default_factory=dict)
    field_matches: list[DocumentFieldMatch] = Field(default_factory=list)
    overall_score: float = Field(default=1.0)
    anomalies_detected: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# NPCI seeding result
# ---------------------------------------------------------------------------

class NpciSeedingResult(BaseModel):
    aadhaar_last4: str
    seeding_status: Literal["SEEDED", "PENDING", "NOT_FOUND"]
    bank_account_masked: str = Field(default="")
    bank_ifsc: str = Field(default="")
    bank_name: str = Field(default="")
    npci_ref: Optional[str] = Field(default=None)


# ---------------------------------------------------------------------------
# UiPath queue submission result
# ---------------------------------------------------------------------------

class UiPathSubmissionResult(BaseModel):
    queue_item_id: str
    queue_name: str
    status: Literal["QUEUED", "FAILED"]
    submitted_at: Optional[datetime] = Field(default=None)
    error_message: Optional[str] = Field(default=None)


# ---------------------------------------------------------------------------
# Public citizen profile — Aadhaar number is masked before any response leaves
# ---------------------------------------------------------------------------

class CitizenProfilePublic(BaseModel):
    citizen_id: str
    full_name: str
    phone: str
    state: str
    district: str
    aadhaar_display: str = Field(
        description="Always rendered as xxxx-xxxx-NNNN per DPDP Act 2023"
    )
    aadhaar_vault_ref: str = Field(
        description="Non-reversible UUIDv4 reference key to the Aadhaar Data Vault"
    )

    @field_validator("aadhaar_display", mode="before")
    @classmethod
    def mask_aadhaar_number(cls, raw: Any) -> str:
        digits: str = re.sub(r"\D", "", str(raw))
        if len(digits) >= 4:
            return f"xxxx-xxxx-{digits[-4:]}"
        return "xxxx-xxxx-xxxx"


# ---------------------------------------------------------------------------
# Real-time stream event frame — emitted by every agent node to the SSE feed
# ---------------------------------------------------------------------------

class AgentStreamFrame(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    case_id: str
    event_type: Literal[
        "agent_start",
        "agent_log",
        "agent_result",
        "agent_complete",
        "anomaly_detected",
        "error",
        "stream_end",
    ]
    agent_name: str
    timestamp: str
    data: dict[str, Any] = Field(default_factory=dict)
    status: str = Field(default="")


# ---------------------------------------------------------------------------
# Consent log payload (persisted to ConsentLog table)
# ---------------------------------------------------------------------------

class ConsentLogCreate(BaseModel):
    case_id: str
    citizen_id: str
    consent_items: list[ConsentItem]
    otp_verified: bool
    ip_address: str
    consented_at: datetime


# ---------------------------------------------------------------------------
# Case status polling response
# ---------------------------------------------------------------------------

class CaseStatusResponse(BaseModel):
    case_id: str
    status: str
    current_agent: str
    schemes_eligible: list[str] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    uipath_job_id: Optional[str] = Field(default=None)
    last_updated: datetime


# ---------------------------------------------------------------------------
# Consent revocation (DPDP Act 2023 — right to withdraw)
# ---------------------------------------------------------------------------

class ConsentRevocationResponse(BaseModel):
    case_id: str
    case_status: str = Field(description="New case status, e.g. REVOKED_BY_USER")
    consent_items_revoked: int = Field(description="Count of ConsentLog rows deactivated")
    vault_purged: bool = Field(description="True if the linked Aadhaar vault blob was hard-deleted")
    vault_reference_key: Optional[str] = Field(default=None)
    revoked_at: datetime
    message: str


# ---------------------------------------------------------------------------
# UiPath asynchronous callback webhook
# ---------------------------------------------------------------------------

class UiPathCallbackPayload(BaseModel):
    case_id: str = Field(description="WelfareFlow case UUID the queue item belonged to")
    status: Literal["SUCCESS", "FAILED"]
    uipath_tx_id: str = Field(description="UiPath transaction / queue-item id")
    error_details: Optional[str] = Field(default=None)


class UiPathCallbackResponse(BaseModel):
    case_id: str
    accepted: bool
    new_status: str
    message: str


# ---------------------------------------------------------------------------
# Human-in-the-loop approval decision
# ---------------------------------------------------------------------------

class CaseDecisionRequest(BaseModel):
    approve: bool = Field(description="True to send the application, False to reject it.")


class CaseDecisionResponse(BaseModel):
    case_id: str
    approved: bool
    resumed: bool
    new_status: str
    message: str


# ---------------------------------------------------------------------------
# SLA watchdog escalation result
# ---------------------------------------------------------------------------

class SlaEscalationRecord(BaseModel):
    case_id: str
    citizen_id: str
    previous_status: str
    new_status: str = Field(default="ESCALATED")
    days_stuck: float
    notification_channel: str = Field(default="SMS+WhatsApp")
    notification_payload: dict[str, Any] = Field(default_factory=dict)


class SlaWatchdogRunResult(BaseModel):
    scanned_at: datetime
    sla_days_threshold: int
    cases_scanned: int
    cases_escalated: int
    escalations: list[SlaEscalationRecord] = Field(default_factory=list)
