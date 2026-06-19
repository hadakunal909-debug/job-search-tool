# JobMatch Helper — Chrome extension

Save any job to your JobMatch **Applications** tracker with one click — from LinkedIn,
Workday, or any company careers site. On supported application forms it can also **tailor your
résumé to the job (Resume Brain → LaTeX → PDF) and auto-fill the form**, then let you review
before you submit.

## Install (one time, ~1 min)
1. In JobMatch, open **Profile**, set your **Default résumé file name**, and copy your **token**
   (looks like `username:xxxxxxxx`).
2. In Chrome go to **`chrome://extensions`**.
3. Turn on **Developer mode** (top-right toggle).
4. Click **Load unpacked** and select this **`extension/`** folder.
5. Pin **JobMatch Helper** (puzzle-piece icon).
6. Click it, paste your **token**, set the **App URL** (`https://stemjobs.astrochakra.co`, or
   `http://127.0.0.1:5000` to test against a local preview), and hit **Save**.

## Use
- On any job posting, click the extension. It auto-detects the **job title** and **company**
  (you can tweak them), then **📌 Just save to tracker** logs it to your **Applications** tab —
  status *applied*, today's date, and your **default résumé name** attached. Duplicates are ignored.
- **✨ Tailor résumé & fill this application (NEW):** on a supported application form (currently
  **Greenhouse** — `boards.greenhouse.io` / `job-boards.greenhouse.io`), the popup shows this
  button. It (1) tailors your résumé to the job description with Resume Brain and compiles it to a
  **PDF via LaTeX**, (2) auto-fills your name, contact, links, work-authorization, EEO answers, and
  common questions from your **Profile**, and attaches the tailored PDF, then (3) drops a **review
  panel** on the page showing how many fields were filled and what still needs your attention.
  **Nothing is submitted automatically** — you review, fix anything flagged, and click **Submit**;
  the panel then logs the application to your tracker. Fill in your **Profile** first so there's
  data to fill with. If you've set a Gemini key in JobMatch it's used to tailor; if not, your best
  matching base résumé is used as-is.
- **Import all jobs on a page → feed:** on **tesla.com/careers/search**, the popup shows an
  **Import all jobs on this page** button. Tesla's site blocks the server-side scraper (Akamai
  bot-wall), but *your* browser has already passed it — so the extension reads the full listing
  from inside the page and sends it to your **jobs feed**, run through the same entry-level /
  title / US filter as every scraped board. Let the careers page finish loading, then click it.
- **Import from ANY careers page (v1.2):** the import button now works on most career
  sites, not just Tesla. It first reads the page's schema.org job data (what Google for
  Jobs reads); if there is none, it harvests the visible job links. Either way the server
  applies the same strict title + US filter and URL dedupe, so a noisy page can't pollute
  the feed. Disabled on LinkedIn/Indeed/Glassdoor (their terms ban collection and your
  account could get flagged — use 📌 Save for single jobs there). RULE OF THUMB: if a site
  has a real job board, paste its URL into **➕ Add board** first — the server then scrapes
  it automatically every day, with full descriptions. Use the extension's import for sites
  the server can't reach: bot-walled (Tesla), JavaScript-only, or feed-less pages.
- **Descriptions + locations come along (v1.3):** after an import, the popup fetches each
  NEW job's detail page (same site, from inside the page) and reads its description, real
  location, and posting date — so imported jobs get a real match %, the 🚫/✅ sponsorship
  badge, and a readable description in the feed, just like scraped boards. Keep the popup
  open for the ~20s it reports while fetching.
- **Tesla AUTO-import (v1.1):** no clicking needed anymore. The extension runs the Tesla
  import by itself **once a day** (background alarm, ~3 min after Chrome starts) and also
  whenever you browse **tesla.com/careers** (throttled to once a day). It then fetches each
  new job's **description**, so those jobs get a real match % instead of 0. The popup shows
  the last run ("🤖 Tesla auto-import …") plus a **Run Tesla import now** button. If a
  background run is blocked by the bot-wall, just open tesla.com/careers once — the visit
  itself triggers the import.

## Batch auto-apply (🚀, NEW)
Open the popup → **🚀 Batch auto-apply** → **Fetch my liked Greenhouse jobs** (or paste job URLs,
one per line) → pick options → **Start**. The runner then, for each job, opens it in a background
tab, tailors your résumé, fills the form, and:
- **Dry run** (default ON): fills + checks and reports "would submit" — **never submits**. Use this first.
- **Auto-submit the clean ones**: when a form is fully filled (résumé attached, no required gaps) and
  there's **no CAPTCHA/login wall**, it submits and logs it to your tracker. Anything with a wall or a
  missing answer is **parked** ("⏸️ needs you") with the reason — it never forces those through.
- Live results show in the popup (✅ submitted · 🟢 ready · ⏸️ needs you · ⚠️ error). It runs in the
  background, but for long queues keep Chrome open; very long runs can pause (browser may idle-evict the
  worker) — just press Start again to resume the rest.
- On Start you'll be asked to **grant access to those job sites** (so the background can fill them), and —
  if auto-submit is on — to **confirm** before any real submission.

Currently sources/handles **Greenhouse** jobs only.

## Tailor & fill — requirements and limits
- **Server setup (one time):** résumé PDFs are compiled with **Tectonic** (a single self-contained
  LaTeX binary). Install it into the repo's `bin/` once with `scripts/get_tectonic.ps1` (Windows)
  or `scripts/get_tectonic.sh` (Linux/macOS) — no system TeX install needed. The first compile
  downloads LaTeX packages into `.tectonic-cache/` (one time). If Tectonic can't run where the
  backend lives (e.g. some shared hosts), the endpoint automatically falls back to a `.docx`.
  **Tip:** for the most reliable apply flow, point the extension's **App URL** at your **local**
  backend (`http://127.0.0.1:5000`) where Tectonic is installed.
- **Coverage:** v1 fills **Greenhouse** only. Workday/iCIMS/Oracle multi-step, login-walled SPAs
  are out of scope. On an unsupported page the button reports that no form was found.
- **CAPTCHAs / logins:** never bypassed. If a verification is detected, the panel says so — solve
  it yourself, then submit. Nothing is auto-submitted.
- **Permissions:** filling runs under `activeTab` (granted when you click the extension), so no
  broad host permission is needed for the page you're on. `optional_host_permissions` is declared
  for future embed/redirect cases but isn't requested unless needed — this keeps the install
  warning minimal.

## Notes
- Single-job **Save** logs to your **tracker**; **Tailor & fill** additionally fills the form.
- Company/title auto-detect is best-effort (reads the page's job metadata) — just edit the two
  fields in the popup if a site doesn't expose it.
- Your token links the extension to your account — keep it private. Re-open Profile any time to
  copy it again.
