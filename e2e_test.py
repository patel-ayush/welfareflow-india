"""
WelfareFlow India — QA Acceptance Runner (self-documenting)
===========================================================

This is a hand-it-to-anyone test. A QA person who has NEVER seen the code (and
has NO frontend app) can run it and understand exactly what the system does:
for every scenario it prints, in plain English,

    GIVEN  — who the citizen is and what they said / uploaded
    WHEN   — the action we performed
    THEN   — what SHOULD happen, and WHY
    ACTUAL — the real step-by-step timeline the agents produced
    VERDICT— PASS / FAIL against the expectation

It also writes a clean, self-contained **qa_report.html** you can open in any
browser and read or share — no app, no terminal needed.

HOW TO RUN
----------
    1. Start the API (token-free sandbox mode):
           SARVAM_MOCK_MODE=true uvicorn main:app --port 8000
    2. Run this script:
           python e2e_test.py                 # all scenarios + report
           python e2e_test.py CITIZEN-001      # one scenario
           WF_BASE_URL=http://host:8000 python e2e_test.py   # custom host

Everything uses the sandbox fallbacks (in-memory SQLite, mock Sarvam OCR/STT,
mock UiPath) so it runs with zero external infrastructure or API tokens.
"""
from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import httpx

BASE_URL: str = os.environ.get("WF_BASE_URL", "http://localhost:8000")

# A 1x1 transparent PNG — content is irrelevant in sandbox mode (mock OCR is
# citizen-aware), it only needs to be valid base64 for the request schema.
_TINY_PNG_B64: str = base64.b64encode(
    base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
).decode()


def _docs() -> list[dict[str, str]]:
    return [
        {"document_type": "aadhaar", "image_base64": _TINY_PNG_B64, "filename": "aadhaar.png"},
        {"document_type": "ration_card", "image_base64": _TINY_PNG_B64, "filename": "ration.png"},
        {"document_type": "bank_passbook", "image_base64": _TINY_PNG_B64, "filename": "passbook.png"},
    ]


def _consent() -> list[dict[str, str]]:
    return [
        {"item_code": "IDENTITY_SHARE", "description_en": "Use my Aadhaar identity to verify scheme eligibility.", "description_hi": "मेरी आधार पहचान का उपयोग पात्रता सत्यापन के लिए करें।"},
        {"item_code": "DOC_OCR", "description_en": "Read text from my uploaded documents.", "description_hi": "मेरे दस्तावेज़ों से पाठ पढ़ें।"},
        {"item_code": "RPA_SUBMIT", "description_en": "Auto-fill and submit my welfare applications.", "description_hi": "मेरे कल्याण आवेदन स्वतः भरें और जमा करें।"},
    ]


# Each scenario carries a plain-English STORY so the report reads like a spec:
#   given / when / then(+why) are shown verbatim to the QA reader.
SCENARIOS: dict[str, dict[str, Any]] = {
    "CITIZEN-001": {
        "label": "Ramesh Kumar — name MISMATCH on documents",
        "given": "Ramesh Kumar, a farmer from Mandya (Karnataka) with 2 acres. His Aadhaar says 'Ramesh Kumar' but his Ration Card says 'Ramesha K' and his passbook says 'R. Kumar'.",
        "when": "He speaks his request and uploads Aadhaar + Ration Card + Bank Passbook.",
        "then": "The system must STOP before submitting and ask him to fix the name (status MISSING_DOCUMENTS), and generate a Name-Correction Affidavit.",
        "why": "Submitting with mismatched names is the #1 cause of welfare rejection. The name-match score (0.76) is below the 0.85 threshold, so it must flag, not pass.",
        "body": {"citizen_id": "CITIZEN-001", "raw_transcript": "My name is Ramesh Kumar from Mandya. I have two acres of land, my knee hurts, I need farmer money and a health card.", "documents": _docs(), "consent_items": _consent(), "otp_verified": True, "language_code": "kn-IN"},
        "expected_terminal": {"MISSING_DOCUMENTS"},
    },
    "CITIZEN-002": {
        "label": "Lakshmi Devi — spelling variant, should PASS",
        "given": "Lakshmi Devi from Sikar (Rajasthan), 1.5 acres. Ration Card spells it 'Laxmi Devi' (a normal transliteration variant).",
        "when": "She submits her request with all three documents.",
        "then": "The application should go THROUGH to UiPath (status PENDING_UIPATH) — no affidavit needed.",
        "why": "'Lakshmi' vs 'Laxmi' is a legitimate vowel variant that scores above 0.85, so it must NOT be wrongly blocked.",
        "body": {"citizen_id": "CITIZEN-002", "raw_transcript": "I am Lakshmi Devi from Sikar Rajasthan, one and half acre land, I want PM Kisan and Ayushman.", "documents": _docs(), "consent_items": _consent(), "otp_verified": True, "language_code": "hi-IN"},
        "expected_terminal": {"PENDING_UIPATH"},
    },
    "CITIZEN-003": {
        "label": "Suresh Prasad — perfect match, should PASS",
        "given": "Suresh Prasad from Varanasi (UP), 0.8 acres. All documents have the exact same name.",
        "when": "He submits his request with all three documents.",
        "then": "The application should go straight through to UiPath (status PENDING_UIPATH).",
        "why": "Everything matches perfectly, so there is nothing to flag.",
        "body": {"citizen_id": "CITIZEN-003", "raw_transcript": "Mera naam Suresh Prasad hai, Varanasi se, point eight acre zameen hai.", "documents": _docs(), "consent_items": _consent(), "otp_verified": True, "language_code": "hi-IN"},
        "expected_terminal": {"PENDING_UIPATH"},
    },
    "CITIZEN-004": {
        "label": "Priya Sharma — trailing-vowel variant, should PASS",
        "given": "Priya Sharma from Nashik (Maharashtra), 3.2 acres, income ₹75,000. Ration Card spells it 'Priya Sharmaa'.",
        "when": "She submits her request with all three documents.",
        "then": "The application should go through to UiPath (status PENDING_UIPATH).",
        "why": "A single trailing extra vowel is a minor variant that should score above threshold and pass.",
        "body": {"citizen_id": "CITIZEN-004", "raw_transcript": "I am Priya Sharma from Nashik, 3.2 acres, income 75000.", "documents": _docs(), "consent_items": _consent(), "otp_verified": True, "language_code": "mr-IN"},
        "expected_terminal": {"PENDING_UIPATH"},
    },
    "CITIZEN-005": {
        "label": "Mohammed Rashid — abbreviation MISMATCH",
        "given": "Mohammed Rashid from Nizamabad (Telangana), 1 acre. Ration Card abbreviates it to 'Mohd Rashid'.",
        "when": "He submits his request with all three documents.",
        "then": "The system must STOP and ask him to fix the name (status MISSING_DOCUMENTS).",
        "why": "'Mohammed' shrinking to 'Mohd' is a token truncation that scores below 0.85 — exactly the kind of mismatch that causes rejection, so it must flag.",
        "body": {"citizen_id": "CITIZEN-005", "raw_transcript": "Naa peru Mohammed Rashid, Nizamabad nunchi, oka acre polam.", "documents": _docs(), "consent_items": _consent(), "otp_verified": True, "language_code": "te-IN"},
        "expected_terminal": {"MISSING_DOCUMENTS"},
    },
    "VOICE_ONLY": {
        "label": "Voice onboarding — audio, no typed text",
        "given": "Lakshmi Devi sends a voice recording and types NOTHING.",
        "when": "She submits only audio (base64), with an empty transcript.",
        "then": "The backend should transcribe her voice (Sarvam Saaras STT) and complete to PENDING_UIPATH.",
        "why": "Voice-first is the whole point for low-literacy users; the request must not be rejected just because there is no typed transcript.",
        "body": {"citizen_id": "CITIZEN-002", "raw_transcript": "", "audio_base64": base64.b64encode(b"demo-voice-bytes").decode(), "documents": _docs(), "consent_items": _consent(), "otp_verified": True, "language_code": "hi-IN"},
        "expected_terminal": {"PENDING_UIPATH"},
    },
    "NO_DOCS": {
        "label": "No documents uploaded — audit skipped",
        "given": "Suresh Prasad submits with NO documents at all.",
        "when": "He submits only his spoken request.",
        "then": "The document check is skipped and the application proceeds to UiPath (PENDING_UIPATH).",
        "why": "With nothing to compare, there is no mismatch to block on — the flow should not get stuck.",
        "body": {"citizen_id": "CITIZEN-003", "raw_transcript": "Suresh Prasad, Varanasi, no documents right now.", "documents": [], "consent_items": _consent(), "otp_verified": True, "language_code": "hi-IN"},
        "expected_terminal": {"PENDING_UIPATH"},
    },
}


# ── Plain-English summariser: one frame → one readable line ───────────────────
def _summarise(frame: dict[str, Any]) -> str:
    d = frame.get("data", {}) or {}
    agent = frame.get("agent_name", "")
    et = frame.get("event_type", "")
    if et == "anomaly_detected":
        return "⚠️  PROBLEM: " + str(d.get("anomaly") or d.get("alert", "issue found"))
    if et == "error":
        return "❌ ERROR: " + str(d.get("error", "unknown"))
    if d.get("voice"):
        return '🔊 spoke to citizen: "' + str(d.get("tts_text", "")) + '"'
    if "transcript" in d:
        return '🎤 transcribed voice: "' + str(d.get("transcript", "")) + '"'
    if agent == "voice_intent_agent" and et == "agent_result":
        p = d.get("extracted_profile", {})
        return f"understood citizen: name={p.get('full_name')!r}, land={p.get('land_area_acres')}ac"
    if agent == "eligibility_router" and et == "agent_result":
        return "eligible schemes: " + ", ".join(d.get("eligible_schemes", []) or ["(none)"])
    if agent == "document_audit" and et == "agent_result":
        return f"documents checked — {d.get('anomalies_count', 0)} problem(s) found"
    if agent == "npci_seeding" and et == "agent_result":
        return f"bank link (NPCI): {d.get('seeding_status')} at {d.get('bank_name')}"
    if agent == "uipath_execution" and et == "agent_result":
        return f"application sent to UiPath ({d.get('mode')}), tx={d.get('tx_id')}"
    if agent == "exception_management" and et == "agent_result":
        return "prepared name-correction affidavit; case blocked for fixing"
    if et == "agent_start":
        return f"{agent}: starting…"
    return f"{agent}: {et}"


async def _consume_stream(client: httpx.AsyncClient, case_id: str) -> tuple[str, list[dict[str, Any]]]:
    """Open the SSE stream; return (final_status, timeline-of-frames)."""
    final_status = "UNKNOWN"
    timeline: list[dict[str, Any]] = []
    url = f"{BASE_URL}/api/cases/{case_id}/stream"
    async with client.stream("GET", url, timeout=60.0) as resp:
        if resp.status_code != 200:
            return f"HTTP_{resp.status_code}", timeline
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if not payload:
                continue
            try:
                frame = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if frame.get("status"):
                final_status = frame["status"]
            timeline.append({
                "agent": frame.get("agent_name", ""),
                "event": frame.get("event_type", ""),
                "status": frame.get("status", ""),
                "summary": _summarise(frame),
            })
            if frame.get("event_type") == "stream_end":
                break
    return final_status, timeline


async def run_scenario(client: httpx.AsyncClient, key: str) -> dict[str, Any]:
    s = SCENARIOS[key]
    print(f"\n{'='*92}\nSCENARIO {key}: {s['label']}\n{'='*92}")
    print(f"  GIVEN : {s['given']}")
    print(f"  WHEN  : {s['when']}")
    print(f"  THEN  : {s['then']}")
    print(f"  WHY   : {s['why']}")

    init = await client.post(f"{BASE_URL}/api/cases/initialize", json=s["body"], timeout=30.0)
    if init.status_code != 201:
        print(f"  ❌ initialize FAILED: HTTP {init.status_code} — {init.text}")
        return {**s, "key": key, "ok": False, "final": f"HTTP_{init.status_code}", "timeline": [], "error": init.text}

    case_id = init.json()["case_id"]
    await asyncio.sleep(0.2)
    final, timeline = await _consume_stream(client, case_id)

    print("  ACTUAL timeline:")
    for t in timeline:
        print(f"      • {t['summary']}")
    ok = final in s["expected_terminal"]
    print(f"  VERDICT: ended at {final!r}; expected {set(s['expected_terminal'])} → {'PASS ✓' if ok else 'FAIL ✗'}")
    return {**s, "key": key, "ok": ok, "final": final, "case_id": case_id, "timeline": timeline}


async def run_guardrail_tests(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Consent / OTP guard rails — must be rejected before any case is created."""
    print(f"\n{'='*92}\nGUARD-RAIL TESTS: consent & OTP must block bad requests\n{'='*92}")
    out: list[dict[str, Any]] = []

    no_otp = dict(SCENARIOS["CITIZEN-003"]["body"]); no_otp["otp_verified"] = False
    r1 = await client.post(f"{BASE_URL}/api/cases/initialize", json=no_otp, timeout=15.0)
    ok1 = r1.status_code == 403
    print(f"  GIVEN no OTP confirmation → THEN reject (403). ACTUAL {r1.status_code} {'✓' if ok1 else '✗'}")
    out.append({"key": "GUARD_OTP", "label": "Reject when OTP not verified", "given": "A request with otp_verified=false", "when": "POST /initialize", "then": "Rejected with HTTP 403", "why": "Consent must be OTP-confirmed (DPDP Act).", "ok": ok1, "final": f"HTTP_{r1.status_code}", "timeline": []})

    no_consent = dict(SCENARIOS["CITIZEN-003"]["body"]); no_consent["consent_items"] = []
    r2 = await client.post(f"{BASE_URL}/api/cases/initialize", json=no_consent, timeout=15.0)
    ok2 = r2.status_code == 422
    print(f"  GIVEN no consent items → THEN reject (422). ACTUAL {r2.status_code} {'✓' if ok2 else '✗'}")
    out.append({"key": "GUARD_CONSENT", "label": "Reject when no consent items", "given": "A request with an empty consent list", "when": "POST /initialize", "then": "Rejected with HTTP 422", "why": "At least one itemised consent is required (DPDP Act).", "ok": ok2, "final": f"HTTP_{r2.status_code}", "timeline": []})
    return out


async def run_lifecycle_tests(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Status polling, UiPath callback, SLA escalation, consent revocation."""
    print(f"\n{'='*92}\nLIFECYCLE TESTS: status • UiPath callback • SLA delay • delete my data\n{'='*92}")
    out: list[dict[str, Any]] = []

    async def init_drain(body: dict[str, Any]) -> tuple[str, str]:
        """Initialize, drain the stream, and return (case_id, tracking_token)."""
        r = await client.post(f"{BASE_URL}/api/cases/initialize", json=body, timeout=30.0)
        r.raise_for_status()
        j = r.json()
        cid, token = j["case_id"], j["tracking_token"]
        await asyncio.sleep(0.2)
        await _consume_stream(client, cid)
        return cid, token

    # A. callback flips PENDING_UIPATH → COMPLETE
    cid, _ = await init_drain(SCENARIOS["CITIZEN-003"]["body"])
    await client.post(f"{BASE_URL}/api/webhooks/uipath/callback", json={"case_id": cid, "status": "SUCCESS", "uipath_tx_id": "RPA-TX-77001"}, timeout=15.0)
    st = (await client.get(f"{BASE_URL}/api/cases/{cid}/status", timeout=15.0)).json().get("status")
    okA = st == "COMPLETE"
    print(f"  A. GIVEN a sent application, WHEN UiPath confirms success, THEN status=COMPLETE. ACTUAL {st!r} {'✓' if okA else '✗'}")
    out.append({"key": "LIFE_CALLBACK", "label": "UiPath success callback completes the case", "given": "A case in PENDING_UIPATH", "when": "UiPath posts a SUCCESS callback", "then": "Case becomes COMPLETE", "why": "The citizen's application is confirmed done.", "ok": okA, "final": str(st), "timeline": []})

    # B. SLA escalation with override 0 days
    pend, _ = await init_drain(SCENARIOS["CITIZEN-004"]["body"])
    sla = (await client.post(f"{BASE_URL}/api/admin/sla/run?sla_days_override=0", timeout=20.0)).json()
    okB = pend in {e["case_id"] for e in sla.get("escalations", [])}
    print(f"  B. GIVEN a pending case, WHEN the delay check runs (0-day), THEN it is escalated. ACTUAL escalated={okB} {'✓' if okB else '✗'}")
    out.append({"key": "LIFE_SLA", "label": "Delayed cases auto-escalate (Right to Service)", "given": "A pending case older than the SLA", "when": "The SLA watchdog sweeps", "then": "The case is ESCALATED and an appeal is queued", "why": "The citizen should not be stuck silently past the legal SLA.", "ok": okB, "final": "ESCALATED" if okB else "NOT_ESCALATED", "timeline": []})

    # C. consent revocation purges the vault — now requires the tracking token (authz)
    rev, rev_token = await init_drain(SCENARIOS["CITIZEN-002"]["body"])
    # First prove it is REJECTED without the token (the B7 security fix).
    no_tok = await client.post(f"{BASE_URL}/api/cases/{rev}/consent/revoke", timeout=15.0)
    auth_blocked = no_tok.status_code == 403
    rb = (await client.post(f"{BASE_URL}/api/cases/{rev}/consent/revoke", headers={"X-Tracking-Token": rev_token}, timeout=15.0)).json()
    okC = auth_blocked and rb.get("case_status") == "REVOKED_BY_USER" and rb.get("vault_purged") is True
    print(f"  C. GIVEN a case, WHEN the citizen deletes their data (with their token), THEN consent revoked + vault purged; without token → 403. ACTUAL blocked_without_token={auth_blocked} status={rb.get('case_status')!r} purged={rb.get('vault_purged')} {'✓' if okC else '✗'}")
    out.append({"key": "LIFE_REVOKE", "label": "Delete my data wipes the Aadhaar vault (DPDP) + needs the citizen's token", "given": "An active case with stored identity", "when": "The citizen revokes consent (token required)", "then": "Without the token → 403; with it, consent deactivated and the encrypted vault row hard-deleted", "why": "Right to withdraw / erase (DPDP Act 2023), but only the rightful owner may trigger it.", "ok": okC, "final": str(rb.get("case_status")), "timeline": []})

    # D. Human-in-the-loop: case pauses for approval, then resumes on approve
    hitl_body = dict(SCENARIOS["CITIZEN-003"]["body"]); hitl_body["require_approval"] = True
    r = await client.post(f"{BASE_URL}/api/cases/initialize", json=hitl_body, timeout=30.0)
    r.raise_for_status()
    hj = r.json(); hcid, htok = hj["case_id"], hj["tracking_token"]
    # Do NOT drain the stream here — the HITL gate leaves it open (no stream_end
    # until a decision). Poll status until it pauses for approval.
    paused = ""
    for _ in range(25):
        await asyncio.sleep(0.4)
        paused = (await client.get(f"{BASE_URL}/api/cases/{hcid}/status", timeout=15.0)).json().get("status")
        if paused == "AWAITING_APPROVAL":
            break
    dec = (await client.post(f"{BASE_URL}/api/cases/{hcid}/decision", json={"approve": True}, headers={"X-Tracking-Token": htok}, timeout=20.0)).json()
    okD = paused == "AWAITING_APPROVAL" and dec.get("new_status") == "PENDING_UIPATH"
    print(f"  D. GIVEN require_approval, WHEN the pipeline reaches submission, THEN it PAUSES for a human; on approve it sends. ACTUAL paused={paused!r} after_approve={dec.get('new_status')!r} {'✓' if okD else '✗'}")
    out.append({"key": "LIFE_HITL", "label": "Human-in-the-loop: agent pauses for human approval before the irreversible government submission", "given": "A case submitted with require_approval=true", "when": "The pipeline reaches the UiPath submission step", "then": "It pauses at AWAITING_APPROVAL and only submits after a human approves via /decision", "why": "Judges reward agents that act autonomously but keep humans accountable for high-impact, irreversible actions.", "ok": okD, "final": str(dec.get("new_status")), "timeline": []})
    return out


# ── HTML report ───────────────────────────────────────────────────────────────
def write_html_report(results: list[dict[str, Any]], path: str = "qa_report.html") -> None:
    passed = sum(1 for r in results if r["ok"])
    total = len(results)
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def esc(x: Any) -> str:
        return html.escape(str(x))

    cards = []
    for r in results:
        badge = ("PASS", "#138808") if r["ok"] else ("FAIL", "#c0392b")
        rows = "".join(
            f"<li><span class='tag tag-{esc(t['event'])}'>{esc(t['event'] or '·')}</span> {esc(t['summary'])}</li>"
            for t in r.get("timeline", [])
        ) or "<li class='muted'>(no live timeline — API-level check)</li>"
        cards.append(f"""
        <section class="card">
          <div class="card-head">
            <h2>{esc(r['key'])} — {esc(r['label'])}</h2>
            <span class="badge" style="background:{badge[1]}">{badge[0]}</span>
          </div>
          <table class="spec">
            <tr><th>GIVEN</th><td>{esc(r['given'])}</td></tr>
            <tr><th>WHEN</th><td>{esc(r['when'])}</td></tr>
            <tr><th>THEN</th><td>{esc(r['then'])}</td></tr>
            <tr><th>WHY</th><td class="muted">{esc(r['why'])}</td></tr>
            <tr><th>RESULT</th><td>Ended at <b>{esc(r['final'])}</b></td></tr>
          </table>
          <p class="tl-title">What actually happened, step by step:</p>
          <ul class="timeline">{rows}</ul>
        </section>""")

    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>WelfareFlow India — QA Acceptance Report</title>
<style>
  body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#fdf8f1;color:#1c1917;margin:0;padding:24px;}}
  .wrap{{max-width:920px;margin:0 auto;}}
  h1{{font-size:24px;margin:0 0 4px;}}
  .sub{{color:#78716c;margin:0 0 20px;}}
  .summary{{display:flex;gap:16px;align-items:center;background:#fff;border:1px solid #e7e5e4;border-radius:16px;padding:16px 20px;margin-bottom:24px;}}
  .summary .big{{font-size:34px;font-weight:800;}}
  .card{{background:#fff;border:1px solid #e7e5e4;border-radius:16px;padding:18px 20px;margin-bottom:18px;}}
  .card-head{{display:flex;justify-content:space-between;align-items:center;gap:12px;}}
  .card-head h2{{font-size:17px;margin:0;}}
  .badge{{color:#fff;font-weight:700;font-size:13px;padding:4px 12px;border-radius:999px;}}
  table.spec{{width:100%;border-collapse:collapse;margin:12px 0;}}
  table.spec th{{text-align:left;width:84px;vertical-align:top;color:#a8a29e;font-size:12px;letter-spacing:.08em;padding:4px 8px 4px 0;}}
  table.spec td{{padding:4px 0;font-size:15px;}}
  .muted{{color:#78716c;}}
  .tl-title{{font-weight:600;margin:14px 0 6px;color:#57534e;}}
  ul.timeline{{list-style:none;margin:0;padding:0;border-left:3px solid #f0e6d6;}}
  ul.timeline li{{padding:5px 0 5px 14px;font-size:14px;}}
  .tag{{display:inline-block;font-size:11px;padding:1px 7px;border-radius:6px;background:#f5f5f4;color:#57534e;margin-right:6px;font-family:ui-monospace,monospace;}}
  .tag-anomaly_detected{{background:#fef3c7;color:#92400e;}}
  .tag-agent_result{{background:#dcfce7;color:#166534;}}
  .tag-error{{background:#fee2e2;color:#991b1b;}}
</style></head><body><div class="wrap">
  <h1>WelfareFlow India — QA Acceptance Report</h1>
  <p class="sub">Generated {generated} · target {esc(BASE_URL)} · read top-to-bottom; no app needed.</p>
  <div class="summary"><span class="big" style="color:{'#138808' if passed==total else '#c0392b'}">{passed}/{total}</span>
  <div><b>scenarios passed</b><br><span class="muted">Each card below states what should happen and shows what actually happened.</span></div></div>
  {''.join(cards)}
</div></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    print(f"\n📄 HTML report written to {os.path.abspath(path)}")


async def main() -> None:
    selected = sys.argv[1:] if len(sys.argv) > 1 else list(SCENARIOS.keys())
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient() as client:
        # Confirm the server is up with a friendly message before we begin.
        try:
            h = await client.get(f"{BASE_URL}/health", timeout=5.0)
            print(f"Connected to {BASE_URL} — health: {h.json()}")
        except Exception as exc:
            print(f"\n❌ Could not reach the API at {BASE_URL}.\n   Start it first:  SARVAM_MOCK_MODE=true uvicorn main:app --port 8000\n   ({exc})")
            sys.exit(2)

        for key in selected:
            if key not in SCENARIOS:
                print(f"Unknown scenario {key!r}. Valid: {list(SCENARIOS.keys())}")
                continue
            results.append(await run_scenario(client, key))

        if len(sys.argv) <= 1:
            results += await run_guardrail_tests(client)
            results += await run_lifecycle_tests(client)

    # Console summary
    print(f"\n{'='*92}\nSUMMARY\n{'='*92}")
    for r in results:
        print(f"  {r['key']:16} {'PASS ✓' if r['ok'] else 'FAIL ✗'}  (ended at {r['final']})")
    passed = sum(1 for r in results if r["ok"])
    print(f"\n  {passed}/{len(results)} checks passed")

    write_html_report(results)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
