"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import type { AgentStreamFrame, AffidavitMetadata, CaseStatus } from "@/lib/types";
import { getCaseStreamUrl } from "@/lib/api";
import { JOURNEY, stepIndexForAgent, friendlyLine } from "@/lib/friendly";

// The backend sends NAMED SSE events (event: agent_start, agent_log, …). Named
// events are NOT delivered to EventSource.onmessage — they must be subscribed to
// by name. This was the bug that left the monitor permanently blank.
const EVENT_NAMES = [
  "agent_start",
  "agent_log",
  "agent_result",
  "agent_complete",
  "anomaly_detected",
  "error",
  "stream_end",
];

type StepState = "pending" | "active" | "done";

interface FeedLine {
  id: number;
  ts: string;
  text: string;
  tone: "info" | "good" | "warn" | "bad";
}

interface Props {
  caseId: string | null;
  onAffidavit?: (meta: AffidavitMetadata) => void;
  onStatusChange?: (status: CaseStatus) => void;
}

const MAX_RECONNECT_DELAY_MS = 30_000;
const BACKOFF_BASE_MS = 1_000;

export default function AgentMonitor({ caseId, onAffidavit, onStatusChange }: Props) {
  const [feed, setFeed] = useState<FeedLine[]>([]);
  const [tech, setTech] = useState<string[]>([]);
  const [showTech, setShowTech] = useState(false);
  const [connected, setConnected] = useState(false);
  const [stepStates, setStepStates] = useState<StepState[]>(() => JOURNEY.map(() => "pending"));

  const bottomRef = useRef<HTMLDivElement>(null);
  const esRef = useRef<EventSource | null>(null);
  const lineIdRef = useRef(0);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const doneRef = useRef(false);

  const pushFeed = useCallback((text: string, tone: FeedLine["tone"]) => {
    setFeed((prev) => [...prev.slice(-199), { id: ++lineIdRef.current, ts: now(), text, tone }]);
  }, []);

  const advanceSteps = useCallback((agent: string, terminal: boolean) => {
    const idx = stepIndexForAgent(agent);
    if (idx < 0) return;
    setStepStates((prev) => {
      const next = [...prev];
      for (let i = 0; i < next.length; i++) {
        if (i < idx) next[i] = "done";
        else if (i === idx) next[i] = terminal ? "done" : "active";
      }
      return next;
    });
  }, []);

  const handleFrame = useCallback(
    (frame: AgentStreamFrame) => {
      // Always keep the raw frame for the technical (QA / judge) view.
      setTech((prev) => [
        ...prev.slice(-299),
        `${now()}  [${frame.event_type}] ${frame.agent_name} ${frame.status ? "→ " + frame.status : ""} ${JSON.stringify(frame.data)}`,
      ]);

      // Status can ride on any frame.
      if (frame.status) onStatusChange?.(frame.status as CaseStatus);

      // Progress tracker.
      const terminalEvent = frame.event_type === "agent_result" || frame.event_type === "agent_complete";
      advanceSteps(frame.agent_name, terminalEvent);

      // Affidavit lives under data.affidavit (was previously looked for at top level).
      const aff = (frame.data?.affidavit ?? null) as AffidavitMetadata | null;
      if (aff && typeof aff === "object" && "has_mismatch" in aff) onAffidavit?.(aff);

      // Plain-language feed line.
      const line = friendlyLine(frame);
      if (line) pushFeed(line.text, line.tone);

      if (frame.event_type === "stream_end") {
        doneRef.current = true;
        esRef.current?.close();
        setConnected(false);
      }
    },
    [advanceSteps, onAffidavit, onStatusChange, pushFeed],
  );

  const connect = useCallback(
    (id: string) => {
      esRef.current?.close();
      const es = new EventSource(getCaseStreamUrl(id));
      esRef.current = es;

      es.onopen = () => {
        setConnected(true);
        retryCountRef.current = 0;
      };

      const onAny = (ev: MessageEvent) => {
        try {
          const frame = JSON.parse(ev.data as string) as AgentStreamFrame;
          if (frame && typeof frame === "object" && "event_type" in frame) handleFrame(frame);
        } catch {
          /* ignore keepalive / malformed */
        }
      };

      // Subscribe to every named event type the backend emits, plus the default.
      EVENT_NAMES.forEach((name) => es.addEventListener(name, onAny as EventListener));
      es.onmessage = onAny;

      es.onerror = () => {
        es.close();
        setConnected(false);
        if (doneRef.current) return;
        const attempt = ++retryCountRef.current;
        const delay = Math.min(BACKOFF_BASE_MS * 2 ** (attempt - 1), MAX_RECONNECT_DELAY_MS);
        retryTimerRef.current = setTimeout(() => connect(id), delay);
      };
    },
    [handleFrame],
  );

  useEffect(() => {
    if (!caseId) return;
    setFeed([]);
    setTech([]);
    setStepStates(JOURNEY.map(() => "pending"));
    setConnected(false);
    doneRef.current = false;
    retryCountRef.current = 0;
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    connect(caseId);
    return () => {
      esRef.current?.close();
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    };
  }, [caseId, connect]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [feed]);

  if (!caseId) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center text-stone-400 gap-2 p-8">
        <span className="text-4xl">📋</span>
        <p className="text-base">Fill the form on the left and press the big button to begin.</p>
        <p className="text-sm text-stone-400">बायीं ओर फ़ॉर्म भरें और बड़ा बटन दबाएँ।</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full gap-4 p-4 sm:p-6">
      {/* ── Step tracker ─────────────────────────────────────────────────── */}
      <ol className="flex flex-col gap-2">
        {JOURNEY.map((step, i) => {
          const st = stepStates[i];
          return (
            <li
              key={step.key}
              className={`flex items-center gap-3 rounded-2xl border px-4 py-3 transition-colors ${
                st === "done"
                  ? "border-green-300 bg-green-50"
                  : st === "active"
                  ? "border-bharat-saffron bg-orange-50"
                  : "border-stone-200 bg-white"
              }`}
            >
              <span className="text-2xl shrink-0">{st === "done" ? "✅" : step.icon}</span>
              <div className="flex-1 min-w-0">
                <p className={`text-base font-semibold ${st === "pending" ? "text-stone-400" : "text-stone-800"}`}>
                  {step.title_en}
                </p>
                <p className={`text-sm ${st === "pending" ? "text-stone-300" : "text-stone-500"}`}>{step.title_hi}</p>
              </div>
              {st === "active" && (
                <span className="shrink-0 inline-block w-3 h-3 rounded-full bg-bharat-saffron animate-pulse" />
              )}
            </li>
          );
        })}
      </ol>

      {/* ── Plain-language activity feed ─────────────────────────────────── */}
      <div className="flex-1 min-h-[120px] overflow-y-auto glass-scroll rounded-2xl border border-stone-200 bg-white p-4 space-y-2">
        <div className="flex items-center justify-between">
          <p className="text-sm font-semibold text-stone-500">What is happening · क्या हो रहा है</p>
          <span className={`inline-block w-2.5 h-2.5 rounded-full ${connected ? "bg-green-500 animate-pulse" : "bg-stone-300"}`} title={connected ? "Live" : "Not connected"} />
        </div>
        {feed.length === 0 && <p className="text-sm text-stone-400 italic">Starting… please wait.</p>}
        {feed.map((line) => (
          <div
            key={line.id}
            className={`text-base leading-relaxed rounded-xl px-3 py-2 ${
              line.tone === "good"
                ? "bg-green-50 text-green-800"
                : line.tone === "warn"
                ? "bg-amber-50 text-amber-800"
                : line.tone === "bad"
                ? "bg-red-50 text-red-700"
                : "bg-stone-50 text-stone-700"
            }`}
          >
            {line.text}
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      {/* ── Technical details (for QA / judges) ──────────────────────────── */}
      <div className="shrink-0">
        <button
          onClick={() => setShowTech((s) => !s)}
          className="text-sm text-stone-500 underline hover:text-stone-700"
        >
          {showTech ? "Hide technical details" : "Show technical details (for testers)"}
        </button>
        {showTech && (
          <pre className="mt-2 max-h-48 overflow-auto glass-scroll rounded-xl bg-stone-900 text-green-300 text-xs p-3 font-mono whitespace-pre-wrap">
            {tech.length ? tech.join("\n") : "No frames yet."}
          </pre>
        )}
      </div>
    </div>
  );
}

function now(): string {
  return new Date().toLocaleTimeString();
}
