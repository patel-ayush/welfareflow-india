"""
Async SQLAlchemy ORM models — WelfareFlow India database layer.

DPDP Act 2023 / Aadhaar Data Vault design:
  - `AadhaarDataVault` is an isolated table.  The `encrypted_aadhaar_blob` column
    stores Fernet (AES-128-CBC + HMAC-SHA256) ciphertext.  No other table ever
    stores a plaintext or masked Aadhaar number.
  - All cross-table references to Aadhaar use the `vault_reference_key` (UUIDv4).
  - `ConsentLog` is insert-only; no UPDATE / DELETE is permitted by convention so
    the audit trail is immutable.

Encryption helpers:
  - `encrypt_aadhaar(plaintext)` → bytes
  - `decrypt_aadhaar(ciphertext)` → str
  Both are exposed at module level for use from agent nodes only.
"""
from __future__ import annotations

import base64
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from config import get_settings
from database import Base

logger: logging.Logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Self-generating cryptographic vault key.
# The key must be a 32-byte URL-safe base64-encoded value (standard Fernet format).
# If AADHAAR_VAULT_AES_KEY is missing or invalid, we mint a cryptographically
# secure key on-the-fly with os.urandom(32) and hold it in runtime memory so the
# vault works out-of-the-box.  In production a persistent key MUST be supplied,
# otherwise vault ciphertext becomes undecryptable across process restarts.
# ---------------------------------------------------------------------------

def _resolve_vault_key() -> bytes:
    """
    Return a valid Fernet key from settings.
    In production, raises RuntimeError if the key is missing or invalid so the
    process fails fast rather than silently losing encrypted data on restart.
    In non-production envs, generates an ephemeral key with a loud warning.
    """
    candidate: str = settings.aadhaar_vault_aes_key.strip()
    try:
        Fernet(candidate.encode())
        return candidate.encode()
    except (ValueError, TypeError):
        if settings.app_env == "production":
            raise RuntimeError(
                "AADHAAR_VAULT_AES_KEY is missing or invalid in production. "
                "Generate a key with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        generated_key: str = base64.urlsafe_b64encode(os.urandom(32)).decode()
        logger.warning(
            "AadhaarDataVault: AADHAAR_VAULT_AES_KEY missing/invalid — generated an "
            "ephemeral in-memory Fernet key. All vault blobs are lost on restart. "
            "Configure a persistent key for any non-development environment."
        )
        return generated_key.encode()


_fernet: Fernet = Fernet(_resolve_vault_key())


def encrypt_aadhaar(plaintext_aadhaar: str) -> bytes:
    """Encrypt a 12-digit Aadhaar number to Fernet ciphertext bytes."""
    return _fernet.encrypt(plaintext_aadhaar.encode("utf-8"))


def decrypt_aadhaar(ciphertext: bytes) -> str:
    """Decrypt Fernet ciphertext back to the plaintext Aadhaar string.
    Raises InvalidToken if the key is wrong or data is tampered with."""
    try:
        return _fernet.decrypt(ciphertext).decode("utf-8")
    except InvalidToken as exc:
        logger.error("AadhaarDataVault: decryption failed — possible key mismatch or data corruption")
        raise exc


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# User — core citizen identity record (no Aadhaar stored here)
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    citizen_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(256), nullable=False)
    phone: Mapped[str] = mapped_column(String(15), nullable=False)
    state: Mapped[str] = mapped_column(String(64), nullable=False)
    district: Mapped[str] = mapped_column(String(64), nullable=False)

    # UUIDv4 reference key that points to the AadhaarDataVault — never the raw number
    aadhaar_vault_ref: Mapped[Optional[str]] = mapped_column(
        String(36), nullable=True, index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    # One-to-many: a citizen may have multiple welfare cases over time
    cases: Mapped[list["HouseholdCase"]] = relationship(
        "HouseholdCase", back_populates="user", lazy="select"
    )
    consent_logs: Mapped[list["ConsentLog"]] = relationship(
        "ConsentLog", back_populates="user", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<User citizen_id={self.citizen_id} name={self.full_name!r}>"


# ---------------------------------------------------------------------------
# HouseholdCase — a single welfare application workflow run
# ---------------------------------------------------------------------------
class HouseholdCase(Base):
    __tablename__ = "household_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True, default=lambda: str(uuid.uuid4())
    )
    citizen_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.citizen_id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Current pipeline status
    status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="INITIALISED", index=True
    )
    # e.g. "voice_intent_agent", "document_audit_node", "COMPLETE", "MISSING_DOCUMENTS"
    current_agent: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # Extracted profile fields (denormalised for quick read)
    raw_transcript: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    language_code: Mapped[str] = mapped_column(String(16), nullable=False, default="hi-IN")
    land_area_acres: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    annual_income_inr: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    age: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    occupation: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # Scheme results (JSON-serialisable text)
    eligible_schemes: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="JSON array of scheme codes"
    )
    anomaly_summary: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True, comment="JSON array of anomaly strings"
    )

    # Capability token returned to the citizen at creation. Sensitive actions
    # (consent revocation) require presenting it, so knowing only the case_id is
    # not enough to revoke/erase another citizen's data.
    tracking_token: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, index=True)

    # UiPath downstream tracking
    uipath_job_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    uipath_queue_item_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    user: Mapped["User"] = relationship("User", back_populates="cases")
    consent_logs: Mapped[list["ConsentLog"]] = relationship(
        "ConsentLog", back_populates="case", lazy="select"
    )

    def __repr__(self) -> str:
        return f"<HouseholdCase case_id={self.case_id} status={self.status!r}>"


# ---------------------------------------------------------------------------
# ConsentLog — immutable DPDP Act 2023 consent audit trail
# INSERT-ONLY: application logic must never UPDATE or DELETE rows here.
# ---------------------------------------------------------------------------
class ConsentLog(Base):
    __tablename__ = "consent_logs"
    __table_args__ = (
        UniqueConstraint("case_id", "item_code", name="uq_consent_case_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    log_id: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
    )
    case_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("household_cases.case_id", ondelete="RESTRICT"), nullable=False, index=True
    )
    citizen_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.citizen_id", ondelete="RESTRICT"), nullable=False, index=True
    )

    # Each consented item is a separate row for fine-grained audit granularity
    item_code: Mapped[str] = mapped_column(String(64), nullable=False)
    description_en: Mapped[str] = mapped_column(Text, nullable=False)
    description_hi: Mapped[str] = mapped_column(Text, nullable=False)

    otp_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    consented_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    # DPDP Act 2023 — right to withdraw consent. The original grant row is never
    # deleted (immutable audit trail); revocation flips is_active and stamps
    # revoked_at so the full consent lifecycle remains queryable.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    case: Mapped["HouseholdCase"] = relationship("HouseholdCase", back_populates="consent_logs")
    user: Mapped["User"] = relationship("User", back_populates="consent_logs")

    def __repr__(self) -> str:
        return f"<ConsentLog case_id={self.case_id} item={self.item_code!r}>"


# ---------------------------------------------------------------------------
# AadhaarDataVault — isolated encrypted identity store
# This table lives logically separate from all business tables.
# The `vault_reference_key` (UUIDv4) is the ONLY pointer used externally.
# The `encrypted_aadhaar_blob` contains Fernet ciphertext of the 12-digit number.
# ---------------------------------------------------------------------------
class AadhaarDataVault(Base):
    __tablename__ = "aadhaar_data_vault"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vault_reference_key: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True,
        default=lambda: str(uuid.uuid4()),
        comment="Non-reversible UUIDv4 reference key — the only ID shared with other tables",
    )
    # AES-128-CBC (Fernet) ciphertext of the 12-digit Aadhaar number
    encrypted_aadhaar_blob: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False,
        comment="Fernet(AES-128-CBC + HMAC-SHA256) ciphertext of the raw Aadhaar number",
    )
    # Stores only the last 4 digits unencrypted to allow display masking without decryption
    last4_digits: Mapped[str] = mapped_column(
        String(4), nullable=False,
        comment="Last 4 digits only — used for masked display (xxxx-xxxx-1234)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    def __repr__(self) -> str:
        return f"<AadhaarDataVault ref={self.vault_reference_key} last4={self.last4_digits!r}>"
