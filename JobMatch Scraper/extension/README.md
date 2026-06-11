# JobMatch Helper — Chrome extension

Save any job to your JobMatch **Applications** tracker with one click — from LinkedIn,
Workday, or any company careers site.

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
  (you can tweak them), then **📌 Save to JobMatch** logs it to your **Applications** tab —
  status *applied*, today's date, and your **default résumé name** attached. Duplicates are ignored.
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
- **Tesla AUTO-import (v1.1):** no clicking needed anymore. The extension runs the Tesla
  import by itself **once a day** (background alarm, ~3 min after Chrome starts) and also
  whenever you browse **tesla.com/careers** (throttled to once a day). It then fetches each
  new job's **description**, so those jobs get a real match % instead of 0. The popup shows
  the last run ("🤖 Tesla auto-import …") plus a **Run Tesla import now** button. If a
  background run is blocked by the bot-wall, just open tesla.com/careers once — the visit
  itself triggers the import.

## Notes
- This logs jobs to your **tracker**; it does **not** fill out the company's application form.
- Company/title auto-detect is best-effort (reads the page's job metadata) — just edit the two
  fields in the popup if a site doesn't expose it.
- Your token links the extension to your account — keep it private. Re-open Profile any time to
  copy it again.
