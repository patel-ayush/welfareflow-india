import type {
  CaseInitPayload,
  CaseInitResponse,
  CaseStatusResponse,
  CaseDecisionResponse,
  ConsentRevocationResponse,
  ImpactSummary,
  SlaWatchdogRunResult,
} from "./types";

const BASE = typeof window !== "undefined"
  ? (process.env.NEXT_PUBLIC_API_BASE_URL ?? `${window.location.protocol}//${window.location.hostname}:8000/api`)
  : (process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api");

async function post<T>(path: string, body: unknown, extraHeaders?: Record<string, string>): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...extraHeaders },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`POST ${path} → ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`);
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`GET ${path} → ${res.status}: ${text}`);
  }
  return res.json() as Promise<T>;
}

// ─── Case lifecycle ──────────────────────────────────────────────────────────

const DEFAULT_CONSENT_ITEMS = [
  {
    item_code: "IDENTITY_SHARE",
    description_en: "Share identity data with the welfare department for scheme eligibility verification",
    description_hi: "योजना पात्रता सत्यापन के लिए पहचान डेटा कल्याण विभाग के साथ साझा करें",
  },
  {
    item_code: "SCHEME_ELIGIBILITY_CHECK",
    description_en: "Automated eligibility check across government welfare schemes (PM-KISAN, Ayushman Bharat, etc.)",
    description_hi: "सरकारी कल्याण योजनाओं में स्वचालित पात्रता जांच",
  },
  {
    item_code: "UIPATH_PROCESSING",
    description_en: "Automated processing and submission via UiPath Maestro robotic workflow",
    description_hi: "UiPath Maestro रोबोटिक वर्कफ़्लो के माध्यम से स्वचालित प्रसंस्करण और सबमिशन",
  },
];

export function initializeCase(payload: CaseInitPayload): Promise<CaseInitResponse> {
  return post<CaseInitResponse>("/cases/initialize", {
    // One of citizen_id or custom_citizen must be provided; backend validates.
    ...(payload.citizen_id    ? { citizen_id: payload.citizen_id }         : {}),
    ...(payload.custom_citizen ? { custom_citizen: payload.custom_citizen } : {}),
    raw_transcript: payload.raw_transcript ?? "",
    consent_items: DEFAULT_CONSENT_ITEMS,
    otp_verified: payload.consent_given,
    language_code: payload.language_code ?? "kn-IN",
    documents: payload.documents ?? [],
    ...(payload.audio_base64 ? { audio_base64: payload.audio_base64 } : {}),
    ...(payload.require_approval !== undefined ? { require_approval: payload.require_approval } : {}),
  });
}

export function getCaseStatus(caseId: string): Promise<CaseStatusResponse> {
  return get<CaseStatusResponse>(`/cases/${caseId}/status`);
}

export function revokeConsent(caseId: string, trackingToken?: string): Promise<ConsentRevocationResponse> {
  const headers = trackingToken ? { "X-Tracking-Token": trackingToken } : undefined;
  return post<ConsentRevocationResponse>(`/cases/${caseId}/consent/revoke`, {}, headers);
}

export function submitDecision(
  caseId: string,
  approve: boolean,
  trackingToken: string,
): Promise<CaseDecisionResponse> {
  return post<CaseDecisionResponse>(
    `/cases/${caseId}/decision`,
    { approve },
    { "X-Tracking-Token": trackingToken },
  );
}

// ─── Admin ───────────────────────────────────────────────────────────────────

export function triggerSlaWatchdog(slaDaysOverride?: number): Promise<SlaWatchdogRunResult> {
  const body = slaDaysOverride !== undefined ? { sla_days_override: slaDaysOverride } : {};
  return post<SlaWatchdogRunResult>("/admin/sla/run", body);
}

export function getImpact(): Promise<ImpactSummary> {
  return get<ImpactSummary>("/impact");
}

// ─── SSE stream URL helper ────────────────────────────────────────────────────
// Returns the raw URL string; callers own the EventSource lifecycle.

export function getCaseStreamUrl(caseId: string): string {
  return `${BASE}/cases/${caseId}/stream`;
}
