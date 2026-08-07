# JobMatch Helper — Chrome extension

Fills job application forms from your JobMatch profile and your own saved answers, saves jobs to
your **Applications** tracker, and imports jobs the server-side scraper can't reach.

**No AI is involved in applying.** The extension fills the fields it can answer from your data;
**you upload your résumé and click Submit.** Nothing is ever auto-submitted.

## Install (one time, ~1 min)
1. In JobMatch, open **Profile**, fill it in (name, contact, work authorization, EEO, salary —
   whatever you're willing to answer), set your **Default résumé file name**, and copy your
   **token** (looks like `username:xxxxxxxx`).
2. In Chrome go to **`chrome://extensions`**.
3. Turn on **Developer mode** (top-right toggle).
4. Click **Load unpacked** and select this **`extension/`** folder.
5. Pin **JobMatch Helper** (puzzle-piece icon).
6. Click it, paste your **token**, set the **App URL** (`https://stemjobs1.astrochakra.co`, or
   `http://127.0.0.1:5000` to test against a local `python web.py`), and hit **Save**.

> The app moved from `stemjobs.astrochakra.co` to **`stemjobs1.astrochakra.co`** (the old cPanel
> account was suspended). An install still pointing at the old host repoints itself on the next
> popup open — nothing to do by hand.

## Use
- **✍️ Fill this application** — appears on any page where a real application form is detected
  (every ATS the scraper feeds, plus company career domains running Phenom/SuccessFactors/iCIMS
  under their own hostname). It fills your profile fields, then replays your **learned answers**
  for custom questions, then drops a **review panel** listing what it filled and what's left,
  including the **résumé upload** it deliberately doesn't touch. If the form is behind an
  "Apply" / "I'm interested" gate it clicks that first — but never while a chooser dialog
  (Workday's "Start Your Application") is open, since that would dismiss your choice.
- **🚀 Fill my latest matches** — the batch version. It loads **the same jobs your feed would
  show**: your saved search decides what qualifies (match %, visa routes, location, pay,
  dev/management track, staffing agencies), closed postings are dropped, repeat listings from one
  employer are collapsed, and anything already marked applied is skipped. Newest first. Each job
  opens in its own tab, gets filled, and **stays open** for you to upload the résumé and submit.
  Tick **Include company career sites beyond the known ATS** for the wider, best-effort net.
- **📝 Save my answers from this page (train)** — reads how *you* filled the current form and
  saves it to your answer bank, so the next form with the same question fills itself. This also
  happens **passively**: whenever you submit or advance an application form anywhere, the answers
  are captured automatically (sensitive fields — password/SSN/card/DOB — are always skipped).
  Manage or delete saved answers under **🧠 Learned answers (training)**.
- **📌 Save this job to my tracker** — logs the current posting to **Applications** (status
  *applied*, today's date, your default résumé name) and marks it **applied in the feed**. The URL
  is normalized the same way the scraper stores jobs, so it lands on the row the feed already has
  instead of creating a second entry.
- **Import all jobs on this page → feed** (under *More tools*) — for pages the server can't
  scrape. Reads the page's schema.org job data, else the visible job links, and sends them
  through the same title/US filter and URL dedupe as every scraped board. Disabled on
  LinkedIn/Indeed/Glassdoor (their terms ban collection). If a site has a real job board, prefer
  pasting its URL into **➕ Add board** in the app — the server then scrapes it every run, with
  full descriptions.
- **Tesla auto-import** — Tesla's Akamai bot-wall 403s every server-side scraper, but your
  browser has already passed it. The extension imports Tesla's US jobs once a day (background
  alarm, and whenever you browse tesla.com/careers), then fetches each new job's description so
  they get real match scores. *More tools* shows the last run plus a manual button.

## Coverage and limits
- **Tuned adapters:** Greenhouse, Lever, Ashby, SmartRecruiters. Everything else — Workday,
  iCIMS, Oracle, SuccessFactors, Phenom, Workable, UltiPro, BambooHR, Pinpoint, Rippling,
  Avature, JobDiva, Recruitee, Breezy, Personio, Jobvite — goes through the **generic adapter**,
  which handles standard forms plus React comboboxes and custom Yes/No toggle widgets.
- **Login/account walls** (Workday, iCIMS, Oracle) aren't bypassed. Those jobs still queue by
  request — the tab opens, you sign in, and the fill runs on what's there.
- **CAPTCHAs** are never bypassed. The panel says one is present; you solve it.
- **Questions it hasn't seen** stay blank until you answer them once — after that they're in your
  bank and fill themselves. The filler self-improves without a model.
- **Permissions:** single-page filling runs under `activeTab` (granted when you click the
  extension). Batch filling asks for broad site access on **Start**, because an apply page often
  redirects to a different host mid-flow.

## Notes
- Your token links the extension to your account — keep it private. Re-open Profile to copy it
  again. Content scripts never see it: the background worker holds it and makes the calls.
- Résumé *tailoring* is not part of this extension. It lives in the app (Resume Brain).
- Company/title auto-detect is best-effort (reads the page's job metadata) — edit the two fields
  in the popup if a site doesn't expose it.
