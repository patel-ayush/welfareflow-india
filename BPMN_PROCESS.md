# WelfareFlow India — Maestro BPMN Process

> **UiPath AgentHack 2026 · Track 2: UiPath Maestro BPMN**
>
> This document describes the end-to-end **BPMN 2.0** business process that orchestrates
> citizen welfare enrolment, and how every element binds to a runtime actor (AI agent,
> RPA/API workflow, or human) in UiPath Maestro.

The model lives in [`welfareflow.bpmn`](welfareflow.bpmn) — a standards-compliant BPMN 2.0
file you can import directly into the Maestro modeling canvas.

---

## Why BPMN (Track 2)

Welfare enrolment is a **structured, end-to-end business process** with a well-defined
happy path, decision points, an exception branch, a human approval step, and a
statutory time-bound escalation. That is exactly what BPMN 2.0 was designed to express —
so we model the whole journey as one executable BPMN process and let Maestro orchestrate
agents, automations, and people against it.

---

## The process at a glance

```
                                          ┌─ Name Mismatch (error boundary) ─┐
                                          ▼                                  │
(start) Citizen ─▶ Extract ─▶ Determine ─▶ Audit Documents ──▶◇ Docs    No ─▶ Resolve ─▶ (end)
 Intake          Profile      Eligibility   (OCR + name match)  Valid?       Discrepancy   Missing
 [msg start]     [agent]      [agent]       [agent]              │            [user task]   Docs
                                                            Yes  ▼
                                              Verify NPCI Seeding [service]
                                                                 ▼
                                                       ◇ Approval Required?
                                                   Yes ▼            │ No
                                          Citizen/Operator Approval │
                                                  [user task]       │
                                                       ▼            │
                                               ◇ Approved? ─ Rejected ─▶ (end) Rejected
                                                   Approved ▼  ◀────────┘
                                              Submit Welfare Application [send]
                                                       ▼
                                              (end) Application Submitted

  ┌───────────────────────────────────────────────────────────────────────┐
  │ EVENT SUB-PROCESS  ·  non-interrupting timer                            │
  │  ⏱ 14 Days Elapsed ─▶ Escalate Appeal (SMS/WhatsApp) [send] ─▶ (end)    │
  └───────────────────────────────────────────────────────────────────────┘
```

---

## Element → implementation map

| BPMN element | Type | Bound to (Maestro) | Backend implementation |
|---|---|---|---|
| **Citizen Intake Received** | Message Start Event | Process trigger | `POST /api/cases/initialize` |
| **Extract Citizen Profile** | Agent Task | Start & wait for agent | `voice_intent_agent_node` (Sarvam STT + Gemini) |
| **Determine Scheme Eligibility** | Agent Task | Start & wait for agent | `eligibility_router_node` |
| **Audit Documents** | Agent Task | Start & wait for agent | `document_audit_node` (Sarvam Vision + Jaro-Winkler) |
| **Name Mismatch Detected** | Error Boundary Event | Business error catch | anomaly raised in `document_audit_node` |
| **Documents Valid?** | Exclusive Gateway | Condition `vars.DocumentsValid` | `route_after_document_audit` |
| **Resolve Document Discrepancy** | User Task | Action Center app task | `exception_management_node` (+ affidavit) |
| **Verify NPCI Aadhaar–Bank Seeding** | Service Task | Start & wait for RPA/API workflow | `npci_seeding_node` |
| **Human Approval Required?** | Exclusive Gateway | Condition `vars.ApprovalRequired` | `route_after_npci` |
| **Citizen / Operator Approval** | User Task | Action Center app task | `await_approval_node` + `POST /api/cases/{id}/decision` |
| **Approved?** | Exclusive Gateway | Condition `vars.ApprovalDecision` | `resume_after_approval` |
| **Submit Welfare Application** | Send Task | Integration / downstream process | `uipath_execution_node` → `uipath_maestro.start_process_instance` |
| **SLA Watchdog** | Event Sub-process (timer) | Non-interrupting 14-day timer | `sla_watchdog.py` |
| **Escalate Appeal** | Send Task | Integration Services (SMS/WhatsApp) | `run_sla_watchdog_once` |

---

## Process variables

These flow across tasks (in Maestro: process configuration variables; in the backend:
the LangGraph `WelfareWorkflowState`):

| Variable | Type | Set by | Consumed by |
|---|---|---|---|
| `CaseId`, `CitizenId`, `LanguageCode` | String | Start event | every task |
| `ExtractedProfile` | Object | Extract Profile | Eligibility, Submit |
| `EligibleSchemes` | Collection | Eligibility | Approval, Submit |
| `MinNameMatchScore` | Number | Audit Documents | Documents Valid? gateway |
| `DocumentsValid` | Boolean | Audit Documents | Documents Valid? gateway |
| `NpciSeedingStatus` | String | Verify NPCI | Submit |
| `ApprovalRequired` | Boolean | Start / config | Approval Required? gateway |
| `ApprovalDecision` | String (`Approved`/`Rejected`) | Approval user task | Approved? gateway |
| `UiPathJobId` | String | Submit | callback / status |

---

## Importing into UiPath Maestro

1. Open **bpmn.uipath.com** (or Studio Web → **New → Agentic Process**).
2. **Import** [`welfareflow.bpmn`](welfareflow.bpmn). The diagram renders with all tasks,
   gateways, the error boundary, and the SLA event sub-process.
3. **Implement** each task (the modeling → implementation step):
   - Agent tasks → *Start and wait for agent*; point each at the corresponding
     WelfareFlow agent (exposed as a UiPath Agent / coded agent or via the FastAPI
     endpoints below).
   - Service/Send tasks → *Start and wait for RPA/API workflow* or
     *Integration Services - API execution*.
   - User tasks → *Create Action Center app task* (the approval & discrepancy forms).
4. Wire the gateway conditions to the process variables (expressions shown above).
5. **Publish** to the process engine and trigger via the Message Start event.

### Binding tasks to the running backend

While iterating, every task can call the already-running FastAPI backend instead of a
re-implemented activity:

| Task | HTTP call |
|---|---|
| Start (intake) | `POST /api/cases/initialize` |
| Approval decision | `POST /api/cases/{case_id}/decision` |
| Live status (for polling/branches) | `GET /api/cases/{case_id}/status` |
| SLA escalation sweep | `POST /api/admin/sla/run` |
| Real-time observability | `GET /api/cases/{case_id}/stream` (SSE Glass Box) |

---

## How the BPMN maps to the executable backend

The backend in [`agent_graph.py`](agent_graph.py) is the **executable realisation** of this
BPMN model: each BPMN task corresponds to a node, the gateways correspond to the
`route_*` functions, the error boundary corresponds to the document-audit anomaly branch,
and the SLA event sub-process corresponds to [`sla_watchdog.py`](sla_watchdog.py). This
keeps the model (what the process *is*) and the implementation (how it *runs*) in lockstep —
the same separation Maestro enforces between **modeling** and **implementation**.
