# WelfareFlow India — Build Task Log

UiPath AgentHack submission · **Track 2: UiPath Maestro BPMN** · <https://uipath-agenthack.devpost.com/>

A detailed, chronological record of everything implemented for the production-ready
async Python backend. Each task notes *what* was built and *why*.

---

## Phase 0 — Project intake

- [x] **Read the PRD** (`PRD.txt`) — Ramesh Kumar's 5-step journey: voice entry →
      document audit → consent → one-click submission → exception watchdog.
- [x] **Read `.env`** — catalogued the integration handles: LangSmith, Gemini,
      Sarvam, UiPath (orchestrator/client/secret), Postgres DSN, Aadhaar vault key.

---

## Phase 1 — Core infrastructure files

- [x] **`requirements.txt`** — pinned the full stack: FastAPI, uvicorn, Pydantic v2,
      SQLAlchemy 2 (async) + asyncpg **+ aiosqlite** (fallback), LangGraph,
      LangSmith, `langchain-google-genai`, httpx, cryptography, `sse-starlette`.
- [x] **`config.py`** — `pydantic-settings` `Settings` object reading every handle
      via `os.getenv`; `get_settings()` is `lru_cache`d. Added `SIMILARITY_THRESHOLD`
      (0.85) and CORS origins as tunables.
- [x] **`event_bus.py`** — in-process async pub/sub powering the Glass Box feed.
      Per-case `asyncio.Queue(maxsize=512)` with drop-oldest eviction (no
      back-pressure on agents). `register_stream` / `publish_event` /
      `close_stream` / `subscribe` / `get_case_queue`.
- [x] **`mock_registry.py`** — sandbox "verification" registries:
      5 synthetic citizens (each with Aadhaar/Ration/Passbook name variants),
      PM-Kisan rules, Ayushman Bharat rules, and a mock NPCI Aadhaar→bank map.

---

## Phase 2 — Data layer

- [x] **`database.py`** — async SQLAlchemy engine factory; converts `postgresql://`
      → `postgresql+asyncpg://`; `get_db_session` transactional dependency.
- [x] **`schemas.py`** — Pydantic v2 request/response models. Key piece:
      `CitizenProfilePublic.aadhaar_display` carries a `@field_validator` that
      rewrites any raw number to **`xxxx-xxxx-1234`** before serialization (DPDP Act).
- [x] **`models.py`** — 4 async ORM models:
      `User`, `HouseholdCase`, `ConsentLog` (insert-only DPDP audit trail with a
      unique `(case_id, item_code)` constraint), and the isolated
      **`AadhaarDataVault`** (Fernet-encrypted blob + UUIDv4 `vault_reference_key`;
      only the ref key is shared with other tables).

---

## Phase 3 — Multi-agent graph (`agent_graph.py`)

- [x] **`WelfareWorkflowState`** TypedDict with `operator.add` reducers on
      `anomalies` and `agent_logs` (append-merge across nodes).
- [x] **Pure-Python Jaro-Winkler** — `_jaro_similarity` + `compute_jaro_winkler`
      (no external dependency), prefix weight 0.1.
- [x] **Indian phonetic preprocessor** — `preprocess_indian_name`: uppercase,
      strip salutations, drop non-alpha, normalize vowel/consonant transliteration
      pairs (AA→A, EE→I, SH→S, BH→B, …), collapse whitespace.
- [x] **6 async agent nodes**, each emitting `agent_start` / `agent_log` /
      `agent_result` frames to the event bus and wrapped in LangSmith `@traceable`:
  - `voice_intent_agent_node` — Gemini Flash structured extraction → profile.
  - `eligibility_router_node` — PM-Kisan + Ayushman Bharat rule evaluation.
  - `document_audit_node` — Sarvam Vision OCR → preprocess → score → anomalies.
  - `npci_seeding_node` — mock NPCI Aadhaar→bank seeding lookup.
  - `exception_management_node` — `MISSING_DOCUMENTS` loop + affidavit/e-KYC steps.
  - `uipath_execution_node` — OAuth2 token exchange + `/OData/QueueItems` injection.
- [x] **`StateGraph` assembly** — linear edges + a conditional edge after
      `document_audit` (`route_after_document_audit`) that diverts to
      `exception_management` when the min score < 0.85. Compiled at module load.

---

## Phase 4 — API surface

- [x] **`routes/stream.py`** — `GET /api/cases/{case_id}/stream` SSE Glass Box feed
      (disconnect-safe async generator, keepalive ping) + `/status` polling fallback.
- [x] **`main.py`** — FastAPI bootstrap: CORS, global error middleware, LangSmith
      env injection + table creation on startup, and
      **`POST /api/cases/initialize`** which upserts the user, writes the encrypted
      vault entry, logs itemized consent, creates the case, registers the stream,
      and fires the LangGraph pipeline as a background task.
- [x] **`/health`** endpoint.

---

## Phase 5 — Sandbox fallback patterns (zero-infra boot)

- [x] **Auto DB setup** — `create_all_tables()` tries Postgres, then transparently
      falls back to **in-memory async SQLite** (`StaticPool`, shared connection),
      rebinding the module-level engine + session factory.
- [x] **Self-generating vault key** — `_resolve_vault_key()` validates
      `AADHAAR_VAULT_AES_KEY`; if missing/invalid it mints a secure
      `urlsafe_b64encode(os.urandom(32))` Fernet key into runtime env (with a warning).
- [x] **High-fidelity UiPath mocking** — node attempts the live token+queue HTTP
      flow; on placeholder creds **or** network failure it sleeps **1.5s** to simulate
      the API window and returns `ui_path_job_id = tx_uipath_mock_99824`.
- [x] **Streaming integration** — every node pushes trace frames to
      `event_bus.publish_event()` for the live "Glass Box" reasoning panel.

---

## Phase 6 — PRD re-check & correctness fixes

> Found via direct numeric testing of the matching engine against all 5 citizens.

- [x] **BUG (critical):** stripping `Kumar`/`Devi`/`Prasad`/etc. as "honorifics"
      collapsed `Ramesh Kumar` → `RAMES`, making the PRD's flagship mismatch score
      0.925 and **wrongly pass**. → Restricted `_HONORIFIC_RE` to *true* salutations
      only (Smt, Shri, Sri, Dr, …); name-component tokens are kept.
- [x] **BUG (critical):** full-string Jaro-Winkler is prefix-biased and still scored
      `Ramesh Kumar` vs `Ramesha K` at 0.908 (passes), unable to detect the
      `Kumar → K` truncation. → Added **`compute_name_match_score`**: conservative
      minimum of full-string JW and the worst aligned-token JW. Hero case now
      scores **0.76 → flagged**; legit variants still pass.
- [x] **BUG:** sandbox mock OCR returned **Ramesh's names for every citizen**, so
      all document scenarios looked like a mismatch. → Made `_call_sarvam_vision`
      **citizen-aware**, pulling each citizen's name variants from the registry.
- [x] **BUG:** `routes/stream.py` referenced `event_bus.get_case_queue()` which did
      not exist. → Added the accessor to `event_bus.py`.

### Verified matching scores (post-fix)

| Citizen | Names (Aadhaar → Ration) | Composite | Outcome |
|---|---|---|---|
| CITIZEN-001 | Ramesh Kumar → Ramesha K | 0.7600 | **MISSING_DOCUMENTS** ✓ |
| CITIZEN-002 | Lakshmi Devi → Laxmi Devi | 0.8578 | COMPLETE ✓ |
| CITIZEN-003 | Suresh Prasad → Suresh Prasad | 1.0000 | COMPLETE ✓ |
| CITIZEN-004 | Priya Sharma → Priya Sharmaa | 1.0000 | COMPLETE ✓ |
| CITIZEN-005 | Mohammed Rashid → Mohd Rashid | 0.7958 | **MISSING_DOCUMENTS** ✓ |

---

## Phase 7 — Testing & docs

- [x] **`e2e_test.py`** — async driver: 6 happy/exception scenarios + 2 negative
      guard-rail tests (403 missing OTP, 422 empty consent). Attaches to SSE,
      prints frames, asserts terminal status, exits non-zero on any failure.
- [x] **`TESTING.md`** — setup, run instructions, scenario matrix, `curl`
      walkthrough, unit checks, troubleshooting.
- [x] **`TASKLIST.md`** — this document.

---

## Phase 8 — Production gap closure (SLA, persistence, DPDP revocation, async callback)

- [x] **14-day SLA watchdog** — new `sla_watchdog.py`: async background loop +
      `run_sla_watchdog_once(sla_days_override)`. Scans `PENDING_UIPATH`/`UNDER_REVIEW`
      cases past the SLA, sets `ESCALATED`, mocks an SMS/WhatsApp appeal payload,
      and emits an escalation frame to `event_bus`. Wired into startup/shutdown;
      on-demand trigger via `POST /api/admin/sla/run`.
- [x] **Terminal state persistence** — `_persist_terminal_state` in `agent_graph.py`
      writes `status`, `uipath_job_id`, `eligible_schemes`, `anomaly_summary` back to
      `HouseholdCase` from both `uipath_execution_node` and `exception_management_node`.
      `/status` now reads live DB data instead of a queue-size guess.
- [x] **Lifecycle correction** — a queued UiPath item is no longer "COMPLETE"; it is
      `PENDING_UIPATH` until the async robot reports back. Callback flips it to
      `COMPLETE` / `SUBMISSION_FAILED`. (e2e expectations updated accordingly.)
- [x] **Consent revocation** — `POST /api/cases/{case_id}/consent/revoke`: atomic
      deactivate of active `ConsentLog` rows (`is_active=False`, `revoked_at` stamped —
      audit trail preserved), case → `REVOKED_BY_USER`, **hard-delete** of the linked
      `AadhaarDataVault` blob and severing of `User.aadhaar_vault_ref`.
- [x] **UiPath async callback** — `POST /api/webhooks/uipath/callback`: verifies the
      case, updates outcome, re-registers + emits a terminal Glass Box frame so the UI
      stops spinning.

### Latent bugs found & fixed during Phase 8

- [x] **Stale sessionmaker capture:** `from database import AsyncSessionLocal` froze the
      Postgres-bound factory; after the SQLite rebind every write would hit the dead
      engine. → Added `database.session_scope()` (resolves the factory at call time);
      migrated `main.py`, `agent_graph.py`, `routes/stream.py`, `sla_watchdog.py` to it.
- [x] **Import-time crash when asyncpg absent:** the module-level engine build raised
      `ModuleNotFoundError` before the connection-time fallback could run. → Guarded the
      build to degrade to SQLite on driver-absence too (verified: app boots with no
      asyncpg installed).

---

## PRD coverage scorecard

| PRD step | Status | Where |
|---|---|---|
| 1. Voice-first intent extraction | ✅ | `voice_intent_agent_node` |
| 2. Multi-document audit + fuzzy matching | ✅ | `document_audit_node` + matching engine |
| 3. DPDP consent + Aadhaar masking/vault | ✅ | `ConsentLog`, `AadhaarDataVault`, masking validator |
| 3b. DPDP right to withdraw (revocation) | ✅ | `POST /api/cases/{id}/consent/revoke` |
| 4. One-click UiPath backend submission | ✅ | `uipath_execution_node` → OData QueueItems |
| 4b. Async RPA callback handler | ✅ | `POST /api/webhooks/uipath/callback` |
| 5. Exception/"missing documents" routing | ✅ | `exception_management_node` + conditional edge |
| 5b. 14-day SLA watchdog (SMS/WhatsApp) | ✅ | `sla_watchdog.py` + `POST /api/admin/sla/run` |
| 5c. Terminal state persistence | ✅ | `_persist_terminal_state` + live `/status` |
| Glass Box real-time observability | ✅ | `event_bus` + `routes/stream.py` SSE |
| NPCI Aadhaar→bank seeding | ✅ | `npci_seeding_node` + mock NPCI map |

---

## Phase 9 — Track 2 pivot (Maestro Case → Maestro BPMN)

> The submission target moved to **Track 2: UiPath Maestro BPMN**. The backend is
> track-agnostic and was retained wholesale; the work was modelling + reframing, not a
> rewrite. The LangGraph topology already maps 1:1 to a BPMN 2.0 process.

- [x] **`welfareflow.bpmn`** — standards-compliant **BPMN 2.0** model of the end-to-end
      welfare-enrolment process, importable into the Maestro canvas (bpmn.uipath.com).
      Contains: message start event, 3 agent tasks, 1 service task, 2 send tasks,
      2 user tasks, 3 exclusive gateways, a **name-mismatch error boundary event**,
      4 end events, and a **non-interrupting 14-day SLA timer event sub-process**.
      Includes full BPMN-DI layout. Validated well-formed.
- [x] **`BPMN_PROCESS.md`** — element→implementation map (every BPMN task → its
      `agent_graph.py` node), process-variable table, and the import-to-Maestro +
      task-binding guide (agent / service / user task wiring + backend HTTP bindings).
- [x] **`README.md`** — reframed to Track 2; architecture section now leads with the
      BPMN process and shows the BPMN-task → backend-node mapping; "Built With" leads
      with Maestro BPMN.
- [x] **`uipath_maestro.py`** — reframed as the BPMN-process integration layer; Tier 1
      documented as the **Maestro BPMN process trigger** (primary path); added public
      `start_process_instance(process_variables=…)` alias.
- [x] **`agent_graph.py`** — header rewritten as "executable backend for the Maestro BPMN
      process" with the BPMN-task→node map; `uipath_execution_node` now calls
      `start_process_instance` and labels its payload as **BPMN process variables**.
- [x] **Import smoke test** — `agent_graph`, `uipath_maestro`, `main` all import clean
      post-pivot; `start_process_instance` resolves.

### Track 2 BPMN — element inventory

| BPMN element | Count | Bound to |
|---|---|---|
| Message start event | 1 | `POST /api/cases/initialize` |
| Agent tasks | 3 | voice_intent, eligibility_router, document_audit |
| Service task | 1 | npci_seeding |
| Send tasks | 2 | uipath_execution, SLA escalate |
| User tasks | 2 | exception_management, await_approval |
| Exclusive gateways | 3 | route_after_document_audit, route_after_npci, approval decision |
| Error boundary event | 1 | document-audit name mismatch |
| Timer event sub-process | 1 | sla_watchdog (P14D) |
| End events | 4 | submitted / missing-docs / rejected / escalated |

---

## Open items / next milestones

1. **Sarvam TTS (Bulbul):** synthesize the spoken response in the citizen's dialect.
2. **Live Sarvam Saaras WebSocket** streaming ASR (currently transcript-in).
3. **Affidavit generator:** render the Name Mismatch Affidavit PDF in the
   exception path.
4. **Real messaging gateway** for the SLA watchdog (currently a logged mock payload).
5. **Webhook auth:** HMAC-sign the UiPath callback to reject spoofed outcomes.
