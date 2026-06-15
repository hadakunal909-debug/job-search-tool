# Resume Brain — Supabase setup (run once)

Resume Brain is merged into the app under **🧠 Resume Brain** in the nav. It works immediately
with **local-file fallback**, but to persist + sync across your two deploys (cPanel + Render) and
across devices, add its storage in Supabase.

Open **Supabase → SQL Editor** and run:

```sql
-- 1) Per-user knowledge base: stories + lessons + the self-training model, as one JSON column.
alter table public.users add column if not exists brain_kb jsonb;

-- 2) Shared company research (one row per domain, reused by everyone — company facts are public).
create table if not exists public.brain_companies (
  domain     text primary key,
  data       jsonb,
  fetched_at timestamptz default now()
);
```

That's it. Notes:

- **Résumés** reuse the existing `public.resumes` table (per user) — no change. On first visit your
  legacy single résumé (the old "My résumé") is auto-migrated into that library so it still drives
  your match scores.
- The app uses the **secret** Supabase key (bypasses RLS, like every other table here), so no RLS
  policies are needed.
- **Before** you run this SQL: stories/lessons/company-research fall back to local JSON files
  (`brain_kb_local.json`, `brain_companies_local.json`). That's fine locally and on cPanel (its disk
  persists), but **Render's disk is ephemeral**, so brain data there won't survive a redeploy until
  the SQL is run. Résumés + the feed always work (they're in Supabase already).

## What's per-user vs shared
- **Per user (private):** résumés, stories, lessons, the self-training model.
- **Shared (team-wide):** company research — any user's crawl of a company benefits everyone.

## How matching changed
The job feed now scores each job against your **complete profile** — every résumé **plus** every
story in Resume Brain — not a single résumé. Add stories in **Resume Brain → Teach** to enrich both
the feed scores and the tailoring.

## Deploy (cPanel)
The merged app adds: the `resume_brain/` package, `templates/brain_*.html`, brain CSS in
`static/style.css`, new `/brain*` routes in `web.py`, and `db.py` storage helpers. Make sure the
deploy bundle includes the **`resume_brain/` folder** and the new templates, then **Restart** the
Python app. `requirements-cpanel.txt` now includes `python-docx` (for .docx export) — Run Pip
Install after updating it. The AI rewrite reuses the app's existing Gemini key (session or
`GEMINI_API_KEY`).
