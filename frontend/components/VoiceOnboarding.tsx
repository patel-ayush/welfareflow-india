"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { initializeCase } from "@/lib/api";
import type { CaseInitResponse, CustomCitizenData, DocumentUpload } from "@/lib/types";
import { LANGUAGES } from "@/lib/friendly";

// ─── Demo citizen registry (mirrors backend mock_registry.py) ────────────────
const CITIZENS: Record<string, {
  name: string; state: string; district: string;
  income: number; land: number; age: number;
  aadhaar4: string; note: string; flag: "mismatch" | "pass" | "perfect";
  defaultLang: string; sampleTranscript: string;
}> = {
  "CITIZEN-001": {
    name: "Ramesh Kumar", state: "Karnataka", district: "Mandya",
    income: 48000, land: 2.0, age: 52, aadhaar4: "5678", flag: "mismatch",
    note: "Ration card reads 'Ramesha K' — will be caught by name-match engine.",
    defaultLang: "kn-IN",
    sampleTranscript: "ನಮಸ್ಕಾರ, ನಾನು ರಮೇಶ್ ಕುಮಾರ್. ನನ್ನ ಹೊಲ ಮಂಡ್ಯ ಜಿಲ್ಲೆಯಲ್ಲಿ ಇದೆ. PM ಕಿಸಾನ್ ಮತ್ತು ಆಯುಷ್ಮಾನ್ ಭಾರತ್ ಯೋಜನೆಗೆ ಅರ್ಜಿ ಸಲ್ಲಿಸಲು ಬಂದಿದ್ದೇನೆ.",
  },
  "CITIZEN-002": {
    name: "Lakshmi Devi", state: "Rajasthan", district: "Sikar",
    income: 36000, land: 1.5, age: 44, aadhaar4: "9012", flag: "pass",
    note: "Ration card reads 'Laxmi Devi' — vowel variant, score 0.86 → passes.",
    defaultLang: "hi-IN",
    sampleTranscript: "नमस्ते, मेरा नाम लक्ष्मी देवी है। मैं सीकर जिले से हूँ। मुझे PM किसान और आयुष्मान भारत योजना के लिए मदद चाहिए।",
  },
  "CITIZEN-003": {
    name: "Suresh Prasad", state: "Uttar Pradesh", district: "Varanasi",
    income: 29000, land: 0.8, age: 60, aadhaar4: "3456", flag: "perfect",
    note: "All three documents match perfectly — straight-through processing.",
    defaultLang: "hi-IN",
    sampleTranscript: "नमस्ते, मेरा नाम सुरेश प्रसाद है। मैं वाराणसी से हूँ। मुझे PM किसान सम्मान निधि योजना के बारे में जानकारी चाहिए।",
  },
  "CITIZEN-004": {
    name: "Priya Sharma", state: "Maharashtra", district: "Nashik",
    income: 75000, land: 3.2, age: 38, aadhaar4: "7890", flag: "pass",
    note: "Ration card has an extra letter 'Priya Sharmaa' — just passes.",
    defaultLang: "mr-IN",
    sampleTranscript: "नमस्कार, माझे नाव प्रिया शर्मा आहे. मी नाशिकमधून आहे. मला PM किसान आणि आयुष्मान भारत योजनेसाठी अर्ज करायचा आहे.",
  },
  "CITIZEN-005": {
    name: "Mohammed Rashid", state: "Telangana", district: "Nizamabad",
    income: 32000, land: 1.0, age: 48, aadhaar4: "2345", flag: "mismatch",
    note: "Passbook has 'Mohd Rashid' — abbreviation triggers mismatch flag.",
    defaultLang: "te-IN",
    sampleTranscript: "నమస్కారం, నా పేరు మొహమ్మద్ రషీద్. నేను నిజామాబాద్ నుండి వచ్చాను. నాకు PM కిసాన్ మరియు ఆయుష్మాన్ భారత్ పథకానికి దరఖాస్తు చేయాలి.",
  },
};

type Mode = "text" | "voice";
type Tab = "demo" | "live";
type RecordState = "idle" | "recording" | "processing" | "done" | "error";

function getSupportedMimeType(): string {
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
  for (const t of candidates) {
    if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

function uint8ToBase64(bytes: Uint8Array): string {
  let b = "";
  bytes.forEach((v) => (b += String.fromCharCode(v)));
  return btoa(b);
}

async function fileToBase64(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  return uint8ToBase64(new Uint8Array(buf));
}

// A 1×1 transparent PNG — used when no actual file is uploaded (demo mode).
const TINY_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

const STANDARD_DOCS: DocumentUpload[] = [
  { document_type: "aadhaar",      image_base64: TINY_PNG, filename: "aadhaar.png" },
  { document_type: "ration_card",  image_base64: TINY_PNG, filename: "ration_card.png" },
  { document_type: "bank_passbook",image_base64: TINY_PNG, filename: "bank_passbook.png" },
];

interface Props {
  onCaseReady: (caseId: string, trackingToken: string) => void;
}

// ─── Custom Citizen Form ──────────────────────────────────────────────────────

const INDIAN_STATES = [
  "Andhra Pradesh","Arunachal Pradesh","Assam","Bihar","Chhattisgarh","Goa","Gujarat",
  "Haryana","Himachal Pradesh","Jharkhand","Karnataka","Kerala","Madhya Pradesh",
  "Maharashtra","Manipur","Meghalaya","Mizoram","Nagaland","Odisha","Punjab",
  "Rajasthan","Sikkim","Tamil Nadu","Telangana","Tripura","Uttar Pradesh","Uttarakhand",
  "West Bengal","Delhi","Jammu and Kashmir","Ladakh","Puducherry",
];

interface DocSlot {
  type: "aadhaar" | "ration_card" | "bank_passbook";
  label: string;
  hint: string;
  sampleFile: string;
  sampleName: string;
}

const DOC_SLOTS: DocSlot[] = [
  {
    type: "aadhaar",
    label: "Aadhaar Card",
    hint: "Upload a photo or scan of your Aadhaar",
    sampleFile: "/sample-docs/aadhaar_ramesh_kumar.png",
    sampleName: "aadhaar_ramesh_kumar.png",
  },
  {
    type: "ration_card",
    label: "Ration Card",
    hint: "Upload a photo or scan of your Ration Card",
    sampleFile: "/sample-docs/ration_card_ramesha_k.png",
    sampleName: "ration_card_ramesha_k.png",
  },
  {
    type: "bank_passbook",
    label: "Bank Passbook",
    hint: "Upload front page of your bank passbook",
    sampleFile: "/sample-docs/passbook_r_kumar.png",
    sampleName: "passbook_r_kumar.png",
  },
];

const FLAG_COLORS: Record<string, string> = {
  mismatch: "bg-red-50 border-red-200 text-red-700",
  pass:     "bg-amber-50 border-amber-200 text-amber-700",
  perfect:  "bg-green-50 border-green-200 text-green-700",
};
const FLAG_ICONS: Record<string, string> = { mismatch: "⚠️", pass: "✅", perfect: "✅" };

export default function VoiceOnboarding({ onCaseReady }: Props) {
  const [tab, setTab]           = useState<Tab>("demo");
  const [mode, setMode]         = useState<Mode>("text");
  const [citizenId, setCitizenId] = useState("CITIZEN-001");
  const [langCode, setLangCode] = useState("kn-IN");
  const [transcript, setTranscript] = useState("");
  const [consentGiven, setConsentGiven] = useState(false);
  const [requireApproval, setRequireApproval] = useState(false);
  const [recState, setRecState] = useState<RecordState>("idle");
  const [caseResp, setCaseResp] = useState<CaseInitResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState("");
  const [submitting, setSubmitting] = useState(false);

  // Live-mode citizen fields
  const [liveNameAadhaar,    setLiveNameAadhaar]    = useState("Ramesh Kumar");
  const [liveNameRation,     setLiveNameRation]      = useState("Ramesha K");
  const [liveNamePassbook,   setLiveNamePassbook]    = useState("R. Kumar");
  const [liveState,          setLiveState]           = useState("Karnataka");
  const [liveDistrict,       setLiveDistrict]        = useState("Mandya");
  const [liveIncome,         setLiveIncome]          = useState("48000");
  const [liveLand,           setLiveLand]            = useState("2.0");
  const [liveAge,            setLiveAge]             = useState("52");
  const [liveAadhaar4,       setLiveAadhaar4]        = useState("5678");
  const [liveTranscript,     setLiveTranscript]      = useState(
    "Namaste, my name is Ramesh Kumar. I am from Mandya, Karnataka. " +
    "I have 2 acres of farmland and need help with PM-Kisan and Ayushman Bharat."
  );

  // Uploaded document files (live mode)
  const [uploadedDocs, setUploadedDocs] = useState<Record<string, File | null>>({
    aadhaar: null, ration_card: null, bank_passbook: null,
  });

  const mediaRef  = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);

  const citizen = CITIZENS[citizenId];

  useEffect(() => {
    setTranscript(citizen.sampleTranscript);
    setLangCode(citizen.defaultLang);
    setCaseResp(null);
    setErrorMsg("");
    setRecState("idle");
  }, [citizenId, citizen.sampleTranscript, citizen.defaultLang]);

  const stopMedia = useCallback(() => {
    mediaRef.current?.stop();
    mediaRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, []);
  useEffect(() => () => stopMedia(), [stopMedia]);

  // ── Build docs array for live mode ───────────────────────────────────────
  const buildLiveDocs = useCallback(async (): Promise<DocumentUpload[]> => {
    const result: DocumentUpload[] = [];
    for (const slot of DOC_SLOTS) {
      const file = uploadedDocs[slot.type];
      const b64 = file ? await fileToBase64(file) : TINY_PNG;
      result.push({ document_type: slot.type, image_base64: b64, filename: file?.name ?? `${slot.type}.png` });
    }
    return result;
  }, [uploadedDocs]);

  // ── Submit: demo mode ─────────────────────────────────────────────────────
  const handleDemoSubmit = useCallback(async () => {
    if (!consentGiven || !transcript.trim() || submitting) return;
    setSubmitting(true); setErrorMsg("");
    try {
      const resp = await initializeCase({
        citizen_id: citizenId,
        consent_given: true,
        raw_transcript: transcript.trim(),
        language_code: langCode,
        documents: STANDARD_DOCS,
        require_approval: requireApproval || undefined,
      });
      setCaseResp(resp);
      onCaseReady(resp.case_id, resp.tracking_token);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }, [citizenId, consentGiven, langCode, onCaseReady, requireApproval, submitting, transcript]);

  // ── Submit: live mode ─────────────────────────────────────────────────────
  const handleLiveSubmit = useCallback(async () => {
    if (!consentGiven || !liveTranscript.trim() || submitting) return;
    if (!liveNameAadhaar.trim() || !liveNameRation.trim() || !liveNamePassbook.trim()) {
      setErrorMsg("Please fill in the name on each document.");
      return;
    }
    setSubmitting(true); setErrorMsg("");
    try {
      const docs = await buildLiveDocs();
      const customCitizen: CustomCitizenData = {
        name_aadhaar:      liveNameAadhaar.trim(),
        name_ration_card:  liveNameRation.trim(),
        name_passbook:     liveNamePassbook.trim(),
        state:             liveState,
        district:          liveDistrict,
        annual_income_inr: parseInt(liveIncome) || 40000,
        land_area_acres:   parseFloat(liveLand) || 2.0,
        age:               parseInt(liveAge) || 40,
        aadhaar_last4:     liveAadhaar4.slice(-4).padStart(4, "0"),
        phone:             "9999999999",
      };
      const resp = await initializeCase({
        custom_citizen: customCitizen,
        consent_given: true,
        raw_transcript: liveTranscript.trim(),
        language_code: langCode,
        documents: docs,
        require_approval: requireApproval || undefined,
      });
      setCaseResp(resp);
      onCaseReady(resp.case_id, resp.tracking_token);
    } catch (err) {
      setErrorMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }, [
    consentGiven, liveTranscript, submitting, liveNameAadhaar, liveNameRation,
    liveNamePassbook, liveState, liveDistrict, liveIncome, liveLand, liveAge,
    liveAadhaar4, langCode, requireApproval, onCaseReady, buildLiveDocs,
  ]);

  // ── Voice recording ───────────────────────────────────────────────────────
  const startRecording = useCallback(async () => {
    if (recState === "recording" || !consentGiven) return;
    setErrorMsg("");
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setErrorMsg("Microphone access denied — please allow and try again.");
      setRecState("error"); return;
    }
    streamRef.current = stream;
    const mimeType = getSupportedMimeType();
    const mr = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
    chunksRef.current = [];
    mr.ondataavailable = (ev) => { if (ev.data.size > 0) chunksRef.current.push(ev.data); };
    mr.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      setRecState("processing");
      const blob = new Blob(chunksRef.current, { type: mimeType || "audio/webm" });
      let audioBase64: string | undefined;
      try { audioBase64 = uint8ToBase64(new Uint8Array(await blob.arrayBuffer())); } catch { /* */ }
      try {
        const docs = tab === "live" ? await buildLiveDocs() : STANDARD_DOCS;
        const payload = tab === "live" ? {
          custom_citizen: {
            name_aadhaar: liveNameAadhaar.trim(),
            name_ration_card: liveNameRation.trim(),
            name_passbook: liveNamePassbook.trim(),
            state: liveState, district: liveDistrict,
            annual_income_inr: parseInt(liveIncome) || 40000,
            land_area_acres: parseFloat(liveLand) || 2.0,
            age: parseInt(liveAge) || 40,
            aadhaar_last4: liveAadhaar4.slice(-4).padStart(4, "0"),
            phone: "9999999999",
          },
          consent_given: true, audio_base64: audioBase64,
          language_code: langCode, documents: docs,
          require_approval: requireApproval || undefined,
        } : {
          citizen_id: citizenId, consent_given: true, audio_base64: audioBase64,
          language_code: langCode, documents: docs, require_approval: requireApproval || undefined,
        };
        const resp = await initializeCase(payload);
        setCaseResp(resp); setRecState("done");
        onCaseReady(resp.case_id, resp.tracking_token);
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : String(err));
        setRecState("error");
      }
    };
    mr.start(); mediaRef.current = mr; setRecState("recording");
  }, [
    recState, consentGiven, tab, citizenId, langCode, requireApproval,
    liveNameAadhaar, liveNameRation, liveNamePassbook, liveState, liveDistrict,
    liveIncome, liveLand, liveAge, liveAadhaar4, buildLiveDocs, onCaseReady,
  ]);

  const stopRecording = useCallback(() => { mediaRef.current?.stop(); mediaRef.current = null; }, []);

  const micLabel: Record<RecordState, string> = {
    idle: "Tap and speak", recording: "Listening… tap to stop",
    processing: "Please wait…", done: "Got it!", error: "Try again",
  };
  const canRecord = consentGiven && recState !== "processing" && recState !== "done";
  const isBusy    = submitting || recState === "recording" || recState === "processing";
  const isDone    = !!caseResp;

  return (
    <div className="flex flex-col gap-4">

      {/* ── Tab switcher ───────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-2">
        <button
          onClick={() => { setTab("demo"); setCaseResp(null); setErrorMsg(""); }}
          className={`py-2.5 rounded-xl font-semibold text-base border-2 transition ${
            tab === "demo"
              ? "bg-bharat-saffron text-white border-bharat-saffron"
              : "bg-white text-stone-600 border-stone-300 hover:border-bharat-saffron"
          }`}
        >
          🎭 Demo Scenarios
        </button>
        <button
          onClick={() => { setTab("live"); setCaseResp(null); setErrorMsg(""); }}
          className={`py-2.5 rounded-xl font-semibold text-base border-2 transition ${
            tab === "live"
              ? "bg-bharat-green text-white border-bharat-green"
              : "bg-white text-stone-600 border-stone-300 hover:border-bharat-green"
          }`}
        >
          👤 My Own Details
        </button>
      </div>

      {/* ══════════════════════════════════════════════════════════════
          DEMO TAB
      ══════════════════════════════════════════════════════════════ */}
      {tab === "demo" && (
        <>
          <div>
            <label className="block text-sm font-semibold text-stone-600 mb-1.5">
              Choose a demo citizen <span className="text-stone-400 font-normal">· परीक्षण नागरिक</span>
            </label>
            <select
              className="w-full bg-white border-2 border-stone-300 text-stone-900 text-base rounded-xl px-3 py-2.5 focus:outline-none focus:ring-2 focus:ring-bharat-saffron"
              value={citizenId}
              onChange={(e) => { setCitizenId(e.target.value); setCaseResp(null); }}
              disabled={isBusy || isDone}
            >
              {Object.entries(CITIZENS).map(([id, c]) => (
                <option key={id} value={id}>{c.name} — {c.district}, {c.state}</option>
              ))}
            </select>

            <div className="mt-2 rounded-xl border border-stone-200 bg-white px-3 py-2.5 text-sm">
              <div className="flex items-center justify-between mb-1">
                <span className="font-semibold text-stone-800">{citizen.name}</span>
                <span className="text-stone-400 text-xs">Aadhaar ···· {citizen.aadhaar4}</span>
              </div>
              <div className="text-stone-600 flex flex-wrap gap-x-3 gap-y-0.5 text-xs mb-2">
                <span>📍 {citizen.district}, {citizen.state}</span>
                <span>💰 ₹{citizen.income.toLocaleString("en-IN")}/yr</span>
                <span>🌾 {citizen.land} acres</span>
              </div>
              <div className={`rounded-lg border px-2.5 py-1.5 text-xs ${FLAG_COLORS[citizen.flag]}`}>
                {FLAG_ICONS[citizen.flag]} {citizen.note}
              </div>
            </div>
          </div>

          {/* Input mode */}
          <div>
            <label className="block text-sm font-semibold text-stone-600 mb-1.5">
              How to tell us? <span className="text-stone-400 font-normal">· कैसे बताएँगे?</span>
            </label>
            <div className="grid grid-cols-2 gap-2 mb-2">
              {(["text","voice"] as Mode[]).map((m) => (
                <button key={m} onClick={() => setMode(m)} disabled={isBusy || isDone}
                  className={`py-2.5 rounded-xl text-base font-semibold border-2 transition ${
                    mode === m ? "bg-bharat-saffron text-white border-bharat-saffron" : "bg-white text-stone-600 border-stone-300"
                  }`}>
                  {m === "text" ? "📝 Type" : "🎤 Speak"}
                </button>
              ))}
            </div>

            <div className="flex items-center gap-2">
              <label className="text-xs text-stone-500 shrink-0">Language</label>
              <select
                className="flex-1 bg-white border-2 border-stone-300 text-stone-900 text-sm rounded-xl px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-bharat-saffron"
                value={langCode} onChange={(e) => setLangCode(e.target.value)} disabled={isBusy || isDone}
              >
                {LANGUAGES.map((l) => <option key={l.code} value={l.code}>{l.label}</option>)}
              </select>
            </div>

            {mode === "text" ? (
              <textarea
                className="mt-2 w-full rounded-xl border-2 border-stone-300 bg-white text-stone-900 text-sm p-2.5 resize-none focus:outline-none focus:ring-2 focus:ring-bharat-saffron leading-relaxed"
                rows={3} placeholder="Tell us what help you need…"
                value={transcript} onChange={(e) => setTranscript(e.target.value)}
                disabled={isBusy || isDone}
              />
            ) : (
              <div className="mt-3 flex flex-col items-center gap-2">
                <button
                  onClick={recState === "recording" ? stopRecording : startRecording}
                  disabled={!canRecord} aria-label={micLabel[recState]}
                  className={`w-20 h-20 rounded-full flex items-center justify-center text-4xl border-4 transition ${
                    recState === "recording" ? "border-red-500 bg-red-50 animate-pulse"
                    : !canRecord ? "border-stone-200 bg-stone-100 opacity-50 cursor-not-allowed"
                    : "border-bharat-saffron bg-orange-50 hover:bg-orange-100 active:scale-95"
                  }`}
                >🎤</button>
                <p className="text-sm font-medium text-stone-700">{micLabel[recState]}</p>
              </div>
            )}
          </div>
        </>
      )}

      {/* ══════════════════════════════════════════════════════════════
          LIVE TAB — My Own Details
      ══════════════════════════════════════════════════════════════ */}
      {tab === "live" && (
        <>
          {/* Name fields — the critical inputs for name-match demo */}
          <div className="rounded-xl border-2 border-bharat-green/30 bg-green-50 px-3 py-3">
            <p className="text-xs font-semibold text-bharat-green mb-2 uppercase tracking-wide">
              📄 Names on your documents — ये नाम सटीक रूप से दर्ज करें
            </p>
            <p className="text-xs text-stone-500 mb-3">
              Enter the name <em>exactly as printed</em> on each document. Even small differences (like &ldquo;Ramesha K&rdquo; vs &ldquo;Ramesh Kumar&rdquo;) will be caught by our AI before they reach the government.
            </p>
            {[
              { label: "Name on Aadhaar Card", val: liveNameAadhaar, set: setLiveNameAadhaar, hint: "e.g. RAMESH KUMAR" },
              { label: "Name on Ration Card",  val: liveNameRation,  set: setLiveNameRation,  hint: "e.g. RAMESHA K" },
              { label: "Name on Bank Passbook",val: liveNamePassbook,set: setLiveNamePassbook, hint: "e.g. R. KUMAR" },
            ].map(({ label, val, set, hint }) => (
              <div key={label} className="mb-2">
                <label className="block text-xs text-stone-600 mb-0.5">{label}</label>
                <input
                  type="text" value={val} onChange={(e) => set(e.target.value)}
                  placeholder={hint} disabled={isBusy || isDone}
                  className="w-full rounded-lg border border-stone-300 bg-white text-stone-900 text-sm px-2.5 py-2 focus:outline-none focus:ring-2 focus:ring-bharat-green"
                />
              </div>
            ))}
          </div>

          {/* Basic details */}
          <div className="grid grid-cols-2 gap-2">
            {[
              { label: "Age", val: liveAge, set: setLiveAge, type: "number", placeholder: "52" },
              { label: "Aadhaar last 4 digits", val: liveAadhaar4, set: setLiveAadhaar4, type: "text", placeholder: "5678" },
              { label: "Annual Income (₹)", val: liveIncome, set: setLiveIncome, type: "number", placeholder: "48000" },
              { label: "Land (acres)", val: liveLand, set: setLiveLand, type: "number", placeholder: "2.0" },
            ].map(({ label, val, set, type, placeholder }) => (
              <div key={label}>
                <label className="block text-xs text-stone-500 mb-0.5">{label}</label>
                <input
                  type={type} value={val} onChange={(e) => set(e.target.value)}
                  placeholder={placeholder} disabled={isBusy || isDone}
                  className="w-full rounded-lg border border-stone-300 bg-white text-stone-900 text-sm px-2.5 py-2 focus:outline-none focus:ring-2 focus:ring-bharat-saffron"
                />
              </div>
            ))}
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="block text-xs text-stone-500 mb-0.5">State</label>
              <select
                value={liveState} onChange={(e) => setLiveState(e.target.value)} disabled={isBusy || isDone}
                className="w-full rounded-lg border border-stone-300 bg-white text-stone-900 text-sm px-2.5 py-2 focus:outline-none focus:ring-2 focus:ring-bharat-saffron"
              >
                {INDIAN_STATES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
            </div>
            <div>
              <label className="block text-xs text-stone-500 mb-0.5">District</label>
              <input
                type="text" value={liveDistrict} onChange={(e) => setLiveDistrict(e.target.value)}
                placeholder="Mandya" disabled={isBusy || isDone}
                className="w-full rounded-lg border border-stone-300 bg-white text-stone-900 text-sm px-2.5 py-2 focus:outline-none focus:ring-2 focus:ring-bharat-saffron"
              />
            </div>
          </div>

          {/* Language */}
          <div className="flex items-center gap-2">
            <label className="text-xs text-stone-500 shrink-0">Language / भाषा</label>
            <select
              className="flex-1 bg-white border border-stone-300 text-stone-900 text-sm rounded-lg px-2.5 py-2 focus:outline-none focus:ring-2 focus:ring-bharat-saffron"
              value={langCode} onChange={(e) => setLangCode(e.target.value)} disabled={isBusy || isDone}
            >
              {LANGUAGES.map((l) => <option key={l.code} value={l.code}>{l.label}</option>)}
            </select>
          </div>

          {/* What you need (transcript) */}
          <div>
            <label className="block text-xs text-stone-600 mb-0.5">Tell us what help you need</label>
            <textarea
              className="w-full rounded-xl border-2 border-stone-300 bg-white text-stone-900 text-sm p-2.5 resize-none focus:outline-none focus:ring-2 focus:ring-bharat-green leading-relaxed"
              rows={3} value={liveTranscript} onChange={(e) => setLiveTranscript(e.target.value)}
              disabled={isBusy || isDone}
            />
          </div>

          {/* Document upload */}
          <div>
            <p className="text-xs font-semibold text-stone-600 mb-1 uppercase tracking-wide">
              📎 Upload your documents <span className="font-normal text-stone-400">(optional — or use our sample docs)</span>
            </p>
            <div className="rounded-xl border border-bharat-saffron/30 bg-amber-50 px-3 py-2 mb-2 text-xs text-amber-800">
              <strong>Try the mismatch demo:</strong> Download our sample Aadhaar + Ration Card below — the names deliberately differ so you can see the AI catch the discrepancy live.
            </div>
            {DOC_SLOTS.map((slot) => (
              <div key={slot.type} className="mb-2 rounded-lg border border-stone-200 bg-white px-3 py-2">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs font-semibold text-stone-700">{slot.label}</span>
                  <a
                    href={slot.sampleFile} download={slot.sampleName}
                    className="text-xs text-bharat-saffron underline hover:text-amber-700"
                    onClick={(e) => e.stopPropagation()}
                  >
                    ↓ Download sample
                  </a>
                </div>
                <label className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="file" accept="image/*,.pdf" className="hidden"
                    disabled={isBusy || isDone}
                    onChange={(e) => {
                      const file = e.target.files?.[0] ?? null;
                      setUploadedDocs((prev) => ({ ...prev, [slot.type]: file }));
                    }}
                  />
                  <div className={`flex-1 rounded-lg border-2 border-dashed px-3 py-2 text-xs text-center transition ${
                    uploadedDocs[slot.type] ? "border-bharat-green bg-green-50 text-bharat-green" : "border-stone-300 text-stone-400 hover:border-bharat-saffron"
                  }`}>
                    {uploadedDocs[slot.type]
                      ? `✅ ${uploadedDocs[slot.type]!.name}`
                      : `${slot.hint} — click to browse`}
                  </div>
                </label>
              </div>
            ))}
          </div>
        </>
      )}

      {/* ── Consent (shared) ────────────────────────────────────────────────── */}
      <label className="flex items-start gap-3 cursor-pointer rounded-xl border-2 border-stone-200 bg-white p-3">
        <input
          type="checkbox" checked={consentGiven} onChange={(e) => setConsentGiven(e.target.checked)}
          disabled={isBusy || isDone} className="mt-1 w-5 h-5 accent-bharat-green"
        />
        <span className="text-sm text-stone-700 leading-relaxed">
          I agree to let WelfareFlow use my details to check and apply for welfare schemes.
          <span className="block text-xs text-stone-500 mt-0.5">
            मैं अपनी जानकारी योजनाओं के लिए उपयोग करने की अनुमति देता/देती हूँ। (DPDP Act 2023)
          </span>
        </span>
      </label>

      {/* Expert toggle */}
      <label className="flex items-center gap-3 cursor-pointer text-xs text-stone-500">
        <input
          type="checkbox" checked={requireApproval} onChange={(e) => setRequireApproval(e.target.checked)}
          disabled={isBusy || isDone} className="w-4 h-4 accent-bharat-saffron"
        />
        <span>Require volunteer review before sending · भेजने से पहले समीक्षा</span>
      </label>

      {/* Submit button */}
      <button
        onClick={tab === "demo" ? handleDemoSubmit : handleLiveSubmit}
        disabled={!consentGiven || isBusy || isDone || (tab === "demo" && !transcript.trim()) || (tab === "live" && !liveTranscript.trim())}
        className="w-full py-4 rounded-2xl bg-bharat-green text-white text-xl font-bold disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-105 active:scale-[0.99] transition"
      >
        {submitting ? "Please wait…" : "Get my help →"}
      </button>

      {errorMsg && (
        <p className="text-sm text-red-600 text-center border-2 border-red-200 bg-red-50 rounded-xl p-3">{errorMsg}</p>
      )}

      {caseResp && (
        <div className="rounded-xl border-2 border-green-300 bg-green-50 px-4 py-3 text-sm">
          <p className="text-green-800 font-bold mb-1">✅ We have started helping you.</p>
          <p className="text-stone-600 text-xs">Watch the steps on the right. आगे की जानकारी दाहिनी ओर देखें।</p>
          <button
            onClick={() => { setCaseResp(null); setRecState("idle"); setErrorMsg(""); }}
            className="mt-2 text-bharat-saffron underline text-xs"
          >
            Start again
          </button>
        </div>
      )}
    </div>
  );
}
