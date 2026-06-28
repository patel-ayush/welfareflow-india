"""
WelfareFlow India — LangSmith observability bootstrap.

WHY THIS MODULE EXISTS (and why it must be imported FIRST)
----------------------------------------------------------
LangChain / LangGraph decide whether tracing is on by reading the
`LANGCHAIN_TRACING_V2` (and newer `LANGSMITH_TRACING`) environment variables the
*first* time their global tracer is initialised — which happens the moment
`langchain_*` is imported and a client like `ChatGoogleGenerativeAI(...)` is
constructed.

Previously these vars were set inside the FastAPI startup hook, but `agent_graph`
(which builds the Gemini client and compiles the graph at import time) was
imported *before* that hook ran. So LangChain cached "tracing = off" and **no
traces ever reached LangSmith.**

The fix: this module sets every relevant env var at IMPORT time, and is imported
as the very first line of `agent_graph.py` and `main.py` — before any langchain
import. Importing it has no heavy dependencies (no langchain import here), so it
is safe to put first.

WHAT GETS TRACED
----------------
Once tracing is enabled, the following all appear as a single nested trace tree
in the LangSmith project dashboard:
  - the whole LangGraph run (root span, named per case, tagged + metadata-rich)
  - every @traceable agent node (run_type="chain")
  - every @traceable tool call — Sarvam STT/OCR/TTS, UiPath submission, the
    Jaro-Winkler name match, the NPCI lookup (run_type="tool")
  - every Gemini LLM call (auto-instrumented by the langchain integration)

See OBSERVABILITY.md for the full decorator/parameter reference.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from config import get_settings

logger: logging.Logger = logging.getLogger("welfareflow.observability")

_settings = get_settings()

# Public constants reused by the graph for consistent tagging.
LANGSMITH_PROJECT: str = _settings.langsmith_project
BASE_TAGS: list[str] = ["welfareflow-india", "uipath-agenthack", _settings.app_env]


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def configure_tracing() -> dict[str, Any]:
    """
    Idempotently push LangSmith configuration into the process environment.

    Sets BOTH the legacy `LANGCHAIN_*` names and the modern `LANGSMITH_*` names
    so any langchain/langsmith version picks them up. Tracing is only switched on
    when an API key is actually present — otherwise we force it OFF so the SDK
    does not spend every request trying (and silently failing) to upload spans.

    Returns a small status dict (also logged) describing the resolved config.
    """
    tracing_requested: bool = _truthy(_settings.langsmith_tracing)
    has_key: bool = bool(_settings.langsmith_api_key.strip())
    enabled: bool = tracing_requested and has_key

    flag: str = "true" if enabled else "false"

    # Tracing on/off
    os.environ["LANGCHAIN_TRACING_V2"] = flag
    os.environ["LANGSMITH_TRACING"] = flag

    # Endpoint
    os.environ["LANGCHAIN_ENDPOINT"] = _settings.langsmith_endpoint
    os.environ["LANGSMITH_ENDPOINT"] = _settings.langsmith_endpoint

    # API key (only export when present; never export an empty string)
    if has_key:
        os.environ["LANGCHAIN_API_KEY"] = _settings.langsmith_api_key
        os.environ["LANGSMITH_API_KEY"] = _settings.langsmith_api_key

    # Project
    os.environ["LANGCHAIN_PROJECT"] = _settings.langsmith_project
    os.environ["LANGSMITH_PROJECT"] = _settings.langsmith_project

    status: dict[str, Any] = {
        "enabled": enabled,
        "tracing_requested": tracing_requested,
        "api_key_present": has_key,
        "project": _settings.langsmith_project,
        "endpoint": _settings.langsmith_endpoint,
    }

    if enabled:
        logger.info(
            "LangSmith tracing ENABLED at import — project=%r endpoint=%r",
            _settings.langsmith_project,
            _settings.langsmith_endpoint,
        )
    elif tracing_requested and not has_key:
        logger.warning(
            "LangSmith tracing was requested (LANGSMITH_TRACING=true) but no "
            "LANGSMITH_API_KEY is set — tracing is DISABLED. Add the key to .env "
            "to see traces at %s",
            _settings.langsmith_endpoint,
        )
    else:
        logger.info("LangSmith tracing disabled (LANGSMITH_TRACING is not true).")

    return status


def verify_langsmith_connection() -> dict[str, Any]:
    """
    Actively confirm we can reach LangSmith with the configured key, and ensure
    the target project exists. Safe to call at startup or from a health endpoint;
    never raises — returns a structured result describing what happened.

    This is the thing to call when someone says "I can't see any traces": it
    tells you immediately whether the key works and which project to look in.
    """
    result: dict[str, Any] = {
        "enabled": _truthy(os.environ.get("LANGCHAIN_TRACING_V2", "false")),
        "project": _settings.langsmith_project,
        "endpoint": _settings.langsmith_endpoint,
        "reachable": False,
        "project_ready": False,
        "detail": "",
        "dashboard_hint": (
            "Open https://smith.langchain.com → select project "
            f"{_settings.langsmith_project!r} (top-left project picker)."
        ),
    }

    if not _settings.langsmith_api_key.strip():
        result["detail"] = "No LANGSMITH_API_KEY configured — tracing cannot upload."
        logger.warning("verify_langsmith_connection: %s", result["detail"])
        return result

    try:
        # Imported lazily so this module stays free of heavy deps at import time.
        from langsmith import Client

        client = Client(
            api_url=_settings.langsmith_endpoint,
            api_key=_settings.langsmith_api_key,
        )
        # A cheap authenticated round-trip.
        _ = list(client.list_projects(limit=1))
        result["reachable"] = True

        if not client.has_project(_settings.langsmith_project):
            client.create_project(project_name=_settings.langsmith_project)
            logger.info(
                "verify_langsmith_connection: created LangSmith project %r",
                _settings.langsmith_project,
            )
        result["project_ready"] = True
        result["detail"] = "LangSmith reachable and project ready."
        logger.info(
            "verify_langsmith_connection: OK — traces will appear in project %r at %s",
            _settings.langsmith_project,
            _settings.langsmith_endpoint,
        )
    except Exception as exc:  # noqa: BLE001 — diagnostics must never crash startup
        result["detail"] = f"{type(exc).__name__}: {exc}"
        logger.error(
            "verify_langsmith_connection: FAILED to reach LangSmith — %s. "
            "Check LANGSMITH_API_KEY validity and network access.",
            result["detail"],
        )

    return result


# Configure as a side effect of import — this is the whole point of the module.
TRACING_STATUS: dict[str, Any] = configure_tracing()
