# Add-a-board — one-time Supabase setup

The **➕ Add board** view lets you paste a company's job-board link and scrape its
postings. The boards you add are stored in a Supabase table called `boards`.

If you run the app **locally without Supabase**, nothing to do — added boards are
saved to `boards.json` automatically.

If you're on **Supabase** (the deployed app), create the table once:

1. Open your project → **SQL Editor** → **New query**.
2. Paste and **Run**:

```sql
create table if not exists public.boards (
  url        text primary key,
  ats_type   text not null,
  company    text,
  added_by   text,
  created_at timestamptz default now()
);
```

3. Go back to **➕ Add board** and try again. (The app shows this same SQL if it
   notices the table is missing.)

## How it works

- The scraper can read **Greenhouse, Lever, Ashby, SmartRecruiters, Workday, and
  iCIMS/Jibe career sites**. Paste a link whose address contains `greenhouse.io`,
  `lever.co`, `ashbyhq.com`, `smartrecruiters.com`, or `myworkdayjobs.com` — **or**
  an iCIMS career site on a custom domain (e.g. `careers.company.com`); the app probes
  for its `/api/jobs` feed and detects it automatically.
- Other platforms (Oracle, Taleo, SuccessFactors, Eightfold) have no uniform public
  feed and can't be auto-scraped — add those companies to `sponsors.txt` instead.
- The app detects the platform from the link, checks it returns live postings, and
  saves it. On the **next scrape** (`python -m scraper`, or the 🛰️ Update jobs
  button), those postings flow through the same entry-level / title / US filters as
  everything else.
- Generic career portals (custom sites, iCIMS, Oracle, Eightfold) can't be
  auto-scraped — add those companies to `sponsors.txt` and they appear under
  **🏢 Sponsor careers** as apply links instead.

## Bot-protected employers (e.g. Tesla) — two extra routes

Some companies (Tesla is the classic case) sit behind an Akamai bot-wall: every
server-side request — plain `requests`, a Chrome TLS fingerprint, even headless or
visible Playwright — gets `403`/`429`. They can't be added as a normal board.

**The Adzuna aggregator used to be the answer here and no longer is** (removed
2026-08-16). It worked, in the narrow sense that rows arrived — but its API returns a
truncated blurb rather than a description and its redirect pages refuse a server-side
fetch, so an Adzuna row could never carry a real JD or a real apply form. At 6% of the
feed it was 38% of every job with no description. Don't reach for it again; the
reasoning and the measurements are in the comment where `ADZUNA_BOARDS` used to live in
`scraper/__init__.py`. Before assuming an employer needs an aggregator at all, run
`python scripts/probe_adzuna_replacements.py` — it walks the full ATS detect chain and
found real SuccessFactors boards for EY, Capgemini and Birlasoft that nobody had looked for.

What actually works for a genuinely bot-walled employer:

1. **Browser extension (manual, complete).** On **tesla.com/careers**, the JobMatch
   Helper extension's **Import all jobs on this page** button reads the full live
   listing from inside your own browser (which already passed the bot-wall) and pushes
   it into your feed. See `extension/README.md`. Most complete and current, but you run
   it by visiting the page and clicking.
