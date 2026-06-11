# Deploy to cPanel (Flask + Passenger)

This runs the app as a **Flask web app** under cPanel **"Setup Python App"**, with the
**scraper on a cron job**. Data stays in **Supabase**. Your Streamlit/Render version
keeps working separately — these don't interfere.

App URL: **stemjobs.astrochakra.co** · Python app root: **`stemjobs`**

---

## 1. Put the files on the server
**Option A — Git** (cPanel → *Git Version Control* → Create → clone your repo into `stemjobs`).
**Option B — manual:** download the repo ZIP from GitHub → File Manager → upload into
`stemjobs` → Extract.

The `stemjobs` folder must contain: `passenger_wsgi.py`, `web.py`, `core.py`, `db.py`,
`auth.py`, `scraper/` (the whole package — includes `score_jobs.py`, `notify.py`), `templates/`, `static/`, `requirements-cpanel.txt`,
`idf.json`, `careers_us.md`, `sponsors.txt`, `resume.txt`.

## 2. Create `.env` (credentials)
In File Manager, inside `stemjobs`, make a file named **`.env`**:

```
SUPABASE_URL=https://oxvikayddpeczlrzanlb.supabase.co
SUPABASE_KEY=your-supabase-secret-key
APP_SECRET=any-long-random-string
```

(`.env` is gitignored — never commit it. Both the web app and the cron scraper read it.)

## 3. Install dependencies
Setup Python App → your app → **Configuration files** → add `requirements-cpanel.txt`
→ **Run Pip Install**. (Installs Flask, requests, beautifulsoup4, lxml, rapidfuzz.)

## 4. Startup settings
- **Application startup file:** `passenger_wsgi.py`
- **Application Entry point:** `application`
- Click **Restart**.

## 5. Open it
Visit **https://stemjobs.astrochakra.co** → log in (e.g. `Kunalrana` / `Kunal123`).
Paste a résumé under **My résumé** for personalized match %.

## 6. Schedule the scraper (cron)
Find your venv Python path from the app's *"Enter to the virtual environment"* line —
it's the `…/bin/activate` path with `activate` swapped for `python`, e.g.
`/home/USER/virtualenv/stemjobs/3.9/bin/python`.

cPanel → **Cron Jobs** → add (e.g. daily 6:00 am):

```
cd /home/USER/stemjobs && /home/USER/virtualenv/stemjobs/3.9/bin/python -m scraper >> scrape.log 2>&1 && /home/USER/virtualenv/stemjobs/3.9/bin/python -m scraper.score_jobs >> score.log 2>&1
```

(`cd` first so `.env`, `idf.json`, etc. resolve. Replace `USER` and confirm the path.)

---

## Notes
- After a scrape, click **🔄 Reload** in the app (it caches jobs ~5 min).
- On Python 3.9 the `secrets.toml` path is inactive — that's why we use `.env`.
- Résumés are private: behind login, stored in the Supabase `users` table.
- If the host limits CPU/processes and the scrape is killed, lower the worker count in
  `scraper.scrape_all(..., workers=8)` to e.g. 4.
- This is **on-demand/cron** scraping — no always-on process, so it fits shared cPanel.
