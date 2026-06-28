# WelfareFlow India — Handoff Notes

Tasks that require your credentials / action before the demo is production-ready.

---

## G1 — Real UiPath Round-Trip

**What's needed:** Live UiPath Orchestrator tenant + a deployed `WelfareSchemeSubmission` Maestro process.

**Steps:**
1. Log in to cloud.uipath.com and create a new process (or import the skeleton from `uipath_maestro.py`).
2. In `.env` set:
   ```
   UIPATH_ORCHESTRATOR_URL=https://cloud.uipath.com/<your-org>/<tenant>
   UI_PATH_APP_ID=<OAuth2 client id>
   UIPATH_APP_SECRET=<OAuth2 secret>
   UIPATH_TENANT_NAME=<tenant name>
   UIPATH_QUEUE_NAME=WelfareFlow_Submissions
   UIPATH_PROCESS_NAME=WelfareSchemeSubmission
   UIPATH_WEBHOOK_SECRET=<random 32-byte hex>
   ```
3. Restart the backend. The mock fallback activates automatically when these vars are absent.

---

## G3 — Real Sarvam Voice (STT / TTS)

**What's needed:** A Sarvam AI API key with credits for Saaras V2 (STT) and Bulbul V3 (TTS).

**Steps:**
1. Sign up at console.sarvam.ai → generate an API key.
2. In `.env` set:
   ```
   SARVAM_API_KEY=<your-key>
   SARVAM_MOCK_MODE=false
   ```
3. The voice onboarding mic recording path will now call real STT, and TTS audio will play in the browser.

---

## Demo Video

Target: **< 5 minutes** for Devpost submission.

Suggested flow:
1. Open app → select CITIZEN-001 (Ramesh Kumar — name mismatch case).
2. Press "Get my help →" — watch the 5 steps animate in real-time.
3. Show the amber warning banner + Name Mismatch Affidavit.
4. Select CITIZEN-003 (Suresh Prasad — all names match) — show the green "Application sent" result.
5. Briefly show: LangSmith trace, UiPath Orchestrator queue item (or mock label), DPDP consent revoke.

---

## Devpost Submission

1. Go to https://uipath-agenthack.devpost.com
2. Add project — include:
   - GitHub repo URL (make public)
   - Demo video link
   - Architecture diagram (from README or Miro board)
3. Fill out the $1,500 feedback form linked in the hackathon rules.

---

## Production Hardening (post-hackathon)

- Replace SQLite with PostgreSQL: set `DATABASE_URL=postgresql+asyncpg://...` in `.env`.
- Set `AADHAAR_VAULT_AES_KEY` to a real Fernet key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- Set `ADMIN_API_KEY` to a 32-byte random hex: `openssl rand -hex 32`.
- Set `APP_ENV=production` — this activates hard-fail guards for missing secrets.
- Deploy behind HTTPS with a reverse proxy (Nginx / Caddy).
