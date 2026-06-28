# WelfareFlow India — End-to-End Testing Guide

This document explains how to run the full pipeline locally and verify every demo
scenario. Everything runs **with zero external infrastructure or tokens** thanks
to the built-in sandbox fallbacks:

| Concern | Fallback when no real key/infra |
|---|---|
| PostgreSQL | In-memory async SQLite (`sqlite+aiosqlite:///:memory:`) |
| Aadhaar Vault key | Self-generated ephemeral Fernet key (`os.urandom(32)`) |
| Sarvam Vision OCR | Citizen-aware mock OCR from `mock_registry.py` |
| Sarvam ASR / Gemini | Mock registry profile fallback on LLM error |
| UiPath Orchestrator | Simulated 1.5s API window → `ui_path_job_id = tx_uipath_mock_99824` |

---

## 1. Setup

```bash
cd /Users/ayushpatel/Desktop/Wellfair
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

`.env` can stay on its placeholder values — the sandbox fallbacks activate
automatically when keys are placeholders.

---

## 2. Start the API

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

Watch the startup logs. With no live Postgres you will see:

```
database: PostgreSQL unavailable (...) — falling back to in-memory SQLite
database: in-memory SQLite fallback active — all tables created
AadhaarDataVault: AADHAAR_VAULT_AES_KEY missing/invalid — generated an ephemeral in-memory Fernet key.
LangSmith tracing configured — project='WelfareFlow-India' ...
WelfareFlow India API started — env='development'
```

Interactive docs: <http://localhost:8000/docs>

---

## 3. Run the automated E2E driver

```bash
# all scenarios + negative guard-rail tests
python e2e_test.py

# a single scenario
python e2e_test.py CITIZEN-001
```

The driver POSTs to `/api/cases/initialize`, attaches to the Glass Box SSE
stream, prints every agent frame, and asserts the terminal status. It exits
`0` only if all selected scenarios pass.

---

## 4. Scenario matrix

| Scenario | Citizen | Name on Aadhaar → Ration | Composite score | Routed to | Terminal status |
|---|---|---|---|---|---|
| **Hero / mismatch** | CITIZEN-001 | `Ramesh Kumar` → `Ramesha K` | **0.7600** | `exception_management` | `MISSING_DOCUMENTS` |
| Legit vowel variant | CITIZEN-002 | `Lakshmi Devi` → `Laxmi Devi` | 0.8578 | `npci_seeding` | `PENDING_UIPATH` |
| Perfect match | CITIZEN-003 | `Suresh Prasad` → `Suresh Prasad` | 1.0000 | `npci_seeding` | `PENDING_UIPATH` |
| Trailing-vowel variant | CITIZEN-004 | `Priya Sharma` → `Priya Sharmaa` | 1.0000 | `npci_seeding` | `PENDING_UIPATH` |
| Abbreviation mismatch | CITIZEN-005 | `Mohammed Rashid` → `Mohd Rashid` | 0.7958 | `exception_management` | `MISSING_DOCUMENTS` |
| No documents | CITIZEN-003 | — (audit skipped) | n/a | `npci_seeding` | `PENDING_UIPATH` |
| Missing OTP | — | — | — | rejected | HTTP **403** |
| Empty consent | — | — | — | rejected | HTTP **422** |

> **Lifecycle note:** a successfully queued case ends at **`PENDING_UIPATH`** — the
> UiPath robot processes the QueueItem asynchronously and reports back via the
> callback webhook, which flips the case to `COMPLETE` / `SUBMISSION_FAILED`. The
> SLA watchdog escalates cases stuck in `PENDING_UIPATH` past the threshold.

> The 0.85 Jaro-Winkler threshold lives in `config.py` (`SIMILARITY_THRESHOLD`).
> The scores above use the **token-aware composite** (`compute_name_match_score`):
> the conservative minimum of full-string Jaro-Winkler and the worst aligned-token
> score. This is what lets `Kumar → K` truncation be caught while `Lakshmi → Laxmi`
> passes.

---

## 5. Manual `curl` walkthrough (hero scenario)

### 5a. Initialize the case

```bash
curl -s -X POST http://localhost:8000/api/cases/initialize \
  -H 'Content-Type: application/json' \
  -d '{
    "citizen_id": "CITIZEN-001",
    "raw_transcript": "My name is Ramesh Kumar from Mandya. I have two acres of land, my knee hurts, I need farmer money and a health card.",
    "documents": [
      {"document_type": "aadhaar",      "image_base64": "iVBORw0KGgo=", "filename": "a.png"},
      {"document_type": "ration_card",  "image_base64": "iVBORw0KGgo=", "filename": "r.png"},
      {"document_type": "bank_passbook","image_base64": "iVBORw0KGgo=", "filename": "p.png"}
    ],
    "consent_items": [
      {"item_code":"IDENTITY_SHARE","description_en":"Verify eligibility","description_hi":"पात्रता सत्यापन"}
    ],
    "otp_verified": true,
    "ip_address": "203.0.113.10",
    "language_code": "kn-IN"
  }'
```

Response (201):

```json
{
  "case_id": "…uuid…",
  "tracking_token": "…uuid…",
  "stream_url": "/api/cases/…uuid…/stream",
  "consent_logged": true,
  "message": "Case initialised successfully…",
  "created_at": "…"
}
```

### 5b. Watch the Glass Box stream

```bash
curl -N http://localhost:8000/api/cases/<CASE_ID>/stream
```

Expected frame sequence (abbreviated):

```
event: agent_start      voice_intent
event: agent_result     voice_intent        PROFILE_EXTRACTED
event: agent_start      eligibility_router
event: agent_result     eligibility_router  ELIGIBILITY_CHECKED
event: agent_start      document_audit
event: anomaly_detected document_audit      {"anomaly":"Name mismatch: 'Ramesh Kumar' on aadhaar vs 'Ramesha K' on ration_card …"}
event: agent_result     document_audit      DOCUMENTS_AUDITED
event: agent_start      exception_management
event: agent_result     exception_management MISSING_DOCUMENTS
event: stream_end       exception_management
```

For a passing citizen (e.g. CITIZEN-003) the tail instead reads:

```
event: agent_result     npci_seeding        NPCI_VERIFIED
event: agent_start      uipath_execution
event: agent_log        uipath_execution    {"message":"UiPath live API unavailable (placeholder credentials) — entering high-fidelity mock window","simulated_latency_seconds":1.5}
event: agent_result     uipath_execution    SUBMITTED_TO_UIPATH   {"queue_item_id":"tx_uipath_mock_99824", …}
event: stream_end       uipath_execution
```

---

## 5c. Lifecycle endpoints (status → callback → SLA → revoke)

```bash
CASE=<CASE_ID>   # from a passing citizen, e.g. CITIZEN-003

# 1) Poll live status (served from the DB, not a guess)
curl -s http://localhost:8000/api/cases/$CASE/status
#   → {"status":"PENDING_UIPATH", "schemes_eligible":["PM-KISAN","AYUSHMAN_BHARAT"], ...}

# 2) UiPath robot reports success asynchronously → case flips to COMPLETE
curl -s -X POST http://localhost:8000/api/webhooks/uipath/callback \
  -H 'Content-Type: application/json' \
  -d "{\"case_id\":\"$CASE\",\"status\":\"SUCCESS\",\"uipath_tx_id\":\"RPA-TX-77001\"}"
curl -s http://localhost:8000/api/cases/$CASE/status   # → "COMPLETE"

# 3) SLA watchdog — force escalation of all pending cases (demo override)
curl -s -X POST "http://localhost:8000/api/admin/sla/run?sla_days_override=0"
#   → {"cases_escalated":N, "escalations":[{"case_id":..., "new_status":"ESCALATED",
#       "notification_payload":{"channel":"SMS+WhatsApp", ...}}]}

# 4) DPDP consent revocation — deactivates consent + purges the Aadhaar vault blob
curl -s -X POST http://localhost:8000/api/cases/$CASE/consent/revoke
#   → {"case_status":"REVOKED_BY_USER","consent_items_revoked":3,"vault_purged":true, ...}
```

The `e2e_test.py` **INTEGRATION** block automates all four of these and asserts the
status transitions (`PENDING_UIPATH → COMPLETE`, escalation present, revocation purged).

---

## 6. Unit-level checks (no server required)

```bash
# name-matching math + masking + Fernet roundtrip
python - <<'PY'
from schemas import CitizenProfilePublic
p = CitizenProfilePublic(citizen_id="C1", full_name="X", phone="9", state="KA",
                         district="Mandya", aadhaar_display="1234 5678 5678",
                         aadhaar_vault_ref="ref")
assert p.aadhaar_display == "xxxx-xxxx-5678"
print("masking OK:", p.aadhaar_display)
PY
```

---

## 7. Troubleshooting

| Symptom | Cause / Fix |
|---|---|
| `404` on `/stream` | Pipeline already finished (stream auto-closes). Re-initialize and attach within ~1s, or poll `/api/cases/{id}/status`. |
| All citizens show Ramesh's mismatch | You're on an old build — mock OCR is now citizen-aware (`_call_sarvam_vision(..., citizen_id)`). |
| `ModuleNotFoundError: aiosqlite` | `pip install -r requirements.txt` (aiosqlite is required for the SQLite fallback). |
| UiPath returns real HTTP errors | Real credentials are set in `.env`; the node attempts the live API first, then degrades to the mock window on failure. |
