// ─────────────────────────────────────────────────────────────────────────────
// "Plain language" layer.
//
// The backend speaks in agent names, status codes and technical data. A villager
// (or the volunteer helping them) should never see "PENDING_UIPATH" or
// "npci_seeding". This module is the single place that translates the machine
// vocabulary into simple, reassuring, bilingual (English + Hindi) words.
// ─────────────────────────────────────────────────────────────────────────────

import type { AgentStreamFrame, CaseStatus } from "./types";

// The five visible steps of the journey, in order. Exception handling is folded
// into the document step so the citizen sees one clear "problem to fix" state.
export interface JourneyStep {
  key: string;
  agents: string[];
  title_en: string;
  title_hi: string;
  icon: string;
}

export const JOURNEY: JourneyStep[] = [
  {
    key: "understand",
    agents: ["voice_intent_agent"],
    title_en: "Understanding you",
    title_hi: "आपकी बात समझ रहे हैं",
    icon: "👂",
  },
  {
    key: "schemes",
    agents: ["eligibility_router"],
    title_en: "Finding your schemes",
    title_hi: "आपकी योजनाएँ ढूँढ रहे हैं",
    icon: "🔎",
  },
  {
    key: "documents",
    agents: ["document_audit", "exception_management"],
    title_en: "Checking your documents",
    title_hi: "आपके दस्तावेज़ जाँच रहे हैं",
    icon: "📄",
  },
  {
    key: "bank",
    agents: ["npci_seeding"],
    title_en: "Checking your bank link",
    title_hi: "आपका बैंक लिंक जाँच रहे हैं",
    icon: "🏦",
  },
  {
    key: "submit",
    agents: ["uipath_execution", "uipath_callback"],
    title_en: "Sending your application",
    title_hi: "आपका आवेदन भेज रहे हैं",
    icon: "📨",
  },
];

export function stepIndexForAgent(agent: string): number {
  const i = JOURNEY.findIndex((s) => s.agents.includes(agent));
  return i; // -1 if not a journey agent (system / sla / consent)
}

// Friendly, reassuring one-liners for each backend status.
export interface FriendlyStatus {
  en: string;
  hi: string;
  tone: "neutral" | "good" | "warn" | "bad";
  icon: string;
}

export const STATUS_TEXT: Record<string, FriendlyStatus> = {
  INITIALISED:         { en: "Getting started…",                         hi: "शुरू कर रहे हैं…",                         tone: "neutral", icon: "⏳" },
  PROFILE_EXTRACTED:   { en: "We understood your details",               hi: "हमने आपकी जानकारी समझ ली",                 tone: "neutral", icon: "✅" },
  ELIGIBILITY_CHECKED: { en: "We found schemes you can get",             hi: "हमें आपके लिए योजनाएँ मिलीं",              tone: "good",    icon: "🎉" },
  DOCUMENTS_AUDITED:   { en: "Your documents have been checked",         hi: "आपके दस्तावेज़ जाँच लिए गए",               tone: "neutral", icon: "📄" },
  MISSING_DOCUMENTS:   { en: "Action needed: names on your papers don't match", hi: "ध्यान दें: आपके कागज़ों पर नाम अलग है", tone: "warn", icon: "⚠️" },
  AWAITING_APPROVAL:   { en: "Ready to send — please review and confirm",      hi: "भेजने के लिए तैयार — कृपया जाँच कर पुष्टि करें", tone: "warn", icon: "🔔" },
  PENDING_UIPATH:      { en: "Application sent — waiting for confirmation", hi: "आवेदन भेज दिया — पुष्टि का इंतज़ार",      tone: "neutral", icon: "📨" },
  SUBMITTED_TO_UIPATH: { en: "Application sent — waiting for confirmation", hi: "आवेदन भेज दिया — पुष्टि का इंतज़ार",      tone: "neutral", icon: "📨" },
  COMPLETE:            { en: "Done! Your application is submitted",       hi: "हो गया! आपका आवेदन जमा हो गया",            tone: "good",    icon: "🎊" },
  SUBMISSION_FAILED:   { en: "Sorry, submitting failed. Please try again", hi: "माफ़ करें, जमा नहीं हो सका। फिर कोशिश करें", tone: "bad",  icon: "❌" },
  ESCALATED:           { en: "Your case was delayed — we raised it for you", hi: "आपका मामला देर से चल रहा — हमने आगे बढ़ाया", tone: "warn", icon: "📢" },
  REVOKED_BY_USER:     { en: "Your data has been deleted",               hi: "आपका डेटा हटा दिया गया",                   tone: "neutral", icon: "🗑️" },
  UNDER_REVIEW:        { en: "Your case is under review",                hi: "आपका मामला समीक्षा में है",                tone: "neutral", icon: "🔍" },
  FAILED:              { en: "Something went wrong",                     hi: "कुछ गड़बड़ हो गई",                          tone: "bad",     icon: "❌" },
};

export function friendlyStatus(status?: string | null): FriendlyStatus {
  if (status && STATUS_TEXT[status]) return STATUS_TEXT[status];
  return { en: "Working on your request…", hi: "आपके अनुरोध पर काम हो रहा है…", tone: "neutral", icon: "⏳" };
}

// Turn one raw backend SSE frame into a single simple sentence for the activity
// feed. Returns null for frames that shouldn't surface to a citizen.
export function friendlyLine(frame: AgentStreamFrame): { text: string; tone: "info" | "good" | "warn" | "bad" } | null {
  const d = frame.data ?? {};
  const agent = frame.agent_name;

  // Anomaly = the human-important "your name doesn't match" moment.
  if (frame.event_type === "anomaly_detected") {
    const anomaly = String(d.anomaly ?? d.alert ?? "A problem was found in your documents.");
    // Strip the technical tail like "(Jaro-Winkler=0.76, threshold=0.85)".
    const clean = anomaly.replace(/\s*\(Jaro-Winkler[^)]*\)/i, "").replace(/_/g, " ");
    return { text: `⚠️ ${clean}`, tone: "warn" };
  }

  if (frame.event_type === "error") {
    return { text: "Something went wrong while processing your request.", tone: "bad" };
  }

  // A spoken line (Bulbul TTS) — show the caption text.
  if (d.voice && typeof d.tts_text === "string") {
    return { text: `🔊 ${d.tts_text}`, tone: "info" };
  }

  switch (agent) {
    case "voice_intent_agent":
      if (frame.event_type === "agent_start") return { text: "Listening to what you need…", tone: "info" };
      if (typeof d.transcript === "string" && d.transcript) return { text: `We heard: "${d.transcript}"`, tone: "info" };
      if (frame.event_type === "agent_result") return { text: "We understood your details.", tone: "good" };
      return null;
    case "eligibility_router":
      if (frame.event_type === "agent_start") return { text: "Checking which government schemes you can get…", tone: "info" };
      if (frame.event_type === "agent_result") {
        const schemes = Array.isArray(d.eligible_schemes) ? (d.eligible_schemes as string[]) : [];
        const reasoning = typeof d.llm_reasoning === "string" && d.llm_reasoning ? d.llm_reasoning : null;
        const headline = schemes.length
          ? `Good news — you qualify for: ${schemes.join(", ")}.`
          : "We checked the schemes for you.";
        return { text: reasoning ? `${headline} ${reasoning}` : headline, tone: schemes.length ? "good" : "info" };
      }
      return null;
    case "document_audit":
      if (frame.event_type === "agent_start") return { text: "Reading and checking your documents…", tone: "info" };
      if (frame.event_type === "agent_result") {
        const n = Number(d.anomalies_count ?? 0);
        return n > 0
          ? { text: "We found a problem with your documents (see below).", tone: "warn" }
          : { text: "Your documents look good — names match.", tone: "good" };
      }
      return null;
    case "exception_management":
      if (frame.event_type === "agent_result") {
        const reasoning = typeof d.llm_reasoning === "string" && d.llm_reasoning ? d.llm_reasoning : null;
        return { text: reasoning || "We prepared a name-correction form for you.", tone: "warn" };
      }
      return { text: "Preparing how to fix your document problem…", tone: "warn" };
    case "npci_seeding":
      if (frame.event_type === "agent_start") return { text: "Checking if your bank account is linked to Aadhaar…", tone: "info" };
      if (frame.event_type === "agent_result") {
        const s = String(d.seeding_status ?? "");
        if (s === "SEEDED") return { text: "Your bank account is linked. Money can reach you.", tone: "good" };
        if (s === "PENDING") return { text: "Your bank link is still pending — please visit your bank.", tone: "warn" };
        return { text: "We checked your bank link.", tone: "info" };
      }
      return null;
    case "uipath_execution":
      if (frame.event_type === "agent_start") return { text: "Filling and sending your application to the government…", tone: "info" };
      if (frame.event_type === "agent_result") {
        const isSimulated = d.simulated === true || d.mode === "mock";
        return { text: isSimulated ? "Your application has been sent (demo mode — not live)." : "Your application has been sent.", tone: "good" };
      }
      return null;
    case "uipath_callback":
      return { text: "The government office confirmed your application.", tone: "good" };
    case "sla_watchdog":
      return { text: "Your case was delayed, so we raised an appeal for you.", tone: "warn" };
    default:
      return null;
  }
}

export const LANGUAGES = [
  { code: "hi-IN", label: "हिन्दी (Hindi)" },
  { code: "kn-IN", label: "ಕನ್ನಡ (Kannada)" },
  { code: "te-IN", label: "తెలుగు (Telugu)" },
  { code: "mr-IN", label: "मराठी (Marathi)" },
  { code: "ta-IN", label: "தமிழ் (Tamil)" },
  { code: "en-IN", label: "English (India)" },
];
