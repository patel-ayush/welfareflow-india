"use client";

import { useState, useCallback, useEffect } from "react";
import AgentMonitor from "@/components/AgentMonitor";
import VoiceOnboarding from "@/components/VoiceOnboarding";
import AffidavitViewer from "@/components/AffidavitViewer";
import type { AffidavitMetadata, CaseStatus } from "@/lib/types";
import { getCaseStatus, revokeConsent, submitDecision, triggerSlaWatchdog, getImpact } from "@/lib/api";
import type { ImpactSummary } from "@/lib/types";
import { friendlyStatus } from "@/lib/friendly";

export default function DashboardPage() {
  const [caseId, setCaseId] = useState<string | null>(null);
  const [trackingToken, setTrackingToken] = useState<string>("");
  const [status, setStatus] = useState<CaseStatus | null>(null);
  const [affidavit, setAffidavit] = useState<AffidavitMetadata | null>(null);
  const [showHelperTools, setShowHelperTools] = useState(false);
  const [adminMsg, setAdminMsg] = useState<string>("");
  const [revokeMsg, setRevokeMsg] = useState<string>("");
  const [hitlMsg, setHitlMsg] = useState<string>("");
  const [hitlLoading, setHitlLoading] = useState(false);
  const [impact, setImpact] = useState<ImpactSummary | null>(null);

  const handleCaseReady = useCallback((id: string, token: string) => {
    setCaseId(id);
    setTrackingToken(token);
    setStatus("INITIALISED");
    setAffidavit(null);
    setRevokeMsg("");
    setHitlMsg("");
  }, []);

  const handleStatusChange = useCallback((s: CaseStatus) => setStatus(s), []);
  const handleAffidavit = useCallback((meta: AffidavitMetadata) => setAffidavit(meta), []);

  const handlePollStatus = async () => {
    if (!caseId) return;
    try {
      const resp = await getCaseStatus(caseId);
      setStatus(resp.status);
      setAdminMsg(`Status: ${resp.status} · agent: ${resp.current_agent}`);
    } catch (e) {
      setAdminMsg(String(e));
    }
  };

  const handleRevoke = async () => {
    if (!caseId) return;
    try {
      const resp = await revokeConsent(caseId, trackingToken || undefined);
      setRevokeMsg(resp.message);
      setStatus("REVOKED_BY_USER");
    } catch (e) {
      setRevokeMsg(String(e));
    }
  };

  const handleHitlDecision = async (approve: boolean) => {
    if (!caseId || !trackingToken || hitlLoading) return;
    setHitlLoading(true);
    setHitlMsg("");
    try {
      const resp = await submitDecision(caseId, approve, trackingToken);
      setHitlMsg(resp.message);
      setStatus(resp.new_status as CaseStatus);
    } catch (e) {
      setHitlMsg(String(e));
    } finally {
      setHitlLoading(false);
    }
  };

  const handleSlaRun = async () => {
    try {
      const resp = await triggerSlaWatchdog(0);
      setAdminMsg(`SLA: ${resp.cases_escalated} escalated of ${resp.cases_scanned} scanned`);
    } catch (e) {
      setAdminMsg(String(e));
    }
  };

  // Fetch impact stats on mount (best-effort, non-blocking)
  useEffect(() => {
    getImpact().then(setImpact).catch(() => {/* ignore */});
  }, []);

  const fs = caseId
    ? friendlyStatus(status)
    : { en: "Welcome — let's get your benefits", hi: "स्वागत है — आइए आपके लाभ दिलाएँ", tone: "neutral" as const, icon: "🙏" };
  const bannerTone =
    fs.tone === "good"
      ? "bg-green-50 border-green-300 text-green-800"
      : fs.tone === "warn"
      ? "bg-amber-50 border-amber-300 text-amber-800"
      : fs.tone === "bad"
      ? "bg-red-50 border-red-300 text-red-700"
      : "bg-white border-stone-200 text-stone-800";

  return (
    <main className="min-h-screen">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <header className="border-b border-stone-200 bg-white px-5 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <span className="inline-flex flex-col gap-0.5">
            <span className="w-6 h-1.5 rounded-full bg-bharat-saffron" />
            <span className="w-6 h-1.5 rounded-full bg-stone-300" />
            <span className="w-6 h-1.5 rounded-full bg-bharat-green" />
          </span>
          <div>
            <p className="font-bold text-xl tracking-tight text-stone-900">
              Welfare<span className="text-bharat-saffron">Flow</span> <span className="text-bharat-green">India</span>
            </p>
            <p className="text-sm text-stone-500 -mt-0.5">Free help to apply for government schemes · सरकारी योजनाओं के लिए मुफ़्त मदद</p>
          </div>
        </div>
      </header>

      <div className="max-w-6xl mx-auto p-4 sm:p-6 grid grid-cols-1 lg:grid-cols-[minmax(0,420px)_1fr] gap-6">
        {/* ── Left: the form ──────────────────────────────────────────────── */}
        <section className="rounded-3xl border border-stone-200 bg-white/60 p-5">
          <h1 className="text-lg font-bold text-stone-800 mb-4">Let&apos;s apply for your benefits</h1>
          <VoiceOnboarding onCaseReady={handleCaseReady} />
        </section>

        {/* ── Right: status + steps + affidavit ───────────────────────────── */}
        <section className="flex flex-col gap-4">
          {/* Big plain-language status banner */}
          <div className={`rounded-3xl border-2 ${bannerTone} px-5 py-4 flex items-center gap-4`}>
            <span className="text-4xl shrink-0">{fs.icon}</span>
            <div>
              <p className="text-xl font-bold">{fs.en}</p>
              <p className="text-base opacity-80">{fs.hi}</p>
            </div>
          </div>

          {/* Steps + activity feed */}
          <div className="rounded-3xl border border-stone-200 bg-stone-50 min-h-[420px] flex flex-col">
            <AgentMonitor caseId={caseId} onAffidavit={handleAffidavit} onStatusChange={handleStatusChange} />
          </div>

          {/* HITL approval panel — shown when the pipeline is awaiting human confirmation */}
          {status === "AWAITING_APPROVAL" && (
            <div className="rounded-3xl border-2 border-amber-300 bg-amber-50 px-5 py-4">
              <p className="text-lg font-bold text-amber-800 mb-1">🔔 Ready to send — please confirm</p>
              <p className="text-base text-amber-700 mb-3">
                The system has prepared your application. A volunteer or you can review and approve it, or reject to cancel.
                <span className="block text-sm text-amber-600 mt-0.5">आवेदन तैयार है। समीक्षा करें और पुष्टि करें।</span>
              </p>
              <div className="flex gap-3">
                <button
                  onClick={() => handleHitlDecision(true)}
                  disabled={hitlLoading}
                  className="flex-1 py-3 rounded-2xl bg-bharat-green text-white font-bold text-lg disabled:opacity-50 hover:brightness-105 active:scale-[0.99] transition"
                >
                  {hitlLoading ? "Please wait…" : "✅ Yes, send it"}
                </button>
                <button
                  onClick={() => handleHitlDecision(false)}
                  disabled={hitlLoading}
                  className="flex-1 py-3 rounded-2xl bg-red-50 border-2 border-red-300 text-red-700 font-bold text-lg disabled:opacity-50 hover:bg-red-100 active:scale-[0.99] transition"
                >
                  ❌ No, cancel
                </button>
              </div>
              {hitlMsg && <p className="mt-2 text-sm text-amber-700 font-mono">{hitlMsg}</p>}
            </div>
          )}

          {/* Affidavit (only when there is a name mismatch) */}
          {affidavit && affidavit.has_mismatch && (
            <div className="rounded-3xl border border-stone-200 bg-white p-5">
              <AffidavitViewer meta={affidavit} />
            </div>
          )}

          {/* Helper tools (volunteer / QA) — hidden by default */}
          <div>
            <button
              onClick={() => setShowHelperTools((s) => !s)}
              className="text-sm text-stone-500 underline hover:text-stone-700"
            >
              {showHelperTools ? "Hide helper tools" : "Helper tools (for volunteer / tester)"}
            </button>
            {showHelperTools && (
              <div className="mt-2 rounded-2xl border border-stone-200 bg-white p-4 flex flex-col gap-3">
                <div className="flex flex-wrap gap-2">
                  <button onClick={handlePollStatus} disabled={!caseId} className="px-3 py-2 text-sm rounded-lg bg-blue-50 border border-blue-200 text-blue-700 disabled:opacity-40">
                    Check status now
                  </button>
                  <button onClick={handleSlaRun} className="px-3 py-2 text-sm rounded-lg bg-purple-50 border border-purple-200 text-purple-700">
                    Run delay check (SLA)
                  </button>
                  {caseId && status !== "REVOKED_BY_USER" && (
                    <button onClick={handleRevoke} className="px-3 py-2 text-sm rounded-lg bg-orange-50 border border-orange-200 text-orange-700">
                      Delete my data (DPDP)
                    </button>
                  )}
                </div>
                {adminMsg && <p className="text-sm text-stone-500 font-mono">{adminMsg}</p>}
                {revokeMsg && <p className="text-sm text-orange-700">{revokeMsg}</p>}
                {caseId && <p className="text-xs text-stone-400 font-mono">case: {caseId}</p>}
              </div>
            )}
          </div>

          {/* Impact dashboard — live aggregate stats */}
          {impact && (
            <div className="rounded-3xl border border-stone-200 bg-white px-5 py-4">
              <p className="text-sm font-bold text-stone-500 mb-3 uppercase tracking-wide">Our impact · हमारा प्रभाव</p>
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <div className="rounded-2xl bg-bharat-saffron/10 border border-bharat-saffron/30 px-3 py-3 text-center">
                  <p className="text-2xl font-bold text-bharat-saffron">{impact.total_cases}</p>
                  <p className="text-xs text-stone-600 mt-0.5">Families helped</p>
                </div>
                <div className="rounded-2xl bg-green-50 border border-green-200 px-3 py-3 text-center">
                  <p className="text-2xl font-bold text-bharat-green">{impact.applications_submitted}</p>
                  <p className="text-xs text-stone-600 mt-0.5">Applications sent</p>
                </div>
                <div className="rounded-2xl bg-amber-50 border border-amber-200 px-3 py-3 text-center">
                  <p className="text-2xl font-bold text-amber-700">{impact.mismatches_caught_before_rejection}</p>
                  <p className="text-xs text-stone-600 mt-0.5">Rejections prevented</p>
                </div>
                <div className="rounded-2xl bg-blue-50 border border-blue-200 px-3 py-3 text-center">
                  <p className="text-xl font-bold text-blue-700">
                    ₹{((impact.pmkisan_income_unlocked_inr + impact.health_cover_unlocked_inr) / 100000).toFixed(1)}L
                  </p>
                  <p className="text-xs text-stone-600 mt-0.5">Benefits unlocked</p>
                </div>
              </div>
            </div>
          )}
        </section>
      </div>
    </main>
  );
}
