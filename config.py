"""
Centralised settings resolved from environment variables via pydantic-settings.
All downstream modules import `get_settings()` rather than calling `os.getenv` directly.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LangSmith tracing ---
    langsmith_tracing: str = "true"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langsmith_api_key: str = ""
    langsmith_project: str = "WelfareFlow-India"

    # --- Core orchestration LLM (Gemini Flash) ---
    gemini_api_key: str = ""

    # --- Sarvam AI (STT / TTS / Vision) ---
    sarvam_api_key: str = ""
    sarvam_base_url: str = "https://api.sarvam.ai"
    sarvam_tts_model: str = "bulbul:v3"
    sarvam_tts_speaker: str = "anushka"
    # Saaras speech-to-text(-translate) model used for voice onboarding.
    sarvam_stt_model: str = "saaras:v2"
    # Set True to skip live Sarvam API calls (uses mock responses in development/test)
    sarvam_mock_mode: bool = False

    # --- Outbound HTTP resilience (Gemini / Sarvam) ---
    http_max_retries: int = 3
    http_backoff_base_seconds: float = 0.5

    # --- UiPath Orchestrator + Maestro ---
    # UIPATH_ORCHESTRATOR_URL: full URL to the UiPath cloud/staging tenant portal.
    # The org name is auto-parsed from the first path segment of this URL.
    uipath_orchestrator_url: str = ""

    # OAuth2 credentials — generated under UiPath Admin > External Applications.
    # .env uses UI_PATH_APP_ID / UIPATH_APP_SECRET; these aliases map them in.
    ui_path_app_id: str = ""        # reads UI_PATH_APP_ID
    uipath_app_secret: str = ""     # reads UIPATH_APP_SECRET

    # Legacy aliases (kept for backward compat with older .env files)
    uipath_client_id: str = ""
    uipath_client_secret: str = ""

    # Personal Access Token — simpler alternative to OAuth2 client_credentials.
    # Generate under UiPath Admin > External Applications > Personal Access Token.
    # When set, OAuth2 token acquisition is skipped entirely.
    uipath_personal_access_token: str = ""

    # Identity server URL — auto-derived from orchestrator URL at runtime.
    # Override only if your UiPath tenant uses a non-standard identity endpoint.
    uipath_identity_url: str = ""

    # Orchestrator / Maestro specifics
    uipath_tenant_name: str = ""    # logical tenant name (e.g. "Default"), NOT the UUID
    uipath_folder_path: str = "Shared"
    uipath_queue_name: str = "WelfareFlow_Submissions"
    uipath_process_name: str = "WelfareSchemeSubmission"  # Maestro process slug
    uipath_webhook_secret: str = ""

    # --- PostgreSQL (asyncpg) ---
    # Leave blank to auto-fallback to in-memory SQLite (development only)
    database_url: str = ""

    # --- Aadhaar Data Vault encryption key (32-byte Fernet base64 key) ---
    aadhaar_vault_aes_key: str = ""

    # --- Application tuning ---
    app_env: Literal["development", "staging", "production"] = "development"
    app_cors_origins: str = "http://localhost:3000,http://localhost:5173"
    similarity_threshold: float = 0.85

    # --- Human-in-the-loop approval gate ---
    # When True (or when a request sets require_approval=true), the pipeline pauses
    # before the irreversible UiPath submission and waits for an explicit human
    # approve/reject decision (humans accountable for high-impact actions).
    hitl_approval_enabled: bool = False
    # --- Agent decision reasoning (LLM-generated plain-language explanations) ---
    llm_reasoning_enabled: bool = True

    # --- Admin API key — protects /api/admin/* endpoints ---
    # Leave blank to allow unauthenticated access (development only).
    # In production, generate with: openssl rand -hex 32
    admin_api_key: str = ""

    # --- SLA watchdog (Right to Service) ---
    sla_days_threshold: int = 14
    sla_watchdog_interval_seconds: int = 3600
    sla_watchdog_enabled: bool = True

    @field_validator("app_cors_origins")
    @classmethod
    def no_wildcard_with_credentials(cls, v: str) -> str:
        origins = [o.strip() for o in v.split(",") if o.strip()]
        if "*" in origins:
            raise ValueError(
                "Wildcard '*' origin cannot be used with allow_credentials=True. "
                "Set APP_CORS_ORIGINS to explicit comma-separated origins."
            )
        return v


@lru_cache()
def get_settings() -> Settings:
    return Settings()
