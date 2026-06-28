# WelfareFlow India 🇮🇳

> **UiPath AgentHack 2026 — Track 2: UiPath Maestro BPMN**
>
> Helping rural Indian families navigate government welfare schemes — by voice, in their own language, with zero paperwork confusion.
>
> The complete enrolment journey is modelled as an executable **BPMN 2.0** process in
> UiPath Maestro: see [`welfareflow.bpmn`](welfareflow.bpmn) and [BPMN_PROCESS.md](BPMN_PROCESS.md).

---

## What It Does

1. **Voice onboarding** — citizen speaks (or types) in Kannada / Hindi / Telugu / Marathi / Tamil.  Sarvam Saaras V2 STT transcribes; Gemini Flash extracts the citizen profile.
2. **Eligibility routing** — rule-based check against PM-Kisan and Ayushman Bharat criteria, with LLM-generated plain-language reasoning.
3. **Document audit** — Sarvam Vision OCR reads Aadhaar, Ration Card and Bank Passbook; Jaro-Winkler + best-alignment token matching flags cross-document name mismatches before they cause a government rejection.
4. **NPCI seeding check** — verifies Aadhaar↔Bank account link required for DBT transfers.
5. **UiPath Maestro submission** — the verified application starts a downstream UiPath process instance (the BPMN "Submit Welfare Application" send task) via a tiered authentication flow.  Honest mock fallback when live creds are absent.
6. **Human-in-the-loop gate** — supervisor can review and approve/reject before the irreversible submission.
7. **Name Mismatch Affidavit** — bilingual (English + Kannada) legal template generated on-the-fly, ready to print on Rs. 10 stamp paper.
8. **DPDP Act 2023 compliance** — itemised consent, Aadhaar Data Vault (AES-128 Fernet), right to withdraw, immutable consent log.

---

## Architecture

The process is **modelled in BPMN 2.0** ([`welfareflow.bpmn`](welfareflow.bpmn)) and
**executed** by the FastAPI + LangGraph backend. The BPMN tasks and the backend nodes are
a 1:1 mapping (see [BPMN_PROCESS.md](BPMN_PROCESS.md)).

```
UiPath Maestro — BPMN 2.0 process (welfareflow.bpmn)
  (start) Intake ▶ Extract Profile ▶ Eligibility ▶ Audit Docs ▶◇Valid?▶ NPCI ▶◇Approval?▶ Submit ▶(end)
     [agent tasks]            [agent]    [agent + error boundary]   [service]  [user task]   [send]
     + event sub-process: ⏱14-day SLA watchdog ▶ escalate appeal
                          │  implemented by ▼
Browser (Next.js 14)
    │  SSE Glass Box stream
    ▼
FastAPI 0.111  ←→  LangGraph 0.2 multi-agent pipeline (the BPMN execution engine)
    │                   ├─ voice_intent      → Agent Task "Extract Citizen Profile"
    │                   ├─ eligibility_router → Agent Task "Determine Scheme Eligibility"
    │                   ├─ document_audit     → Agent Task "Audit Documents" (+ error boundary)
    │                   ├─ npci_seeding       → Service Task "Verify NPCI Seeding"
    │                   ├─ await_approval     → User Task "Citizen / Operator Approval"
    │                   └─ uipath_execution   → Send Task "Submit Welfare Application"
    │
    ├─ SQLite (dev) / PostgreSQL (prod)  — process-instance state + consent log
    ├─ LangSmith                         — full trace observability
    └─ UiPath Maestro                    — BPMN process trigger + webhook callback
```

---

## Quick Start

### Backend

```bash
cd /path/to/Wellfair
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in API keys (see HANDOFF.md)

uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:3000
```

### End-to-End Tests

```bash
python e2e_test.py   # produces qa_report.html — open in browser
```

Expected: **13/13 PASS**.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Yes | Google Gemini Flash for orchestration |
| `LANGSMITH_API_KEY` | Recommended | Trace observability in LangSmith |
| `SARVAM_API_KEY` | Optional | Real STT/TTS (mock used when absent) |
| `UIPATH_ORCHESTRATOR_URL` | Optional | UiPath cloud tenant (mock when absent) |
| `UI_PATH_APP_ID` + `UIPATH_APP_SECRET` | Optional | UiPath OAuth2 |
| `DATABASE_URL` | Optional | PostgreSQL URL (SQLite used when absent) |
| `AADHAAR_VAULT_AES_KEY` | Production | Fernet key for Aadhaar vault |
| `ADMIN_API_KEY` | Production | Protects `/api/admin/*` endpoints |
| `UIPATH_WEBHOOK_SECRET` | Production | HMAC webhook verification |

---

## Key Design Decisions

- **No Aadhaar numbers in logs or responses** — always masked as `xxxx-xxxx-NNNN` per DPDP Act 2023.
- **Honest mock labelling** — every response from mock UiPath carries `"mode": "mock", "simulated": true`; no fake "SUCCESS" signals.
- **Best-alignment name matching** — token-aware Jaro-Winkler with greedy best-assignment avoids false mismatches for reordered names (e.g. "Kumar Ramesh" vs "Ramesh Kumar").
- **LangSmith traces from import time** — `observability.py` is the first import in every entry-point so LangChain's tracer is initialised before any other module.

---

## Built With

- **UiPath Maestro (BPMN 2.0)** — the welfare journey is modelled as an end-to-end
  executable BPMN process ([`welfareflow.bpmn`](welfareflow.bpmn)): agent tasks, service
  tasks, user tasks, exclusive gateways, an error boundary event, and a timer-driven
  SLA event sub-process
- **Claude Code** (Anthropic) — AI-assisted development throughout this hackathon
- **LangGraph 0.2** — multi-agent stateful pipeline
- **Sarvam AI** — Saaras V2 STT, Bulbul V3 TTS, Vision OCR (all India-local)
- **Google Gemini Flash** — low-latency LLM orchestration
- **FastAPI + SQLAlchemy 2.0 async** — backend API
- **Next.js 14 + Tailwind CSS** — villager-friendly frontend
- **LangSmith** — observability and tracing

---

## License

MIT — see [LICENSE](LICENSE).
