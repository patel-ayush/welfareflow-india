"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { initializeCase } from "@/lib/api";
import type { CaseInitResponse } from "@/lib/types";
import { LANGUAGES } from "@/lib/friendly";

// ─── Demo citizen registry (mirrors backend mock_registry.py) ────────────────
const CITIZENS: Record<string, {
  name: string; state: string; district: string;
  income: number; land: number; age: number;
  aadhaar4: string; note: string;
  defaultLang: string; sampleTranscript: string;
}> = {
  "CITIZEN-001": {
    name: "Ramesh Kumar", state: "Karnataka", district: "Mandya",
    income: 48000, land: 2.0, age: 52, aadhaar4: "5678",
    note: "Ration card name is slightly different — we will help fix it.",
    defaultLang: "kn-IN",
    sampleTranscript: "ನಮಸ್ಕಾರ, ನಾನು ರಮೇಶ್ ಕುಮಾರ್. ನನ್ನ ಹೊಲ ಮಂಡ್ಯ ಜಿಲ್ಲೆಯಲ್ಲಿ ಇದೆ. PM ಕಿಸಾನ್ ಮತ್ತು ಆಯುಷ್ಮಾನ್ ಭಾರತ್ ಯೋಜನೆಗೆ ಅರ್ಜಿ ಸಲ್ಲಿಸಲು ಬಂದಿದ್ದೇನೆ.",
  },
  "CITIZEN-002": {
    name: "Lakshmi Devi", state: "Rajasthan", district: "Sikar",
    income: 36000, land: 1.5, age: 44, aadhaar4: "9012",
    note: "Spelling variation in name — should pass smoothly.",
    defaultLang: "hi-IN",
    sampleTranscript: "नमस्ते, मेरा नाम लक्ष्मी देवी है। मैं सीकर जिले से हूँ। मुझे PM किसान और आयुष्मान भारत योजना के लिए मदद चाहिए।",
  },
  "CITIZEN-003": {
    name: "Suresh Prasad", state: "Uttar Pradesh", district: "Varanasi",
    income: 29000, land: 0.8, age: 60, aadhaar4: "3456",
    note: "All names match — application goes straight through.",
    defaultLang: "hi-IN",
    sampleTranscript: "नमस्ते, मेरा नाम सुरेश प्रसाद है। मैं वाराणसी से हूँ। मुझे PM किसान सम्मान निधि योजना के बारे में जानकारी चाहिए।",
  },
  "CITIZEN-004": {
    name: "Priya Sharma", state: "Maharashtra", district: "Nashik",
    income: 75000, land: 3.2, age: 38, aadhaar4: "7890",
    note: "Extra letter in ration card name — we will help fix it.",
    defaultLang: "mr-IN",
    sampleTranscript: "नमस्कार, माझे नाव प्रिया शर्मा आहे. मी नाशिकमधून आहे. मला PM किसान आणि आयुष्मान भारत योजनेसाठी अर्ज करायचा आहे.",
  },
  "CITIZEN-005": {
    name: "Mohammed Rashid", state: "Telangana", district: "Nizamabad",
    income: 32000, land: 1.0, age: 48, aadhaar4: "2345",
    note: "Short form of name on passbook — we will help fix it.",
    defaultLang: "te-IN",
    sampleTranscript: "నమస్కారం, నా పేరు మొహమ్మద్ రషీద్. నేను నిజామాబాద్ నుండి వచ్చాను. నాకు PM కిసాన్ మరియు ఆయుష్మాన్ భారత్ పథకానికి దరఖాస్తు చేయాలి.",
  },
};

type Mode = "text" | "voice";
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

// A 1×1 transparent PNG. In the sandbox the OCR is citizen-aware (it does not
// read the image bytes), so this placeholder lets the document-matching feature
// run for the demo. In production these would be real document photos.
const TINY_PNG =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

const STANDARD_DOCS = [
  { document_type: "aadhaar" as const, image_base64: TINY_PNG, filename: "aadhaar.png" },
  { document_type: "ration_card" as const, image_base64: TINY_PNG, filename: "ration_card.png" },
  { document_type: "bank_passbook" as const, image_base64: TINY_PNG, filename: "bank_passbook.png" },
];

interface Props {
  onCaseReady: (caseId: string, trackingToken: string) => void;
}

export default function VoiceOnboarding({ onCaseReady }: Props) {
  const [mode, setMode] = useState<Mode>("text");
  const [citizenId, setCitizenId] = useState("CITIZEN-001");
  const [langCode, setLangCode] = useState("kn-IN");
  const [transcript, setTranscript] = useState("");
  const [consentGiven, setConsentGiven] = useState(false);
  const [requireApproval, setRequireApproval] = useState(false);
  const [recState, setRecState] = useState<RecordState>("idle");
  const [caseResp, setCaseResp] = useState<CaseInitResponse | null>(null);
  const [errorMsg, setErrorMsg] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const mediaRef = useRef<MediaRecorder | null>(null);
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

  const handleTextSubmit = useCallback(async () => {
    if (!consentGiven || !transcript.trim() || submitting) return;
    setSubmitting(true);
    setErrorMsg("");
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

  const startRecording = useCallback(async () => {
    if (recState === "recording" || !consentGiven) return;
    setErrorMsg("");
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      setErrorMsg("We could not use your microphone. Please allow microphone access.");
      setRecState("error");
      return;
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
      try {
        audioBase64 = uint8ToBase64(new Uint8Array(await blob.arrayBuffer()));
      } catch { /* non-fatal */ }
      try {
        const resp = await initializeCase({
          citizen_id: citizenId,
          consent_given: true,
          audio_base64: audioBase64,
          language_code: langCode,
          documents: STANDARD_DOCS,
          require_approval: requireApproval || undefined,
        });
        setCaseResp(resp);
        setRecState("done");
        onCaseReady(resp.case_id, resp.tracking_token);
      } catch (err) {
        setErrorMsg(err instanceof Error ? err.message : String(err));
        setRecState("error");
      }
    };
    mr.start();
    mediaRef.current = mr;
    setRecState("recording");
  }, [citizenId, consentGiven, langCode, onCaseReady, recState]);

  const stopRecording = useCallback(() => {
    mediaRef.current?.stop();
    mediaRef.current = null;
  }, []);

  const micLabel: Record<RecordState, string> = {
    idle: "Tap and speak",
    recording: "Listening… tap to stop",
    processing: "Please wait…",
    done: "Got it!",
    error: "Try again",
  };
  const canRecord = consentGiven && recState !== "processing" && recState !== "done";

  return (
    <div className="flex flex-col gap-5">
      {/* Step 1 — Choose your name (demo) */}
      <div>
        <label className="block text-base font-semibold text-stone-700 mb-2">
          1. Choose your name <span className="text-stone-400 font-normal">· अपना नाम चुनें</span>
        </label>
        <select
          className="w-full bg-white border-2 border-stone-300 text-stone-900 text-lg rounded-xl px-4 py-3 focus:outline-none focus:ring-2 focus:ring-bharat-saffron"
          value={citizenId}
          onChange={(e) => { setCitizenId(e.target.value); setCaseResp(null); }}
          disabled={submitting || recState === "recording" || recState === "processing"}
        >
          {Object.entries(CITIZENS).map(([id, c]) => (
            <option key={id} value={id}>{c.name} — {c.district}, {c.state}</option>
          ))}
        </select>

        <div className="mt-3 rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-stone-800 text-base">{citizen.name}</span>
            <span className="text-stone-400">Aadhaar ···· {citizen.aadhaar4}</span>
          </div>
          <div className="mt-1 text-stone-600 flex flex-wrap gap-x-4 gap-y-1">
            <span>📍 {citizen.district}, {citizen.state}</span>
            <span>💰 ₹{citizen.income.toLocaleString("en-IN")}/year</span>
            <span>🌾 {citizen.land} acres</span>
          </div>
          <p className="mt-2 text-stone-500 border-t border-stone-100 pt-2">ℹ️ {citizen.note}</p>
        </div>
      </div>

      {/* Step 2 — How to tell us */}
      <div>
        <label className="block text-base font-semibold text-stone-700 mb-2">
          2. How would you like to talk to us? <span className="text-stone-400 font-normal">· कैसे बताएँगे?</span>
        </label>
        <div className="grid grid-cols-2 gap-2">
          <button
            onClick={() => setMode("text")}
            className={`py-3 rounded-xl text-lg font-semibold border-2 transition ${mode === "text" ? "bg-bharat-saffron text-white border-bharat-saffron" : "bg-white text-stone-600 border-stone-300"}`}
          >
            📝 Type
          </button>
          <button
            onClick={() => setMode("voice")}
            className={`py-3 rounded-xl text-lg font-semibold border-2 transition ${mode === "voice" ? "bg-bharat-saffron text-white border-bharat-saffron" : "bg-white text-stone-600 border-stone-300"}`}
          >
            🎤 Speak
          </button>
        </div>

        {/* Language */}
        <div className="mt-3 flex items-center gap-2">
          <label className="text-sm text-stone-500 shrink-0">Language · भाषा</label>
          <select
            className="flex-1 bg-white border-2 border-stone-300 text-stone-900 text-base rounded-xl px-3 py-2 focus:outline-none focus:ring-2 focus:ring-bharat-saffron"
            value={langCode}
            onChange={(e) => setLangCode(e.target.value)}
            disabled={submitting || recState === "recording" || recState === "processing"}
          >
            {LANGUAGES.map((l) => (
              <option key={l.code} value={l.code}>{l.label}</option>
            ))}
          </select>
        </div>

        {mode === "text" ? (
          <textarea
            className="mt-3 w-full rounded-xl border-2 border-stone-300 bg-white text-stone-900 text-base p-3 resize-none focus:outline-none focus:ring-2 focus:ring-bharat-saffron leading-relaxed"
            rows={4}
            placeholder="Tell us what help you need…"
            value={transcript}
            onChange={(e) => setTranscript(e.target.value)}
            disabled={submitting || !!caseResp}
          />
        ) : (
          <div className="mt-4 flex flex-col items-center gap-3">
            <button
              onClick={recState === "recording" ? stopRecording : startRecording}
              disabled={!canRecord}
              aria-label={micLabel[recState]}
              className={`w-28 h-28 rounded-full flex items-center justify-center text-5xl border-4 transition ${
                recState === "recording"
                  ? "border-red-500 bg-red-50 animate-pulse"
                  : !canRecord
                  ? "border-stone-200 bg-stone-100 opacity-50 cursor-not-allowed"
                  : "border-bharat-saffron bg-orange-50 hover:bg-orange-100 active:scale-95"
              }`}
            >
              🎤
            </button>
            <p className="text-base font-medium text-stone-700">{micLabel[recState]}</p>
          </div>
        )}
      </div>

      {/* Documents note */}
      <div className="rounded-xl border border-stone-200 bg-white px-4 py-3 text-sm text-stone-600">
        📎 We will check your <b>Aadhaar</b>, <b>Ration Card</b> and <b>Bank Passbook</b> for name matches.
        <span className="block text-stone-400 mt-0.5">हम आपके आधार, राशन कार्ड और बैंक पासबुक की जाँच करेंगे।</span>
      </div>

      {/* Step 3 — Consent */}
      <div>
        <label className="flex items-start gap-3 cursor-pointer rounded-xl border-2 border-stone-200 bg-white p-3">
          <input
            type="checkbox"
            checked={consentGiven}
            onChange={(e) => setConsentGiven(e.target.checked)}
            disabled={submitting || recState === "recording" || recState === "processing" || !!caseResp}
            className="mt-1 w-5 h-5 accent-bharat-green"
          />
          <span className="text-base text-stone-700 leading-relaxed">
            I agree to let WelfareFlow use my details to check and apply for welfare schemes.
            <span className="block text-sm text-stone-500 mt-0.5">
              मैं अपनी जानकारी योजनाओं के लिए उपयोग करने की अनुमति देता/देती हूँ। (DPDP Act 2023)
            </span>
          </span>
        </label>
      </div>

      {/* Expert toggle — require human review before submit */}
      <label className="flex items-center gap-3 cursor-pointer text-sm text-stone-500">
        <input
          type="checkbox"
          checked={requireApproval}
          onChange={(e) => setRequireApproval(e.target.checked)}
          disabled={submitting || recState === "recording" || recState === "processing" || !!caseResp}
          className="w-4 h-4 accent-bharat-saffron"
        />
        <span>Require volunteer to review before sending · भेजने से पहले समीक्षा आवश्यक</span>
      </label>

      {/* Submit (text mode) */}
      {mode === "text" && (
        <button
          onClick={handleTextSubmit}
          disabled={!consentGiven || !transcript.trim() || submitting || !!caseResp}
          className="w-full py-4 rounded-2xl bg-bharat-green text-white text-xl font-bold disabled:opacity-40 disabled:cursor-not-allowed hover:brightness-105 active:scale-[0.99] transition"
        >
          {submitting ? "Please wait…" : "Get my help →"}
        </button>
      )}

      {errorMsg && (
        <p className="text-base text-red-600 text-center border-2 border-red-200 bg-red-50 rounded-xl p-3">{errorMsg}</p>
      )}

      {caseResp && (
        <div className="rounded-xl border-2 border-green-300 bg-green-50 px-4 py-3 text-base">
          <p className="text-green-800 font-bold mb-1">✅ We have started helping you.</p>
          <p className="text-stone-600 text-sm">Watch the steps on the right. आगे की जानकारी दाहिनी ओर देखें।</p>
          <button
            onClick={() => { setCaseResp(null); setRecState("idle"); setErrorMsg(""); }}
            className="mt-2 text-bharat-saffron underline text-sm"
          >
            Start again
          </button>
        </div>
      )}
    </div>
  );
}
