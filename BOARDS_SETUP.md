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

- Only **Greenhouse, Lever, Ashby, SmartRecruiters, and Workday** boards expose a
  public feed the scraper can read. Paste a link whose address contains
  `greenhouse.io`, `lever.co`, `ashbyhq.com`, `smartrecruiters.com`, or
  `myworkdayjobs.com`.
- The app detects the platform from the link, checks it returns live postings, and
  saves it. On the **next scrape** (`python scraper.py`, or the 🛰️ Update jobs
  button), those postings flow through the same entry-level / title / US filters as
  everything else.
- Generic career portals (custom sites, iCIMS, Oracle, Eightfold) can't be
  auto-scraped — add those companies to `sponsors.txt` and they appear under
  **🏢 Sponsor careers** as apply links instead.
