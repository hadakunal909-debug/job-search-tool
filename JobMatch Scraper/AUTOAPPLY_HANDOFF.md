# JobMatch Auto-Apply — Handoff / Context Summary

_Last updated: 2026-06-20. Paste this into a new session to get full context._

## 1. The goal
Automate Kunal's job applications **end to end**: find matching jobs → **tailor the résumé** to each
job description (Resume Brain + Gemini) → render it as **LaTeX → compiled PDF** → **auto-fill the
application form** → **submit** → log it. Run it as **batch "queue jobs, walk away, get a report."**

**Realistic ceiling (agreed):** fully unattended for jobs with **no walls**; jobs with a
**CAPTCHA / login / account-creation / novel custom question** pause for the human. We will **not**
build CAPTCHA-solving/bypass (it's circumvention, unreliable, and flags the application/account as a
bot). So the honest outcome is: clean jobs auto-submit; the rest are handed back with the tab open.

## 2. Project / where things live
- **Repo:** github.com/hadakunal909-debug/job-search-tool (branch `main`). Project name "JobMatch Scraper".
- **Local dir:** `C:\Users\k.signhhada\Desktop\Job Planning & Research\JobMatch Scraper`
- **Stack:** Python/Flask (`web.py`) + `db.py` (Supabase REST, local-JSON fallback) + Chrome MV3
  extension (`extension/`) + Resume Brain (`resume_brain/`). Supabase project ref `oxvikayddpeczlrzanlb`.
- **Live site:** stemjobs.astrochakra.co (cPanel) — runs the **OLD** code. **Auto-apply only runs
  locally** until the new code + Tectonic are deployed there.

## 3. What's built (all on `main`)
**Backend (`web.py`) ext endpoints (token auth via `_ext_user`, CORS via `_cors`):**
- `/api/ext/tailor` — Resume Brain `run_tailor` + Gemini rewrite → LaTeX→PDF (Tectonic) → base64 +
  normalized profile fields. Cached in `tailored_cache` (key includes profile hash so edits refresh).
- `/api/ext/profile_fields` — normalized profile map for the filler.
- `/api/ext/apply_queue` — auto-sources the user's **top unapplied matches** (by match_score, liked
  boosted) on supported ATS.
- `/api/ext/answer` — **the AI brain**: given profile+résumé + a list of `{label,type,options}`
  fields, Gemini returns `{key: answer}` (exact option for dropdowns; infers from résumé; never invents).
- `/api/ext/debug` — failing forms self-report **structure only** (labels/types/options) →
  `ext_debug_log.jsonl` (gitignored). This is how we fix new forms without manual error-relay.
- `/api/ext/save` — logs to the Applications tracker. `/brain/export/resume.pdf` — LaTeX PDF download.

**Résumé → PDF:** `resume_brain/latex.py` + `resume_brain/templates/resume.tex` (Calibri/Carlito,
single-column, ATS-safe). Compiled by **Tectonic** at `bin/tectonic.exe` (gitignored; install via
`scripts/get_tectonic.ps1`/`.sh`). Gemini call (`resume_brain/ai.py`) has **model fallback**: 30s
timeout per model → shift down `gemini-2.5-flash → flash-latest → 2.0-flash → 1.5-flash → 2.5-pro`.

**DB (`db.py`):** `profiles` schema extended (identity/address/links/work-auth/EEO/comp + `extra`,
`application_defaults` jsonb); `tailored_cache`; `get/put_tailored`. Migrations live in
`APPLICATIONS_SQL` (already run in Supabase).

**Extension (MV3, currently v1.24.0):**
- `popup`: **🚀 Auto-apply** panel (auto-loads matches, **Dry run default ON**, **Auto-submit opt-in**,
  delay, live ✅/🔍/🟢/⏸️/⚠️ results, red ⏹ Stop), **✨ Tailor & fill** for the current page, legacy
  tools under "More tools".
- `filler.js`: `jmFillApplication` (deterministic: name/email/phone/work-auth/EEO/links + **react-select
  combobox** open→type→pick + file attach incl. **shadow DOM** + **typed-input (execCommand) fallback**);
  `jmSnapshotForm` (tags empty fields w/ `data-jmk`, captures options); `jmApplyAnswers` (applies AI
  answers; keyboard combobox select); `jmClickSubmit` (Submit/Continue/Next, not Back/Cancel);
  `jmApplyState` (confirmation + visible-challenge detection, **ignores the invisible reCAPTCHA badge**);
  `jmFormReady`, `jmClickApply`.
- `background.js`: batch runner. Per job: `/api/ext/tailor` → open background tab → **wait for form to
  render** (poll ~9s, click "Apply" if needed) → **fillPass** (deterministic + AI pass) → **multi-step
  submit loop** (submit/next → re-fill new step, up to 6 steps) → log. **Hard-stop** kills the in-flight
  tab. Genuine CAPTCHA/login leaves the tab **open** for the user. Only a real confirmation = ✅;
  otherwise 🔍 verify. Each failure auto-POSTs to `/api/ext/debug`.

## 4. Current state (what works / what doesn't)
- ✅ **AI brain works** (verified live: returns Yes / MA / sponsorship / "+1 8577014592").
- ✅ **LaTeX→PDF works** (Kunal's résumé reproduced 1-page Calibri at
  `~/Desktop/Resume/Kunal_Singh_Hada_latex.{tex,pdf}`; tailored PDFs compile).
- ✅ Greenhouse **server-rendered** forms fill well; a few jobs reach 🟢/✅.
- ⚠️ **In progress:** react-select combobox + multi-step submit reliability across ATS. Ramp (Ashby)
  reached "submit clicked, no change" — now being diagnosed via the auto-capture log.
- Genuinely-custom required questions (clearance, "describe a product", degree/school) correctly **park**.

## 5. Operational facts to run it (next session)
- **Account:** `Kunal08singh` (password is in Kunal's notes — not stored here). Created in Supabase.
- **Extension token:** derive with `python -c "import web; print(web._ext_token('Kunal08singh'))"`.
- **Run server locally:** `python web.py` (loads `.env` via python-dotenv). **Must** have
  `GEMINI_API_KEY` (newer `AQ.Ab8…` format key) + `GEMINI_MODEL=gemini-2.5-flash` in `.env` (gitignored).
  - Known gotcha: an old keyless server can linger on :5000. If AI returns `no_ai_key`, kill all
    python and restart with the key in env. Flask debug reloader can keep stale env — prefer
    `python -c "import web; web.app.run(host='127.0.0.1', port=5000, debug=False)"`.
- **Extension:** load unpacked from `extension/`; set **App URL** = `http://127.0.0.1:5000` + the token.
  After any code change, **reload** the extension; the **manifest version** bumps every change so the
  card number is a reliable "did it reload" signal.
- **Tectonic:** `bin/tectonic.exe` present; else `scripts/get_tectonic.ps1`. Cache `.tectonic-cache/`.
- **Failure captures:** `ext_debug_log.jsonl` in the project dir — read it to fix unsupported forms.

## 6. Next steps
1. **Run a batch (~10 jobs)** → failing forms auto-capture to `ext_debug_log.jsonl` → read it and fix
   per-ATS (Ramp/Ashby submit, Axon multi-required, SmartRecruiters résumé upload, NICE EU SPA).
2. Confirm ✅ submits actually produce **confirmation emails** (only confirmed pages count as ✅).
3. **Phase 2 — vision "computer-use" fallback** (task exists): foreground-tab screenshot+DOM → vision
   model issues clicks/typing, used **only** when the AI-plan fails (cost/speed). Constraint: needs a
   visible tab (not the silent background batch), higher cost, still walled by CAPTCHA/login.
4. **Deploy** the new code + Tectonic to cPanel so the live site (not just local) runs auto-apply.
5. Optional profile polish: `desired_salary` (some forms require it); "how did you hear" already
   defaults (LinkedIn/Other).

## 7. Hard constraints (don't re-litigate)
- No CAPTCHA-solving / bot-detection evasion / fake accounts. CAPTCHA/login/account-creation are
  human steps. **Workday / iCIMS / Oracle / Taleo** are login-walled → deferred/parked.
- It will never be 100% auto — custom required questions need the human. Auto-submit is **opt-in**,
  defaults OFF (dry-run first), and is gated (won't fire on missing required fields or a CAPTCHA).

## 8. Recent commits (newest first)
`b3f0875` auto-diagnostics · `8e1b15d` no-change diagnostics · `0d4a25d` multi-step advance ·
`7f87deb` honest submit verify · `a45fd56` post-submit captcha badge fix · `923ef1b` combobox + typed
fallback · `273efef` AI form-filler · `6b552db` auto-source queue + multi-ATS · `f2641fc` batch runner ·
`9d51357` tailor→fill→submit pipeline.
