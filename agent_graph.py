"""
WelfareFlow India — Executable backend for the Maestro BPMN process  (File 1 of 4)

Track 2 — UiPath Maestro BPMN.  This LangGraph topology is the *executable
realisation* of the BPMN 2.0 model in `welfareflow.bpmn`: each node corresponds
1:1 to a BPMN task, the `route_*` functions to BPMN exclusive gateways, the
document-audit anomaly to the BPMN error boundary event, and `sla_watchdog.py`
to the BPMN timer event sub-process.  See BPMN_PROCESS.md for the full map.

Pipeline (BPMN task → node):
  Extract Profile → voice_intent → Eligibility → eligibility_router →
  Audit Documents → document_audit → [◇ Documents Valid?] →
      ↘ Resolve Discrepancy → exception_management → END
  → Verify NPCI Seeding → npci_seeding → [◇ Approval Required?] →
      ↘ Citizen/Operator Approval → await_approval → END
  → Submit Welfare Application → uipath_execution → END

Every node publishes structured event frames to the in-process event bus so the
Glass Box SSE feed can stream real-time thinking to the frontend.

LangSmith @traceable decorators wrap every node for full trace observability.
"""
from __future__ import annotations

# IMPORTANT: observability MUST be imported before any langchain/langgraph import
# so LangSmith tracing env vars are set before LangChain initialises its tracer.
# Do not move this below the langchain imports. See observability.py for why.
import observability  # noqa: F401,E402  (intentional import-for-side-effect, first)

import asyncio
import base64
import json
import logging
import operator
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

import httpx
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langsmith import traceable
from langgraph.graph import END, StateGraph
from pydantic import BaseModel as PydanticModel, Field
from sqlalchemy import select

import database
import event_bus
import uipath_maestro
from config import get_settings
from utils.affidavit import generate_mismatch_affidavit_metadata
from mock_registry import (
    AYUSHMAN_ELIGIBILITY_RULES,
    MOCK_CITIZEN_DB,
    MOCK_NPCI_DB,
    PMKISAN_ELIGIBILITY_RULES,
    get_citizen_record,
)
from schemas import (
    DocumentAuditResult,
    DocumentFieldMatch,
    ExtractedCitizenProfile,
    NpciSeedingResult,
    SchemeEligibilityResult,
    UiPathSubmissionResult,
)

logger: logging.Logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# LLM client — Gemini Flash for low-latency orchestration loops
# ---------------------------------------------------------------------------
_llm: ChatGoogleGenerativeAI = ChatGoogleGenerativeAI(
    model="gemini-1.5-flash",
    google_api_key=settings.gemini_api_key,
    temperature=0.1,
)


async def _generate_llm_reasoning(prompt: str) -> str:
    """
    Call Gemini Flash to produce a brief plain-language reasoning string.
    Returns empty string when LLM reasoning is disabled or the API key is absent,
    so every caller can use the result without guarding against None.
    """
    if not settings.llm_reasoning_enabled or not settings.gemini_api_key:
        return ""
    try:
        response = await _llm.ainvoke([HumanMessage(content=prompt)])
        return str(response.content).strip()[:500]
    except Exception as exc:  # noqa: BLE001
        logger.debug("LLM reasoning call failed (non-fatal): %s", exc)
        return ""


# ---------------------------------------------------------------------------
# WelfareWorkflowState — TypedDict with LangGraph-compatible Annotated reducers
# ---------------------------------------------------------------------------
from typing import TypedDict  # noqa: E402 — must be after other imports


class WelfareWorkflowState(TypedDict):
    # Core identifiers
    citizen_id: str
    case_id: str
    stream_queue_id: str            # mirrors case_id; used to route SSE publishes

    # Raw input coming from the initialise endpoint
    raw_transcript: str
    audio_base64: Optional[str]     # citizen voice (base64) — transcribed if transcript empty
    documents: list[dict[str, str]] # [{document_type, image_base64, filename}]
    language_code: str
    require_approval: bool          # human-in-the-loop gate before UiPath submission

    # Agent output fields
    extracted_profile: dict[str, Any]
    eligibility_results: list[dict[str, Any]]
    document_audit_results: list[dict[str, Any]]
    validation_scores: dict[str, float]
    npci_result: dict[str, Any]
    uipath_result: dict[str, Any]

    # Anomaly accumulator (append-only via operator.add)
    anomalies: Annotated[list[str], operator.add]

    # Log accumulator (append-only via operator.add)
    agent_logs: Annotated[list[str], operator.add]

    # Pipeline control
    current_agent: str
    status: str
    uipath_job_id: Optional[str]
    error_message: Optional[str]


# ---------------------------------------------------------------------------
# Pure-Python Jaro-Winkler Implementation
# ---------------------------------------------------------------------------

def _jaro_similarity(s1: str, s2: str) -> float:
    """Compute the raw Jaro similarity score between two strings."""
    if s1 == s2:
        return 1.0
    len1: int = len(s1)
    len2: int = len(s2)
    if len1 == 0 or len2 == 0:
        return 0.0

    match_distance: int = max(len1, len2) // 2 - 1
    match_distance = max(match_distance, 0)

    s1_matches: list[bool] = [False] * len1
    s2_matches: list[bool] = [False] * len2

    matches: int = 0

    for i in range(len1):
        start: int = max(0, i - match_distance)
        end: int = min(i + match_distance + 1, len2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    transpositions: int = 0
    k: int = 0
    for i in range(len1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    jaro: float = (
        (matches / len1)
        + (matches / len2)
        + ((matches - transpositions / 2.0) / matches)
    ) / 3.0
    return jaro


def compute_jaro_winkler(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """
    Compute the Jaro-Winkler similarity score between s1 and s2.
    prefix_weight (p) is conventionally 0.1 as per the original paper.
    Returns a float in [0.0, 1.0] where 1.0 means identical.
    """
    jaro_sim: float = _jaro_similarity(s1, s2)
    max_prefix: int = min(4, len(s1), len(s2))
    prefix_length: int = 0
    for i in range(max_prefix):
        if s1[i] == s2[i]:
            prefix_length += 1
        else:
            break
    return jaro_sim + (prefix_length * prefix_weight * (1.0 - jaro_sim))


def _best_token_alignment_score(tokens_a: list[str], tokens_b: list[str]) -> float:
    """
    For each token in the shorter list, find the best-matching (highest JW)
    token in the longer list. Each longer token is consumed at most once
    (greedy assignment). Returns the minimum such best-match score — conservative,
    so a single badly-matching token still pulls the overall score down.

    This fixes the B11 false-mismatch for reordered names like
    "Ramesh Kumar" vs "Kumar Ramesh" which positional alignment scores ~0.34
    but alignment-aware matching correctly scores ~1.0.
    """
    shorter: list[str]
    longer: list[str]
    shorter, longer = (
        (tokens_a, tokens_b) if len(tokens_a) <= len(tokens_b) else (tokens_b, tokens_a)
    )
    remaining: list[str] = list(longer)
    worst: float = 1.0
    for tok in shorter:
        if not remaining:
            break
        scored: list[tuple[float, int]] = [
            (compute_jaro_winkler(tok, r), i) for i, r in enumerate(remaining)
        ]
        best_score, best_idx = max(scored, key=lambda x: x[0])
        worst = min(worst, best_score)
        remaining.pop(best_idx)
    return worst


def compute_name_match_score(name_a: str, name_b: str) -> float:
    """
    Token-aware composite name similarity used for the eligibility/routing decision.

    Plain full-string Jaro-Winkler is heavily prefix-biased and cannot detect
    token truncation such as "Ramesh Kumar" vs "Ramesha K" (it scores ~0.91 and
    would wrongly pass).  We take the conservative minimum of:
      (a) the full-string Jaro-Winkler score, and
      (b) the worst best-alignment token score (_best_token_alignment_score).

    Best-alignment avoids the old positional-alignment false mismatch for
    legitimately reordered tokens (e.g. "Ramesh Kumar" vs "Kumar Ramesh").
    Inputs MUST already be passed through preprocess_indian_name by the caller.
    """
    full_score: float = compute_jaro_winkler(name_a, name_b)
    tokens_a: list[str] = name_a.split()
    tokens_b: list[str] = name_b.split()
    if not tokens_a or not tokens_b:
        return round(full_score, 4)
    worst_token_score: float = _best_token_alignment_score(tokens_a, tokens_b)
    return round(min(full_score, worst_token_score), 4)


# ---------------------------------------------------------------------------
# Indian Phonetic Name Preprocessor
# ---------------------------------------------------------------------------

# IMPORTANT: only TRUE salutations are stripped here.  Tokens like "Kumar",
# "Devi", "Prasad", "Lal", "Das", "Rao" are GENUINE name components in India,
# not honorifics — stripping them would hide real mismatches (e.g. the PRD's
# "Ramesh Kumar" vs "Ramesha K" case, where the truncated "Kumar"->"K" is the
# very signal we must catch).  See compute_name_match_score for token handling.
_HONORIFIC_RE: re.Pattern[str] = re.compile(
    r"\b("
    r"smt|shrimati|shri|sri|sh|dr|prof|er|rev|"
    r"mr|mrs|ms|miss|master|kumari|"
    r"late|haji|syed"
    r")\b",
    re.IGNORECASE,
)

# Common transliteration vowel / consonant equivalences found on Indian govt docs
_VOWEL_MAP: dict[str, str] = {
    "AA": "A",
    "EE": "I",
    "II": "I",
    "UU": "U",
    "OO": "U",
    "AE": "E",
    "AI": "E",
    "AU": "O",
    "OU": "U",
    "SH": "S",
    "KH": "K",
    "GH": "G",
    "BH": "B",
    "DH": "D",
    "PH": "F",
    "TH": "T",
    "CH": "C",
    "JH": "J",
}


def preprocess_indian_name(name: str) -> str:
    """
    Normalise an Indian personal name for phonetic comparison:
      1. Strip leading/trailing whitespace and uppercase
      2. Remove common honorific tokens (Shri, Smt, Kumar, Devi, …)
      3. Strip non-alphabetic characters
      4. Apply vowel / consonant normalisation rules for common Indic transliterations
      5. Collapse multiple spaces and trim
    """
    processed: str = name.strip().upper()
    processed = _HONORIFIC_RE.sub(" ", processed)
    processed = re.sub(r"[^A-Z\s]", "", processed)
    for pattern, replacement in _VOWEL_MAP.items():
        processed = processed.replace(pattern, replacement)
    processed = re.sub(r"\s+", " ", processed).strip()
    return processed


# ---------------------------------------------------------------------------
# Helper: emit a structured event to the Glass Box SSE bus
# ---------------------------------------------------------------------------

async def _emit(
    case_id: str,
    event_type: str,
    agent_name: str,
    data: dict[str, Any],
    status: str = "",
) -> None:
    await event_bus.publish_event(
        case_id,
        {
            "event_id": str(uuid.uuid4()),
            "case_id": case_id,
            "event_type": event_type,
            "agent_name": agent_name,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "data": data,
            "status": status,
        },
    )


# ---------------------------------------------------------------------------
# Helper: async exponential-backoff retry wrapper for flaky outbound calls
# (Gemini / Sarvam). Retries up to settings.http_max_retries with a doubling
# delay; re-raises the last exception if every attempt fails.
# ---------------------------------------------------------------------------

from typing import Awaitable, Callable, TypeVar  # noqa: E402

_T = TypeVar("_T")


async def _retry_async(
    operation: Callable[[], Awaitable[_T]],
    *,
    label: str,
    max_attempts: Optional[int] = None,
    base_delay_seconds: Optional[float] = None,
) -> _T:
    """
    Execute a zero-arg async `operation`, retrying with exponential backoff.

    delay(attempt) = base_delay_seconds * 2**(attempt-1)
    Raises the final exception once all attempts are exhausted.
    """
    attempts: int = max_attempts if max_attempts is not None else settings.http_max_retries
    base_delay: float = (
        base_delay_seconds if base_delay_seconds is not None else settings.http_backoff_base_seconds
    )
    attempts = max(1, attempts)
    last_exc: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except Exception as exc:  # noqa: BLE001 — we re-raise after exhausting retries
            last_exc = exc
            if attempt >= attempts:
                break
            wait: float = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "retry[%s] attempt %d/%d failed: %s — backing off %.2fs",
                label, attempt, attempts, exc, wait,
            )
            await asyncio.sleep(wait)

    assert last_exc is not None
    logger.error("retry[%s] exhausted %d attempts — raising %s", label, attempts, last_exc)
    raise last_exc


# ---------------------------------------------------------------------------
# Helper: persist the terminal pipeline state back onto the HouseholdCase row
# so the /status polling endpoint and the SLA watchdog see live, correct data.
# ---------------------------------------------------------------------------

async def _persist_terminal_state(
    case_id: str,
    status_value: str,
    current_agent: str,
    uipath_job_id: Optional[str],
    anomalies: list[str],
    eligible_schemes: list[str],
) -> None:
    """
    Defensive write-back of the pipeline's terminal state. Never raises into the
    graph — a DB failure here must not crash the agent run, so we log and move on.
    Uses `database.session_scope` (resolved at call time) so the SQLite fallback
    engine is honoured.
    """
    try:
        async with database.session_scope() as session:
            from models import HouseholdCase

            result = await session.execute(
                select(HouseholdCase).where(HouseholdCase.case_id == case_id)
            )
            db_case: Optional[HouseholdCase] = result.scalar_one_or_none()
            if db_case is None:
                logger.warning(
                    "_persist_terminal_state: no HouseholdCase row for case %s", case_id
                )
                return
            # Never overwrite a user-revoked/erased case (DPDP). If consent was
            # withdrawn mid-run, the citizen's choice is final.
            _locked_statuses = {"REVOKED_BY_USER"}
            if db_case.status in _locked_statuses:
                logger.info(
                    "_persist_terminal_state: case %s is %s — not overwriting with %s",
                    case_id, db_case.status, status_value,
                )
                return
            db_case.status = status_value
            db_case.current_agent = current_agent
            db_case.uipath_job_id = uipath_job_id
            db_case.eligible_schemes = json.dumps(eligible_schemes)
            db_case.anomaly_summary = json.dumps(anomalies)
    except Exception as exc:  # noqa: BLE001 — defensive: persistence must not break the graph
        logger.error("_persist_terminal_state failed for case %s: %s", case_id, exc)


async def _persist_progress(case_id: str, status_value: str, current_agent: str) -> None:
    """
    Lightweight interim write-back so GET /status reflects live progress (not just
    the terminal state). Never raises into the graph; skips locked statuses.
    """
    try:
        async with database.session_scope() as session:
            from models import HouseholdCase

            result = await session.execute(
                select(HouseholdCase).where(HouseholdCase.case_id == case_id)
            )
            db_case: Optional[HouseholdCase] = result.scalar_one_or_none()
            if db_case is None or db_case.status in {"REVOKED_BY_USER"}:
                return
            db_case.status = status_value
            db_case.current_agent = current_agent
    except Exception as exc:  # noqa: BLE001 — progress write is best-effort
        logger.debug("_persist_progress skipped for case %s: %s", case_id, exc)


# ---------------------------------------------------------------------------
# Helper: Sarvam Saaras Speech-to-Text (voice onboarding transcription)
# ---------------------------------------------------------------------------

@traceable(run_type="tool", name="sarvam_saaras_stt")
async def transcribe_audio_saaras(
    audio_base64: str,
    language_code: str,
    citizen_id: str = "",
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """
    Transcribe citizen voice audio to text via Sarvam Saaras speech-to-text.

    Returns {"transcript", "language", "model", "mocked"}.
    In sandbox mode (mock mode / no key / API failure) it returns a CITIZEN-AWARE
    mock transcript built from the mock registry, so voice onboarding always
    produces a usable transcript with zero external calls or tokens.
    """
    def _mock() -> dict[str, Any]:
        record: dict[str, object] = get_citizen_record(citizen_id)
        name: str = str(record.get("full_name_aadhaar", "Ramesh Kumar"))
        district: str = str(record.get("district", "Mandya"))
        land: str = str(record.get("land_area_acres", 2.0))
        transcript: str = (
            f"Namaste, my name is {name}. I am from {district}. I have about {land} "
            f"acres of farm land. I want help applying for PM-Kisan and Ayushman Bharat."
        )
        return {
            "transcript": transcript,
            "language": language_code,
            "model": settings.sarvam_stt_model,
            "mocked": True,
        }

    if settings.sarvam_mock_mode or not settings.sarvam_api_key or not audio_base64:
        return _mock()

    try:
        audio_bytes: bytes = base64.b64decode(audio_base64)
    except Exception as exc:
        logger.warning("STT: could not decode audio_base64 (%s) — using mock", exc)
        return _mock()

    owns_client: bool = client is None
    http_client: httpx.AsyncClient = client or httpx.AsyncClient()

    async def _do_stt() -> httpx.Response:
        response = await http_client.post(
            f"{settings.sarvam_base_url}/speech-to-text-translate",
            headers={"api-subscription-key": settings.sarvam_api_key},
            data={"model": settings.sarvam_stt_model},
            files={"file": ("voice.webm", audio_bytes, "audio/webm")},
            timeout=45.0,
        )
        response.raise_for_status()
        return response

    try:
        response: httpx.Response = await _retry_async(_do_stt, label="sarvam.saaras.stt")
        body: dict[str, Any] = response.json()
        transcript: str = str(body.get("transcript") or body.get("text") or "").strip()
        if not transcript:
            return _mock()
        return {
            "transcript": transcript,
            "language": str(body.get("language_code") or language_code),
            "model": settings.sarvam_stt_model,
            "mocked": False,
        }
    except Exception as exc:
        logger.error("Sarvam Saaras STT failed after retries: %s — using mock", exc)
        return _mock()
    finally:
        if owns_client:
            await http_client.aclose()


# ---------------------------------------------------------------------------
# Helper: Sarvam Vision API call (document OCR)
# ---------------------------------------------------------------------------

@traceable(run_type="tool", name="sarvam_vision_ocr")
async def _call_sarvam_vision(
    image_base64: str,
    document_type: str,
    client: httpx.AsyncClient,
    citizen_id: str = "",
) -> dict[str, str]:
    """
    POST to Sarvam Vision API and return a flat dict of extracted field → value.
    Falls back to a mock OCR response if the API key is a placeholder or the
    call fails, so the pipeline continues in the sandbox environment.

    In sandbox mode the mock OCR is CITIZEN-AWARE: it returns the per-document
    name variants stored against that citizen in the mock registry, so each
    citizen's name-match scenario behaves according to their own data.
    """
    if settings.sarvam_mock_mode or not settings.sarvam_api_key:
        record: dict[str, object] = get_citizen_record(citizen_id)
        name_aadhaar: str = str(record.get("full_name_aadhaar", "Ramesh Kumar"))
        name_ration: str = str(record.get("full_name_ration_card", "Ramesha K"))
        name_passbook: str = str(record.get("full_name_passbook", "R. Kumar"))
        last4: str = str(record.get("aadhaar_last4", "5678"))
        acct: str = str(record.get("bank_account", "11223344556"))
        ifsc: str = str(record.get("bank_ifsc", "SBIN0001234"))
        district: str = str(record.get("district", "Mandya"))
        state: str = str(record.get("state", "Karnataka"))
        land: str = f"{float(str(record.get('land_area_acres', 2.0))):.2f}"

        mock_ocr: dict[str, dict[str, str]] = {
            "aadhaar": {
                "name": name_aadhaar,
                "dob": "15/08/1972",
                "gender": "Male",
                "aadhaar_number": f"XXXX XXXX {last4}",
                "address": f"Village {district}, {state}",
            },
            "ration_card": {
                "name": name_ration,
                "card_number": "KA-MNDY-2019-001234",
                "category": "APL",
                "district": district,
                "state": state,
            },
            "bank_passbook": {
                "account_holder": name_passbook,
                "account_number": f"XXXX XXXX {acct[-4:]}",
                "ifsc": ifsc,
                "bank": "State Bank of India",
            },
            "land_record": {
                "owner_name": name_aadhaar,
                "survey_number": "KA-MN-2019-0042",
                "area_acres": land,
                "taluk": district,
                "village": "Malavalli",
            },
        }
        return mock_ocr.get(document_type, {"error": "unsupported_doc_type"})

    image_bytes: bytes = base64.b64decode(image_base64)

    async def _do_post() -> httpx.Response:
        response = await client.post(
            f"{settings.sarvam_base_url}/v1/parse",
            headers={"api-subscription-key": settings.sarvam_api_key},
            data={"document_type": document_type},
            files={"file": ("document.jpg", image_bytes, "image/jpeg")},
            timeout=30.0,
        )
        response.raise_for_status()
        return response

    try:
        response: httpx.Response = await _retry_async(
            _do_post, label=f"sarvam.vision.{document_type}"
        )
        result: dict[str, Any] = response.json()
        return {k: str(v) for k, v in result.items() if isinstance(v, (str, int, float))}
    except httpx.HTTPStatusError as exc:
        logger.error("Sarvam Vision error %s: %s", exc.response.status_code, exc.response.text)
        return {"ocr_error": str(exc)}
    except Exception as exc:
        logger.error("Sarvam Vision unexpected error after retries: %s", exc)
        return {"ocr_error": str(exc)}


# ---------------------------------------------------------------------------
# Helper: Sarvam Bulbul V3 Text-to-Speech (dialect voice synthesis)
# ---------------------------------------------------------------------------

@traceable(run_type="tool", name="sarvam_bulbul_tts")
async def synthesize_agent_response_dialect(
    text: str,
    language_code: str,
    client: Optional[httpx.AsyncClient] = None,
) -> dict[str, Any]:
    """
    Synthesize `text` into regional-dialect speech via Sarvam Bulbul V3.

    Returns {"audio_base64", "text", "language", "model", "mocked"}.
    In sandbox mode (placeholder key) returns an empty-audio mock so the
    pipeline still emits a voice frame the frontend can render as a caption.
    """
    if settings.sarvam_mock_mode or not settings.sarvam_api_key:
        return {
            "audio_base64": "",
            "text": text,
            "language": language_code,
            "model": settings.sarvam_tts_model,
            "mocked": True,
        }

    owns_client: bool = client is None
    http_client: httpx.AsyncClient = client or httpx.AsyncClient()

    async def _do_tts() -> httpx.Response:
        response = await http_client.post(
            f"{settings.sarvam_base_url}/text-to-speech",
            headers={"api-subscription-key": settings.sarvam_api_key},
            json={
                "inputs": [text],
                "target_language_code": language_code,
                "speaker": settings.sarvam_tts_speaker,
                "model": settings.sarvam_tts_model,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        return response

    try:
        response: httpx.Response = await _retry_async(_do_tts, label="sarvam.bulbul.tts")
        body: dict[str, Any] = response.json()
        audios: list[str] = body.get("audios", []) if isinstance(body, dict) else []
        return {
            "audio_base64": audios[0] if audios else "",
            "text": text,
            "language": language_code,
            "model": settings.sarvam_tts_model,
            "mocked": False,
        }
    except Exception as exc:
        logger.error("Sarvam Bulbul TTS failed after retries: %s", exc)
        return {
            "audio_base64": "",
            "text": text,
            "language": language_code,
            "model": settings.sarvam_tts_model,
            "mocked": True,
        }
    finally:
        if owns_client:
            await http_client.aclose()


async def _emit_voice(case_id: str, agent_name: str, text: str, language_code: str) -> None:
    """Synthesize a dialect voice line and emit it as a Glass Box voice frame."""
    tts: dict[str, Any] = await synthesize_agent_response_dialect(text, language_code)
    await _emit(
        case_id,
        "agent_log",
        agent_name,
        {
            "voice": True,
            "tts_text": text,
            "audio_base64": tts.get("audio_base64", ""),
            "language": language_code,
            "tts_model": tts.get("model"),
            "tts_mocked": tts.get("mocked", True),
        },
    )


# ---------------------------------------------------------------------------
# Helper: UiPath Orchestrator token exchange + queue injection
# ---------------------------------------------------------------------------

async def _get_uipath_token(client: httpx.AsyncClient) -> str:
    response = await client.post(
        settings.uipath_identity_url,
        data={
            "grant_type": "client_credentials",
            "client_id": settings.uipath_client_id,
            "client_secret": settings.uipath_client_secret,
            "scope": "OR.Queues",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15.0,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


async def _inject_queue_item(
    client: httpx.AsyncClient,
    access_token: str,
    specific_content: dict[str, Any],
) -> str:
    """POST a queue item to UiPath Orchestrator OData endpoint and return its Id."""
    payload: dict[str, Any] = {
        "itemData": {
            "Name": settings.uipath_queue_name,
            "Priority": "Normal",
            "SpecificContent": specific_content,
        }
    }
    response = await client.post(
        f"{settings.uipath_orchestrator_url}/OData/QueueItems",
        json=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-UIPATH-OrganizationUnitId": "0",
        },
        timeout=15.0,
    )
    response.raise_for_status()
    return str(response.json().get("Id", str(uuid.uuid4())))


# ===========================================================================
# AGENT NODE 1 — voice_intent_agent_node
# ===========================================================================

@traceable(run_type="chain", name="voice_intent_agent")
async def voice_intent_agent_node(state: WelfareWorkflowState) -> dict[str, Any]:
    """
    Parse the raw Sarvam Saaras ASR transcript using Gemini Flash structured
    output to extract: name, location, land_area, age, health_issues, occupation.
    """
    case_id: str = state["case_id"]
    transcript: str = (state.get("raw_transcript") or "").strip()
    language_code: str = state.get("language_code", "hi-IN")

    # Voice onboarding: if no typed transcript was supplied but the citizen sent
    # audio, transcribe it first via Sarvam Saaras STT (mock-safe in sandbox).
    if not transcript and state.get("audio_base64"):
        await _emit(case_id, "agent_log", "voice_intent_agent", {
            "message": "Transcribing citizen voice via Sarvam Saaras speech-to-text",
            "model": settings.sarvam_stt_model,
        })
        stt: dict[str, Any] = await transcribe_audio_saaras(
            audio_base64=str(state.get("audio_base64") or ""),
            language_code=language_code,
            citizen_id=state.get("citizen_id", ""),
        )
        transcript = str(stt.get("transcript", "")).strip()
        await _emit(case_id, "agent_log", "voice_intent_agent", {
            "message": "Voice transcribed",
            "transcript": transcript,
            "stt_mocked": stt.get("mocked", True),
        })

    await _emit(case_id, "agent_start", "voice_intent_agent", {
        "transcript_length": len(transcript),
        "language": language_code,
    })

    system_prompt: str = (
        "You are an Indian government welfare scheme intake assistant. "
        "Extract structured citizen profile information from the following "
        "regional language transcript. The text may be code-mixed (Kannada/Hindi/Telugu "
        "mixed with English). Return ONLY a valid JSON object with these keys: "
        "full_name (str), location_state (str), location_district (str), "
        "land_area_acres (float, 0.0 if not mentioned), age (int or null), "
        "health_issues (list[str]), occupation (str, default 'Farmer'), "
        "annual_income_inr (int or null), language_detected (BCP-47 code str)."
    )

    llm_structured = _llm.with_structured_output(ExtractedCitizenProfile)

    await _emit(case_id, "agent_log", "voice_intent_agent", {
        "message": "Calling Gemini Flash with structured output schema for citizen profile extraction",
        "model": "gemini-1.5-flash",
    })

    try:
        profile: ExtractedCitizenProfile = await _retry_async(
            lambda: llm_structured.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=f"Transcript:\n{transcript}"),
                ]
            ),
            label="gemini.voice_intent",
        )
        extracted: dict[str, Any] = profile.model_dump()
    except Exception as exc:
        logger.error("voice_intent_agent LLM call failed: %s", exc)
        # Graceful fallback — extract from static or dynamic registry if citizen_id is known
        citizen_record: Optional[dict[str, object]] = get_citizen_record(state["citizen_id"]) or None
        if citizen_record:
            extracted = {
                "full_name": str(citizen_record["full_name_aadhaar"]),
                "location_state": str(citizen_record["state"]),
                "location_district": str(citizen_record["district"]),
                "land_area_acres": float(str(citizen_record["land_area_acres"])),
                "age": int(str(citizen_record["age"])),
                "health_issues": list(citizen_record.get("health_issues", [])),
                "occupation": str(citizen_record["occupation"]),
                "annual_income_inr": int(str(citizen_record["annual_income_inr"])),
                "language_detected": state.get("language_code", "hi-IN"),
            }
        else:
            extracted = {
                "full_name": "Unknown",
                "location_state": "Unknown",
                "location_district": "Unknown",
                "land_area_acres": 0.0,
                "age": None,
                "health_issues": [],
                "occupation": "Farmer",
                "annual_income_inr": None,
                "language_detected": "hi-IN",
            }

    log_entry: str = (
        f"[voice_intent_agent] Extracted profile for citizen {state['citizen_id']}: "
        f"name={extracted.get('full_name')!r}, state={extracted.get('location_state')!r}, "
        f"land={extracted.get('land_area_acres')}ac, age={extracted.get('age')}"
    )

    await _emit(case_id, "agent_result", "voice_intent_agent", {
        "extracted_profile": extracted,
        "llm_model": "gemini-1.5-flash",
    }, status="PROFILE_EXTRACTED")

    await _persist_progress(case_id, "PROFILE_EXTRACTED", "voice_intent_agent")
    return {
        "extracted_profile": extracted,
        "current_agent": "voice_intent_agent",
        "status": "PROFILE_EXTRACTED",
        "agent_logs": [log_entry],
    }


# ===========================================================================
# AGENT NODE 2 — eligibility_router_node
# ===========================================================================

@traceable(run_type="chain", name="eligibility_router_agent")
async def eligibility_router_node(state: WelfareWorkflowState) -> dict[str, Any]:
    """
    Match extracted profile indicators against PM-Kisan and Ayushman Bharat
    regional eligibility rules from the mock registry.
    """
    case_id: str = state["case_id"]
    profile: dict[str, Any] = state.get("extracted_profile", {})

    await _emit(case_id, "agent_start", "eligibility_router", {
        "checking_schemes": ["PM-KISAN", "AYUSHMAN_BHARAT"],
        "profile_summary": {
            "land_area_acres": profile.get("land_area_acres", 0.0),
            "annual_income_inr": profile.get("annual_income_inr"),
            "occupation": profile.get("occupation", ""),
        },
    })

    land_acres: float = float(profile.get("land_area_acres", 0.0))
    income_inr: Optional[int] = profile.get("annual_income_inr")
    occupation: str = str(profile.get("occupation", "")).lower()
    is_farmer: bool = "farm" in occupation or occupation == "" or land_acres > 0

    results: list[dict[str, Any]] = []
    logs: list[str] = []

    # --- PM-Kisan evaluation ---
    pmk_max_land: float = float(str(PMKISAN_ELIGIBILITY_RULES["max_land_acres"]))
    pmk_excluded: list[str] = list(PMKISAN_ELIGIBILITY_RULES["excluded_occupations"])  # type: ignore[arg-type]

    pmk_eligible: bool = (
        is_farmer
        and land_acres > 0.0
        and land_acres <= pmk_max_land
        and occupation not in pmk_excluded
    )
    pmk_reasons: list[str] = []
    if not is_farmer:
        pmk_reasons.append("Occupation is not farming")
    if land_acres == 0.0:
        pmk_reasons.append("No land area declared")
    if land_acres > pmk_max_land:
        pmk_reasons.append(f"Land area {land_acres}ac exceeds PM-Kisan ceiling of {pmk_max_land}ac")
    if occupation in pmk_excluded:
        pmk_reasons.append(f"Occupation '{occupation}' is excluded from PM-Kisan")
    if pmk_eligible:
        pmk_reasons.append("Qualifies as a marginal/small farmer")

    pmk_result: SchemeEligibilityResult = SchemeEligibilityResult(
        scheme_code="PM-KISAN",
        scheme_name="Pradhan Mantri Kisan Samman Nidhi",
        is_eligible=pmk_eligible,
        reasons=pmk_reasons,
        annual_benefit_inr=int(str(PMKISAN_ELIGIBILITY_RULES["min_annual_benefit_inr"])) if pmk_eligible else None,
    )
    results.append(pmk_result.model_dump())
    logs.append(f"[eligibility_router] PM-Kisan eligible={pmk_eligible} — {'; '.join(pmk_reasons)}")

    # --- Ayushman Bharat evaluation ---
    ab_max_income: int = int(str(AYUSHMAN_ELIGIBILITY_RULES["max_annual_income_inr"]))
    ab_excluded: list[str] = list(AYUSHMAN_ELIGIBILITY_RULES["excluded_occupations"])  # type: ignore[arg-type]

    income_ok: bool = income_inr is None or income_inr <= ab_max_income
    ab_eligible: bool = income_ok and occupation not in ab_excluded
    ab_reasons: list[str] = []
    if not income_ok:
        ab_reasons.append(f"Annual income ₹{income_inr} exceeds Ayushman ceiling of ₹{ab_max_income}")
    if occupation in ab_excluded:
        ab_reasons.append(f"Occupation '{occupation}' excluded from Ayushman Bharat")
    if ab_eligible:
        ab_reasons.append(f"Qualifies for health coverage up to ₹{AYUSHMAN_ELIGIBILITY_RULES['coverage_amount_inr']}")

    ab_result: SchemeEligibilityResult = SchemeEligibilityResult(
        scheme_code="AYUSHMAN_BHARAT",
        scheme_name="Ayushman Bharat – PM Jan Arogya Yojana",
        is_eligible=ab_eligible,
        reasons=ab_reasons,
        coverage_inr=int(str(AYUSHMAN_ELIGIBILITY_RULES["coverage_amount_inr"])) if ab_eligible else None,
    )
    results.append(ab_result.model_dump())
    logs.append(f"[eligibility_router] Ayushman eligible={ab_eligible} — {'; '.join(ab_reasons)}")

    eligible_scheme_names: list[str] = [r["scheme_name"] for r in results if r["is_eligible"]]

    # G2: LLM-generated plain-language explanation (graceful fallback to "" when key absent)
    reasoning_prompt: str = (
        f"In 1-2 simple, encouraging sentences suitable for a rural Indian citizen, "
        f"explain the eligibility outcome. "
        f"Land: {land_acres} acres. Income: ₹{income_inr or 'not stated'}. "
        f"Occupation: {occupation or 'farming'}. "
        f"Eligible schemes: {', '.join(eligible_scheme_names) or 'none found'}. "
        f"Keep it warm, positive, and jargon-free."
    )
    llm_reasoning: str = await _generate_llm_reasoning(reasoning_prompt)

    await _emit(case_id, "agent_result", "eligibility_router", {
        "eligibility_results": results,
        "eligible_schemes": eligible_scheme_names,
        "llm_reasoning": llm_reasoning,
    }, status="ELIGIBILITY_CHECKED")

    # Emit a spoken (Bulbul TTS) eligibility summary in the citizen's dialect.
    language_code: str = state.get("language_code", "hi-IN")
    if eligible_scheme_names:
        spoken: str = (
            "Based on your land and details, you are eligible for "
            + " and ".join(eligible_scheme_names)
            + ". Let us now check your documents."
        )
    else:
        spoken = (
            "Based on your details, we could not confirm a scheme yet. "
            "Let us review your documents to help you further."
        )
    await _emit_voice(case_id, "eligibility_router", spoken, language_code)

    await _persist_progress(case_id, "ELIGIBILITY_CHECKED", "eligibility_router")
    return {
        "eligibility_results": results,
        "current_agent": "eligibility_router",
        "status": "ELIGIBILITY_CHECKED",
        "agent_logs": logs,
    }


# ===========================================================================
# AGENT NODE 3 — document_audit_node
# ===========================================================================

@traceable(run_type="chain", name="document_audit_agent")
async def document_audit_node(state: WelfareWorkflowState) -> dict[str, Any]:
    """
    For each uploaded document:
      1. Call Sarvam Vision to extract text fields via layout-aware OCR
      2. Run preprocess_indian_name on all name fields
      3. Compute Jaro-Winkler scores across cross-document name pairs
      4. Flag anomalies where score < SIMILARITY_THRESHOLD
    """
    case_id: str = state["case_id"]
    documents: list[dict[str, str]] = state.get("documents", [])

    await _emit(case_id, "agent_start", "document_audit", {
        "document_count": len(documents),
        "threshold": settings.similarity_threshold,
    })

    audit_results: list[dict[str, Any]] = []
    all_anomalies: list[str] = []
    validation_scores: dict[str, float] = {}
    logs: list[str] = []

    citizen_id: str = state["citizen_id"]
    async with httpx.AsyncClient() as client:
        # Step 1: OCR all documents in parallel
        ocr_tasks: list[asyncio.Task[dict[str, str]]] = [
            asyncio.create_task(
                _call_sarvam_vision(
                    doc["image_base64"], doc["document_type"], client, citizen_id
                )
            )
            for doc in documents
        ]
        ocr_results: list[dict[str, str]] = await asyncio.gather(*ocr_tasks)

    # Step 2: Build a map of doc_type → OCR fields
    doc_ocr_map: dict[str, dict[str, str]] = {}
    for doc, ocr in zip(documents, ocr_results):
        doc_type: str = doc["document_type"]
        doc_ocr_map[doc_type] = ocr
        await _emit(case_id, "agent_log", "document_audit", {
            "message": f"Sarvam Vision OCR complete for {doc_type}",
            "fields_extracted": list(ocr.keys()),
        })
        logs.append(f"[document_audit] OCR fields from {doc_type}: {list(ocr.keys())}")

    # Step 3: Cross-document name comparison
    name_keys: list[str] = ["name", "account_holder", "owner_name"]
    cross_doc_names: dict[str, str] = {}

    for doc_type, fields in doc_ocr_map.items():
        for key in name_keys:
            if key in fields and fields[key]:
                cross_doc_names[doc_type] = fields[key]
                break

    field_matches: list[DocumentFieldMatch] = []
    doc_types_with_names: list[str] = list(cross_doc_names.keys())

    for i in range(len(doc_types_with_names)):
        for j in range(i + 1, len(doc_types_with_names)):
            src_type: str = doc_types_with_names[i]
            tgt_type: str = doc_types_with_names[j]
            src_raw: str = cross_doc_names[src_type]
            tgt_raw: str = cross_doc_names[tgt_type]

            src_preprocessed: str = preprocess_indian_name(src_raw)
            tgt_preprocessed: str = preprocess_indian_name(tgt_raw)

            # Token-aware composite (catches truncation like Kumar->K) drives routing.
            score: float = compute_name_match_score(src_preprocessed, tgt_preprocessed)
            raw_jw: float = compute_jaro_winkler(src_preprocessed, tgt_preprocessed)
            score_key: str = f"{src_type}_vs_{tgt_type}"
            validation_scores[score_key] = round(score, 4)

            passes: bool = score >= settings.similarity_threshold

            match: DocumentFieldMatch = DocumentFieldMatch(
                field_name="name",
                source_doc_type=src_type,
                target_doc_type=tgt_type,
                source_value=src_raw,
                target_value=tgt_raw,
                preprocessed_source=src_preprocessed,
                preprocessed_target=tgt_preprocessed,
                jaro_winkler_score=round(score, 4),
                passes_threshold=passes,
            )
            field_matches.append(match)
            logs.append(
                f"[document_audit] {src_type}.name={src_raw!r} ↔ "
                f"{tgt_type}.name={tgt_raw!r} | "
                f"preprocessed: {src_preprocessed!r} ↔ {tgt_preprocessed!r} | "
                f"composite={score:.4f} (raw_JW={raw_jw:.4f}) | passes={passes}"
            )

            if not passes:
                anomaly: str = (
                    f"Name mismatch: '{src_raw}' on {src_type} vs "
                    f"'{tgt_raw}' on {tgt_type} "
                    f"(Jaro-Winkler={score:.4f}, threshold={settings.similarity_threshold})"
                )
                all_anomalies.append(anomaly)
                await _emit(case_id, "anomaly_detected", "document_audit", {
                    "anomaly": anomaly,
                    "score": score,
                    "threshold": settings.similarity_threshold,
                    "recommendation": "Generate Name Mismatch Affidavit or update e-KYC on Ration Card",
                })

    for doc_type, fields in doc_ocr_map.items():
        doc_result: DocumentAuditResult = DocumentAuditResult(
            document_type=doc_type,
            ocr_fields=fields,
            field_matches=[m for m in field_matches if m.source_doc_type == doc_type or m.target_doc_type == doc_type],
            overall_score=min(
                (s for k, s in validation_scores.items() if doc_type in k),
                default=1.0,
            ),
            anomalies_detected=[a for a in all_anomalies if doc_type in a],
        )
        audit_results.append(doc_result.model_dump())

    await _emit(case_id, "agent_result", "document_audit", {
        "validation_scores": validation_scores,
        "anomalies_count": len(all_anomalies),
        "field_match_count": len(field_matches),
    }, status="DOCUMENTS_AUDITED")

    await _persist_progress(case_id, "DOCUMENTS_AUDITED", "document_audit")
    return {
        "document_audit_results": audit_results,
        "validation_scores": validation_scores,
        "anomalies": all_anomalies,
        "current_agent": "document_audit",
        "status": "DOCUMENTS_AUDITED",
        "agent_logs": logs,
    }


# ===========================================================================
# AGENT NODE 4 — npci_seeding_node
# ===========================================================================

@traceable(run_type="chain", name="npci_seeding_agent")
async def npci_seeding_node(state: WelfareWorkflowState) -> dict[str, Any]:
    """
    Look up the citizen's NPCI Aadhaar → Bank Account seeding status from the
    mock NPCI registry using the last 4 digits of their Aadhaar (sourced from
    the mock citizen DB, never from a raw input field).
    """
    case_id: str = state["case_id"]
    citizen_id: str = state["citizen_id"]

    await _emit(case_id, "agent_start", "npci_seeding", {
        "citizen_id": citizen_id,
        "lookup_source": "mock_npci_registry",
    })

    logs: list[str] = []
    citizen_record: Optional[dict[str, object]] = MOCK_CITIZEN_DB.get(citizen_id)

    if citizen_record is None:
        result: NpciSeedingResult = NpciSeedingResult(
            aadhaar_last4="XXXX",
            seeding_status="NOT_FOUND",
        )
        logs.append(f"[npci_seeding] Citizen {citizen_id} not found in mock registry")
    else:
        last4: str = str(citizen_record["aadhaar_last4"])
        npci_record: Optional[dict[str, object]] = MOCK_NPCI_DB.get(last4)

        if npci_record is None:
            result = NpciSeedingResult(
                aadhaar_last4=last4,
                seeding_status="NOT_FOUND",
            )
            logs.append(f"[npci_seeding] No NPCI record found for Aadhaar last4={last4}")
        else:
            account_raw: str = str(npci_record["bank_account"])
            account_masked: str = f"XXXX XXXX {account_raw[-4:]}" if len(account_raw) >= 4 else "XXXX"

            result = NpciSeedingResult(
                aadhaar_last4=last4,
                seeding_status=str(npci_record["seeding_status"]),  # type: ignore[arg-type]
                bank_account_masked=account_masked,
                bank_ifsc=str(npci_record["bank_ifsc"]),
                bank_name=str(npci_record["bank_name"]),
                npci_ref=str(npci_record["npci_ref"]) if npci_record.get("npci_ref") else None,
            )
            logs.append(
                f"[npci_seeding] Aadhaar xxxx-xxxx-{last4} → "
                f"Bank {result.bank_name} ({result.bank_ifsc}) | status={result.seeding_status}"
            )

    await _emit(case_id, "agent_result", "npci_seeding", {
        "seeding_status": result.seeding_status,
        "bank_name": result.bank_name,
        "bank_ifsc": result.bank_ifsc,
        "npci_ref": result.npci_ref,
    }, status="NPCI_VERIFIED")

    await _persist_progress(case_id, "NPCI_VERIFIED", "npci_seeding")
    return {
        "npci_result": result.model_dump(),
        "current_agent": "npci_seeding",
        "status": "NPCI_VERIFIED",
        "agent_logs": logs,
    }


# ===========================================================================
# AGENT NODE 5 — exception_management_node
# ===========================================================================

@traceable(run_type="chain", name="exception_management_agent")
async def exception_management_node(state: WelfareWorkflowState) -> dict[str, Any]:
    """
    Activated when any cross-document Jaro-Winkler score falls below the 0.85
    similarity threshold.  Logs the specific anomalies and routes the case to
    MISSING_DOCUMENTS status with actionable resolution instructions.
    """
    case_id: str = state["case_id"]
    anomalies: list[str] = state.get("anomalies", [])
    validation_scores: dict[str, float] = state.get("validation_scores", {})

    await _emit(case_id, "agent_start", "exception_management", {
        "anomaly_count": len(anomalies),
        "scores_below_threshold": {
            k: v for k, v in validation_scores.items()
            if v < settings.similarity_threshold
        },
    })

    resolution_steps: list[str] = []
    logs: list[str] = []

    for anomaly in anomalies:
        if "ration_card" in anomaly.lower() and "aadhaar" in anomaly.lower():
            resolution_steps.append(
                "Visit the nearest Nada Kacheri / CSC centre to update Ration Card e-KYC "
                "with the name as it appears on Aadhaar."
            )
            resolution_steps.append(
                "Alternatively, generate a Name Mismatch Affidavit (NMA) on Rs. 10 stamp paper "
                "signed by a Gazetted Officer to submit alongside the application."
            )
        if "bank_passbook" in anomaly.lower():
            resolution_steps.append(
                "Visit your bank branch with original Aadhaar to update the name in bank records."
            )
        if "land_record" in anomaly.lower():
            resolution_steps.append(
                "Visit the sub-registrar's office with original Aadhaar to correct the land record."
            )

    if not resolution_steps:
        resolution_steps.append(
            "Please visit the nearest Common Service Centre (CSC) with original documents "
            "to resolve the identified name discrepancies before resubmitting."
        )

    # Deduplicate
    resolution_steps = list(dict.fromkeys(resolution_steps))

    log_entry: str = (
        f"[exception_management] Case {case_id} routed to MISSING_DOCUMENTS. "
        f"Anomalies: {len(anomalies)}. Resolutions suggested: {len(resolution_steps)}"
    )
    logs.append(log_entry)

    # Build the print-ready Name Mismatch Affidavit metadata for the frontend
    # AffidavitViewer component (bilingual EN/KN legal template).
    affidavit_metadata: dict[str, Any] = generate_mismatch_affidavit_metadata(
        case_id=case_id, anomalies=anomalies
    )

    # G2: LLM-generated gentle explanation of the document mismatch
    exc_reasoning_prompt: str = (
        f"In 1-2 gentle, clear sentences for a rural Indian citizen, "
        f"explain that their documents have name mismatches and what they should do next. "
        f"Mismatches: {'; '.join(anomalies[:2]) or 'name differences found'}. "
        f"Be empathetic and encouraging — this is fixable."
    )
    exc_llm_reasoning: str = await _generate_llm_reasoning(exc_reasoning_prompt)

    await _emit(case_id, "agent_result", "exception_management", {
        "anomalies": anomalies,
        "resolution_steps": resolution_steps,
        "affidavit": affidavit_metadata,
        "case_blocked": True,
        "llm_reasoning": exc_llm_reasoning,
    }, status="MISSING_DOCUMENTS")

    # Speak the gentle mismatch explanation in the citizen's dialect (Bulbul TTS).
    language_code: str = state.get("language_code", "hi-IN")
    if affidavit_metadata.get("has_mismatch"):
        spoken_alert: str = (
            f"Your name is spelled differently on your documents — "
            f"{affidavit_metadata.get('declarant_aadhaar_name', '')} versus "
            f"{affidavit_metadata.get('declarant_ration_name', '')}. "
            "If we submit now, the application will be rejected. "
            "Let us fix this first with a name mismatch affidavit."
        )
    else:
        spoken_alert = (
            "Some documents need attention before we can submit. "
            "Please review the suggested steps."
        )
    await _emit_voice(case_id, "exception_management", spoken_alert, language_code)

    # Persist the terminal (blocked) state before closing the stream.
    await _persist_terminal_state(
        case_id=case_id,
        status_value="MISSING_DOCUMENTS",
        current_agent="exception_management",
        uipath_job_id=None,
        anomalies=anomalies,
        eligible_schemes=[],
    )

    await _emit(case_id, "stream_end", "exception_management", {
        "final_status": "MISSING_DOCUMENTS",
        "case_id": case_id,
    }, status="MISSING_DOCUMENTS")

    await event_bus.close_stream(case_id)

    return {
        "current_agent": "exception_management",
        "status": "MISSING_DOCUMENTS",
        "agent_logs": logs,
        "error_message": "Case halted: cross-document name similarity below threshold. " +
                         " | ".join(anomalies),
    }


# ===========================================================================
# AGENT NODE 6 — uipath_execution_node
# ===========================================================================

# ===========================================================================
# AGENT NODE 5.5 — await_approval_node  (Human-in-the-loop gate)
# ===========================================================================

# In-process store of paused cases awaiting a human decision. Single-process
# only (like the event bus); documented as such. Maps case_id -> graph state.
_PENDING_APPROVALS: dict[str, WelfareWorkflowState] = {}


@traceable(run_type="chain", name="await_approval_agent")
async def await_approval_node(state: WelfareWorkflowState) -> dict[str, Any]:
    """
    Pause before the irreversible UiPath submission and ask a human (the citizen
    or an assisting volunteer/officer) to approve. We deliberately DO NOT close
    the SSE stream here — the stream stays open showing "waiting for approval",
    and `resume_after_approval` publishes the rest onto the same stream once a
    decision arrives via POST /api/cases/{id}/decision.
    """
    case_id: str = state["case_id"]
    eligibility: list[dict[str, Any]] = state.get("eligibility_results", [])
    eligible_names: list[str] = [r["scheme_name"] for r in eligibility if r.get("is_eligible")]

    _PENDING_APPROVALS[case_id] = state

    await _persist_progress(case_id, "AWAITING_APPROVAL", "await_approval")
    await _emit(case_id, "agent_result", "await_approval", {
        "message": "Waiting for your approval before sending the application.",
        "eligible_schemes": eligible_names,
        "decision_url": f"/api/cases/{case_id}/decision",
        "requires_human": True,
    }, status="AWAITING_APPROVAL")

    language_code: str = state.get("language_code", "hi-IN")
    spoken: str = (
        "We are ready to send your application"
        + (" for " + " and ".join(eligible_names) if eligible_names else "")
        + ". Please confirm to send it."
    )
    await _emit_voice(case_id, "await_approval", spoken, language_code)

    # NOTE: no close_stream — the case is paused, not finished.
    return {
        "current_agent": "await_approval",
        "status": "AWAITING_APPROVAL",
        "agent_logs": [f"[await_approval] Case {case_id} paused for human approval."],
    }


async def resume_after_approval(case_id: str, approved: bool) -> dict[str, Any]:
    """
    Resume a case paused at the human approval gate. Called by the decision
    endpoint. Publishes onto the still-open SSE stream and then closes it.
    Returns a small result dict for the HTTP response.
    """
    state: Optional[WelfareWorkflowState] = _PENDING_APPROVALS.pop(case_id, None)
    if state is None:
        return {"resumed": False, "reason": "no_pending_approval", "case_id": case_id}

    if approved:
        await _emit(case_id, "agent_log", "await_approval", {
            "message": "Approved by human — sending the application now.",
        })
        # Execute the (previously deferred) submission node; it emits its own
        # frames, persists the terminal state, and closes the stream.
        await uipath_execution_node(state)
        return {"resumed": True, "approved": True, "case_id": case_id}

    # Rejected by the human.
    await _persist_terminal_state(
        case_id=case_id,
        status_value="REJECTED_BY_USER",
        current_agent="await_approval",
        uipath_job_id=None,
        anomalies=state.get("anomalies", []),
        eligible_schemes=[],
    )
    await _emit(case_id, "stream_end", "await_approval", {
        "final_status": "REJECTED_BY_USER",
        "case_id": case_id,
        "message": "You chose not to send the application. Nothing was submitted.",
    }, status="REJECTED_BY_USER")
    await event_bus.close_stream(case_id)
    return {"resumed": True, "approved": False, "case_id": case_id}


def route_after_npci(state: WelfareWorkflowState) -> str:
    """Route to the human approval gate when required, else straight to submission."""
    return "await_approval" if state.get("require_approval") else "uipath_execution"


# ===========================================================================
# AGENT NODE 6 — uipath_execution_node
# ===========================================================================

@traceable(run_type="chain", name="uipath_execution_agent")
async def uipath_execution_node(state: WelfareWorkflowState) -> dict[str, Any]:
    """
    Submit the validated welfare application to UiPath Maestro.

    Authentication and submission are delegated to the `uipath_maestro` module,
    which tries four tiers in order:
      1. Maestro async process trigger  (POST /orchestrator_/t/{folder}/{process})
      2. Orchestrator Jobs API          (StartJobs via release-key discovery)
      3. OData QueueItems               (classic queue fallback)
      4. In-process 1.5 s mock         (zero-infra boot / demo mode)
    """
    case_id: str = state["case_id"]
    citizen_id: str = state["citizen_id"]
    profile: dict[str, Any] = state.get("extracted_profile", {})
    npci: dict[str, Any] = state.get("npci_result", {})
    eligibility: list[dict[str, Any]] = state.get("eligibility_results", [])

    await _emit(case_id, "agent_start", "uipath_execution", {
        "orchestrator_url": settings.uipath_orchestrator_url,
        "process_name": settings.uipath_process_name,
        "tenant": settings.uipath_tenant_name,
        "citizen_id": citizen_id,
    })

    logs: list[str] = []

    # Build the BPMN process variables for the "Submit Welfare Application" send task
    # (masked Aadhaar, no raw number). These become the process variables of the
    # Maestro BPMN process instance — see welfareflow.bpmn / BPMN_PROCESS.md.
    citizen_record: Optional[dict[str, object]] = MOCK_CITIZEN_DB.get(citizen_id)
    last4: str = str(citizen_record["aadhaar_last4"]) if citizen_record else "XXXX"
    eligible_schemes: list[str] = [r["scheme_code"] for r in eligibility if r.get("is_eligible")]

    input_args: dict[str, Any] = {
        "CaseId": case_id,
        "CitizenId": citizen_id,
        "FullName": profile.get("full_name", ""),
        "State": profile.get("location_state", ""),
        "District": profile.get("location_district", ""),
        "LandAreaAcres": profile.get("land_area_acres", 0.0),
        "AnnualIncomeINR": profile.get("annual_income_inr"),
        "AadhaarDisplay": f"xxxx-xxxx-{last4}",
        "BankIFSC": npci.get("bank_ifsc", ""),
        "BankAccountMasked": npci.get("bank_account_masked", ""),
        "NpciRef": npci.get("npci_ref"),
        "NpciSeedingStatus": npci.get("seeding_status", "UNKNOWN"),
        "EligibleSchemes": eligible_schemes,
        "SubmissionSource": "WelfareFlow-India-AgentHack",
        "SubmittedAt": datetime.now(tz=timezone.utc).isoformat(),
    }

    await _emit(case_id, "agent_log", "uipath_execution", {
        "message": "Starting UiPath Maestro BPMN process instance (Submit Welfare Application)",
        "eligible_schemes": eligible_schemes,
        "tiers": ["maestro_bpmn_process", "jobs_api", "queue_items", "mock"],
    })

    # Start a BPMN process instance in Maestro, passing the BPMN process variables.
    submission: dict[str, Any] = await uipath_maestro.start_process_instance(
        case_id=case_id,
        citizen_id=citizen_id,
        process_variables=input_args,
    )

    tx_id: str = str(submission["tx_id"])
    mode: str = str(submission.get("mode", "unknown"))
    simulated: bool = bool(submission.get("simulated", mode == "mock"))
    logs.append(
        f"[uipath_execution] Submitted via {mode}"
        f"{' (SIMULATED)' if simulated else ''} | tx_id={tx_id} | status={submission.get('status')}"
    )

    await _emit(case_id, "agent_result", "uipath_execution", {
        "tx_id": tx_id,
        "mode": mode,
        "simulated": simulated,
        "submission_status": submission.get("status"),
        "poll_url": submission.get("poll_url", ""),
    }, status="SUBMITTED_TO_UIPATH")

    terminal_status: str = (
        "PENDING_UIPATH" if submission.get("status") == "QUEUED" else "SUBMISSION_FAILED"
    )

    await _persist_terminal_state(
        case_id=case_id,
        status_value=terminal_status,
        current_agent="uipath_execution",
        uipath_job_id=tx_id,
        anomalies=state.get("anomalies", []),
        eligible_schemes=eligible_schemes,
    )

    await _emit(case_id, "stream_end", "uipath_execution", {
        "final_status": terminal_status,
        "case_id": case_id,
        "tx_id": tx_id,
        "mode": mode,
        "simulated": simulated,
        "awaiting_callback": terminal_status == "PENDING_UIPATH",
    }, status=terminal_status)

    await event_bus.close_stream(case_id)

    return {
        "uipath_result": submission,
        "uipath_job_id": tx_id,
        "current_agent": "uipath_execution",
        "status": terminal_status,
        "agent_logs": logs,
    }


# ===========================================================================
# Conditional routing function — invoked by LangGraph after document_audit
# ===========================================================================

def route_after_document_audit(state: WelfareWorkflowState) -> str:
    """
    Returns the next node name based on whether all cross-document similarity
    scores meet the 0.85 Jaro-Winkler threshold.

    Three cases:
      - No documents uploaded at all       → npci_seeding (nothing to audit; OK)
      - Documents uploaded but NO name pair could be compared (unreadable OCR /
        single named doc) → exception_management (do NOT silently pass an
        un-auditable case through the fraud check)
      - All comparable pairs >= threshold  → npci_seeding
      - Any pair below threshold           → exception_management
    """
    scores: dict[str, float] = state.get("validation_scores", {})
    documents: list[dict[str, str]] = state.get("documents", [])

    if not scores:
        if documents:
            # Documents were provided but nothing comparable was extracted — we
            # cannot verify identity, so block rather than wave it through.
            logger.warning(
                "route_after_document_audit: %d document(s) but no comparable name "
                "pair — routing to exception_management", len(documents),
            )
            return "exception_management"
        return "npci_seeding"

    min_score: float = min(scores.values())
    if min_score < settings.similarity_threshold:
        return "exception_management"
    return "npci_seeding"


# ===========================================================================
# Graph assembly and compilation
# ===========================================================================

def build_welfare_graph() -> Any:
    """
    Assemble and compile the WelfareFlow LangGraph StateGraph.
    Returns the compiled graph object ready for `.ainvoke()`.
    """
    workflow: StateGraph = StateGraph(WelfareWorkflowState)

    # Register nodes
    workflow.add_node("voice_intent", voice_intent_agent_node)
    workflow.add_node("eligibility_router", eligibility_router_node)
    workflow.add_node("document_audit", document_audit_node)
    workflow.add_node("npci_seeding", npci_seeding_node)
    workflow.add_node("exception_management", exception_management_node)
    workflow.add_node("await_approval", await_approval_node)
    workflow.add_node("uipath_execution", uipath_execution_node)

    # Set entry point
    workflow.set_entry_point("voice_intent")

    # Linear edges
    workflow.add_edge("voice_intent", "eligibility_router")
    workflow.add_edge("eligibility_router", "document_audit")

    # Conditional branch from document_audit
    workflow.add_conditional_edges(
        "document_audit",
        route_after_document_audit,
        {
            "npci_seeding": "npci_seeding",
            "exception_management": "exception_management",
        },
    )

    # After NPCI: optional human-in-the-loop gate before the irreversible submit.
    workflow.add_conditional_edges(
        "npci_seeding",
        route_after_npci,
        {
            "await_approval": "await_approval",
            "uipath_execution": "uipath_execution",
        },
    )

    # Terminal edges
    workflow.add_edge("await_approval", END)
    workflow.add_edge("uipath_execution", END)
    workflow.add_edge("exception_management", END)

    return workflow.compile()


# Module-level compiled graph — imported by main.py
compiled_welfare_graph = build_welfare_graph()
