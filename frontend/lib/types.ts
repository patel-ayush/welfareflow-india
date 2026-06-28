// ─────────────────────────────────────────────────────────────────────────────
// Types aligned 1:1 with the FastAPI backend contract (schemas.py).
// Previously these drifted from the backend and broke the UI; keep them in sync.
// ─────────────────────────────────────────────────────────────────────────────

export type AgentNodeName =
  | "voice_intent_agent"
  | "eligibility_router"
  | "document_audit"
  | "npci_seeding"
  | "exception_management"
  | "uipath_execution"
  | "uipath_callback"
  | "consent_revocation"
  | "sla_watchdog"
  | "pipeline_runner"
  | "system";

export type CaseStatus =
  | "INITIALISED"
  | "PROFILE_EXTRACTED"
  | "ELIGIBILITY_CHECKED"
  | "DOCUMENTS_AUDITED"
  | "MISSING_DOCUMENTS"
  | "AWAITING_APPROVAL"
  | "PENDING_UIPATH"
  | "SUBMITTED_TO_UIPATH"
  | "COMPLETE"
  | "SUBMISSION_FAILED"
  | "ESCALATED"
  | "REVOKED_BY_USER"
  | "UNDER_REVIEW"
  | "FAILED";

// ─── SSE frame — EXACTLY what the backend emits (schemas.py:AgentStreamFrame) ──

export type AgentEventType =
  | "agent_start"
  | "agent_log"
  | "agent_result"
  | "agent_complete"
  | "anomaly_detected"
  | "error"
  | "stream_end";

export interface AgentStreamFrame {
  event_id: string;
  case_id: string;
  event_type: AgentEventType;
  agent_name: string;
  timestamp: string;
  data: Record<string, unknown>;
  status: string;
}

// ─── Affidavit — matches utils/affidavit.generate_mismatch_affidavit_metadata ──

export interface AffidavitMismatch {
  source_doc_type: string;
  source_doc_label_en: string;
  source_doc_label_kn: string;
  source_name: string;
  target_doc_type: string;
  target_doc_label_en: string;
  target_doc_label_kn: string;
  target_name: string;
  raw_anomaly: string;
}

export interface AffidavitMetadata {
  case_id: string;
  document_title_en: string;
  document_title_kn: string;
  has_mismatch: boolean;
  declarant_aadhaar_name: string;
  declarant_ration_name: string;
  declarant_other_doc_type?: string;
  declarant_other_doc_label_en?: string;
  declarant_other_doc_label_kn?: string;
  mismatches: AffidavitMismatch[];
  stamp_paper_value_inr: number;
  jurisdiction: string;
  notary_sworn_text_en: string;
  notary_sworn_text_kn: string;
  affidavit_body_en: string;
  affidavit_body_kn: string;
  generated_at: string;
}

// Impact dashboard — matches GET /api/impact response
export interface ImpactSummary {
  total_cases: number;
  applications_submitted: number;
  mismatches_caught_before_rejection: number;
  pmkisan_income_unlocked_inr: number;
  health_cover_unlocked_inr: number;
  status_breakdown: Record<string, number>;
}

// HITL decision — matches POST /api/cases/{id}/decision
export interface CaseDecisionResponse {
  case_id: string;
  approved: boolean;
  resumed: boolean;
  new_status: string;
  message: string;
}

// ─── Case lifecycle payloads ──────────────────────────────────────────────────

export interface DocumentUpload {
  document_type: "aadhaar" | "ration_card" | "bank_passbook" | "land_record";
  image_base64: string;
  filename: string;
}

export interface CustomCitizenData {
  name_aadhaar: string;
  name_ration_card: string;
  name_passbook: string;
  state: string;
  district: string;
  annual_income_inr: number;
  land_area_acres: number;
  age: number;
  aadhaar_last4: string;
  phone: string;
}

export interface CaseInitPayload {
  citizen_id?: string;
  custom_citizen?: CustomCitizenData;
  consent_given: boolean;
  audio_base64?: string;
  raw_transcript?: string;
  language_code?: string;
  documents?: DocumentUpload[];
  require_approval?: boolean;
}

export interface CaseInitResponse {
  case_id: string;
  tracking_token: string;
  stream_url: string;
  consent_logged: boolean;
  message: string;
  created_at: string;
}

// Matches CaseStatusResponse in schemas.py
export interface CaseStatusResponse {
  case_id: string;
  status: CaseStatus;
  current_agent: string;
  schemes_eligible: string[];
  anomalies: string[];
  uipath_job_id?: string | null;
  last_updated: string;
}

// Matches ConsentRevocationResponse in schemas.py
export interface ConsentRevocationResponse {
  case_id: string;
  case_status: string;
  consent_items_revoked: number;
  vault_purged: boolean;
  vault_reference_key?: string | null;
  revoked_at: string;
  message: string;
}

// Matches SlaWatchdogRunResult in schemas.py
export interface SlaEscalationRecord {
  case_id: string;
  citizen_id: string;
  previous_status: string;
  new_status: string;
  days_stuck: number;
  notification_channel: string;
  notification_payload: Record<string, unknown>;
}

export interface SlaWatchdogRunResult {
  scanned_at: string;
  sla_days_threshold: number;
  cases_scanned: number;
  cases_escalated: number;
  escalations: SlaEscalationRecord[];
}
