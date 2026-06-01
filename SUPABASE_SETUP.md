# Supabase setup (cloud storage)

The tool stores everything in Supabase (hosted Postgres) when keys are present, and
falls back to local `jobs.csv` / `user_jobs.json` when they're not. Do this once.

---

## 1. Create the project
1. Sign up at **https://supabase.com** (free).
2. **New project** → name it (e.g. `job-search`), set a database password, pick a region near you, **Create**. Wait ~2 min for it to spin up.

## 2. Create the table
Left sidebar → **SQL Editor** → **New query** → paste this and click **Run**:

```sql
create table if not exists jobs (
  url           text primary key,   -- dedup happens on this
  found_date    text,
  title         text,
  company       text,
  location      text,
  sponsors_h1b  text,
  match_score   int,
  status        text                -- liked / hidden / applied / null
);

-- single-user private project: turn off row-level security so your key has full access
alter table jobs disable row level security;
```

## 3. Get your keys
Left sidebar → **Settings → API**. Copy two things:
- **Project URL** — looks like `https://abcdxyz.supabase.co`
- **`service_role` key** — under "Project API keys" (click reveal). ⚠️ This is a **secret** — it has full DB access. Never commit it or share it publicly. (We use it because the scraper/app run server-side.)

## 4. Add the keys locally
Create the file **`.streamlit/secrets.toml`** in the project folder:

```toml
[supabase]
url = "https://abcdxyz.supabase.co"
key = "PASTE-YOUR-service_role-KEY-HERE"
```

`.gitignore` already excludes this file so it won't be committed.

> Prefer environment variables? Set `SUPABASE_URL` and `SUPABASE_KEY` instead — `db.py` checks those first (this is how GitHub Actions will pass them).

## 5. Test the connection
```bash
pip install -r requirements.txt
python db.py
```
Expected: `Storage backend: Supabase` and a job count (0 until you migrate).

## 6. Migrate your existing 216 jobs
```bash
python -c "import db; db.import_from_files()"
```
This pushes your local `jobs.csv` + `user_jobs.json` into the `jobs` table. Re-run `python db.py` — the job count should now match.

---

## Daily use (unchanged commands, now cloud-backed)
```bash
python scraper.py        # new jobs upserted into Supabase (deduped by url)
python score_jobs.py     # match scores written to Supabase
streamlit run app.py     # reads/writes Supabase; like/hide/applied persist in the cloud
```

## What's next (separate steps — I'll provide these)
- **Deploy** the app to Streamlit Community Cloud (add the same `[supabase]` secrets in the app's *Settings → Secrets*).
- **Schedule** the scrape via GitHub Actions (add `SUPABASE_URL` / `SUPABASE_KEY` as repo secrets).

## Turning it off
Delete `.streamlit/secrets.toml` (or unset the env vars) and the tool silently goes back to local files. Nothing else to change.
