# LangSmith Observability — WelfareFlow India

Full, transparent tracing of every agent step. This document explains **why
traces were missing**, **how it is now wired**, and **every decorator + parameter**
in use so you can extend it.

---

## TL;DR — "I can't see any traces"

1. Make sure `.env` has a valid key and tracing on:
   ```
   LANGSMITH_TRACING="true"
   LANGSMITH_API_KEY="lsv2_..."
   LANGSMITH_PROJECT="WelfareFlow-India"
   ```
2. Start the API and hit the probe:
   ```
   curl http://localhost:8000/api/admin/langsmith/health
   ```
   `reachable: true, project_ready: true` ⇒ traces will appear.
3. Open <https://smith.langchain.com> → pick project **WelfareFlow-India**.

---

## The bug that hid all traces (now fixed)

LangChain decides whether tracing is on the **first time** its tracer initialises
— which happens the moment `langchain_*` is imported and a client like
`ChatGoogleGenerativeAI(...)` is constructed.

Previously the `LANGCHAIN_TRACING_V2` env var was set inside the FastAPI startup
hook, but `agent_graph` (which builds the Gemini client + compiles the graph at
**import time**) was imported *before* that hook ran. LangChain cached
"tracing = off" → **nothing was ever sent.**

**Fix:** [`observability.py`](observability.py) sets every tracing env var at
import time and is imported as the **first line** of both
[`agent_graph.py`](agent_graph.py) and [`main.py`](main.py), before any langchain
import. It sets both the legacy `LANGCHAIN_*` and modern `LANGSMITH_*` names, and
only enables tracing when an API key is actually present (otherwise it forces it
**off** so the SDK doesn't burn every request on failed uploads).

---

## What the trace tree looks like

One nested trace per case (verified live against LangSmith):

```
welfare_case_<id>                         (root — named, tagged, metadata-rich)
├─ voice_intent                           [chain]   LangGraph node
│  └─ voice_intent_agent                  [chain]   @traceable node
│     ├─ sarvam_saaras_stt                [tool]    (only when audio sent)
│     └─ ChatGoogleGenerativeAI           [llm]     auto-instrumented Gemini call
├─ eligibility_router_agent               [chain]
│  └─ sarvam_bulbul_tts                   [tool]    spoken dialect reply
├─ document_audit_agent                   [chain]
│  ├─ sarvam_vision_ocr   ×N              [tool]    one per uploaded document
│  └─ sarvam_bulbul_tts                   [tool]
├─ npci_seeding_agent                     [chain]
├─ exception_management_agent             [chain]   (mismatch path)  OR
└─ uipath_execution_agent                 [chain]   (happy path)
   └─ uipath_maestro_submit               [tool]
```

---

## Every decorator & parameter in use

### 1. `@traceable` — agent nodes (`agent_graph.py`)

```python
@traceable(run_type="chain", name="voice_intent_agent")
async def voice_intent_agent_node(state): ...
```

| Node function                  | `run_type` | `name`                        |
|--------------------------------|-----------|-------------------------------|
| `voice_intent_agent_node`      | `chain`   | `voice_intent_agent`          |
| `eligibility_router_node`      | `chain`   | `eligibility_router_agent`    |
| `document_audit_node`          | `chain`   | `document_audit_agent`        |
| `npci_seeding_node`            | `chain`   | `npci_seeding_agent`          |
| `exception_management_node`    | `chain`   | `exception_management_agent`  |
| `uipath_execution_node`        | `chain`   | `uipath_execution_agent`      |

### 2. `@traceable` — tool calls (the external I/O boundaries)

These were **added** so the trace is fully transparent — previously the Sarvam
and UiPath calls were invisible plain `httpx` calls.

| Function                          | File               | `run_type` | `name`                 |
|-----------------------------------|--------------------|-----------|------------------------|
| `transcribe_audio_saaras`         | `agent_graph.py`   | `tool`    | `sarvam_saaras_stt`    |
| `_call_sarvam_vision`             | `agent_graph.py`   | `tool`    | `sarvam_vision_ocr`    |
| `synthesize_agent_response_dialect` | `agent_graph.py` | `tool`    | `sarvam_bulbul_tts`    |
| `submit_to_maestro`               | `uipath_maestro.py`| `tool`    | `uipath_maestro_submit`|

**`@traceable` parameters reference:**
- `run_type` — span category: `"chain"` (orchestration step), `"tool"` (external
  call), `"llm"`, `"retriever"`, `"prompt"`, `"parser"`. Drives the icon/grouping
  in the dashboard.
- `name` — display name of the span (defaults to the function name).
- `tags` — `list[str]`, optional per-span labels for filtering.
- `metadata` — `dict`, optional structured fields attached to the span.
- `project_name` — override the destination project for that span.
- Inputs/outputs are captured **automatically** from the function args and return
  value (no manual logging needed).

### 3. Root-run enrichment (`main.py`)

The whole graph run is named and labelled by passing a config to `ainvoke`:

```python
run_config = {
    "run_name": f"welfare_case_{case_id[:8]}",
    "tags": [*observability.BASE_TAGS, f"citizen:{citizen_id}"],
    "metadata": {
        "case_id": ..., "citizen_id": ..., "language_code": ...,
        "channel": "voice" | "text", "document_count": ...,
    },
}
await compiled_welfare_graph.ainvoke(initial_state, config=run_config)
```

This config **propagates to every child span**, so you can filter the whole
project by `citizen:CITIZEN-001`, by `channel`, etc.

### 4. Auto-instrumented LLM spans

`ChatGoogleGenerativeAI` (Gemini) needs **no decorator** — when tracing is on, the
langchain integration emits an `llm` span (prompts, tokens, latency) automatically
under whatever node called it.

---

## Environment variables (`observability.configure_tracing`)

Set at import from `config.Settings`. Both naming schemes are written so any
langchain/langsmith version is satisfied:

| Set by us                              | From setting            |
|----------------------------------------|-------------------------|
| `LANGCHAIN_TRACING_V2` / `LANGSMITH_TRACING` | `langsmith_tracing` (+ key present) |
| `LANGCHAIN_ENDPOINT` / `LANGSMITH_ENDPOINT`  | `langsmith_endpoint`    |
| `LANGCHAIN_API_KEY` / `LANGSMITH_API_KEY`    | `langsmith_api_key`     |
| `LANGCHAIN_PROJECT` / `LANGSMITH_PROJECT`    | `langsmith_project`     |

---

## Helpers

- `observability.configure_tracing()` — idempotent; runs once on import. Returns a
  status dict (`observability.TRACING_STATUS`).
- `observability.verify_langsmith_connection()` — authenticated round-trip; ensures
  the project exists; never raises. Used at startup and by the health endpoint.
- `GET /api/admin/langsmith/health` — live probe; the first thing to call when
  traces aren't showing.
