# Deploy the job tool (Streamlit Cloud + scheduled scraping)

**Do `SUPABASE_SETUP.md` first** (project, table, keys, migration). Storage must be on
Supabase so the cloud app and the scheduled scraper share one database.

## A. Put the code on GitHub
1. Create a GitHub repo — **Private recommended** (the repo includes your `resume.txt`).
2. From the project folder:
   ```bash
   git init
   git add .
   git commit -m "Job-search tool"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```
   `.gitignore` already keeps `secrets.toml`, `jobs.csv`, and `__pycache__` out of the repo.

## B. Deploy the app on Streamlit Community Cloud
1. Go to **https://share.streamlit.io** → sign in with GitHub → **New app**.
2. Choose your repo / branch / **`app.py`** → **Deploy**.
3. App → **Settings → Secrets** → paste (same values as your local `secrets.toml`):
   ```toml
   [supabase]
   url = "https://xxxx.supabase.co"
   key = "your-service_role-key"
   ```
4. The app restarts and now reads/writes your Supabase data. Share the public URL.

## C. Schedule the daily scrape (GitHub Actions)
The workflow already exists at `.github/workflows/scrape.yml` (daily + manual).
1. Repo → **Settings → Secrets and variables → Actions → New repository secret**, add two:
   - `SUPABASE_URL` = `https://xxxx.supabase.co`
   - `SUPABASE_KEY` = `your-service_role-key`
2. Repo → **Actions** tab → enable workflows → open **“Scrape jobs”** → **Run workflow** to test it once.
3. Refresh your deployed app — new jobs should appear.

## Notes
- Cron is **UTC** (`0 13 * * *` ≈ 8am ET) — adjust in `scrape.yml`.
- Keep the repo **Private**, or remove `resume.txt` from it (it has your contact info).
- Free tiers cover all of this: Streamlit Community Cloud (1 app), GitHub Actions minutes, Supabase (500 MB).
- The scraper still runs locally too — with `secrets.toml` present it writes to Supabase; without it, to `jobs.csv`.
