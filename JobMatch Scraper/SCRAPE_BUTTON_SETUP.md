# "Update jobs" button + live progress bar — setup

Clicking **Update jobs** on the feed fires the GitHub Actions workflow
(`.github/workflows/scrape.yml`, at the repo root) which runs the scraper + scorer on GitHub's
servers and writes to Supabase. As it runs, it reports progress to a shared `scrape_status` row;
the feed page polls it and draws a live bar (boards done, jobs found, elapsed, ETA) and auto-refreshes
when the run finishes.

The free cPanel host **can't** run a 15–30 min scrape inside a web request (it would freeze/timeout),
which is why the scrape runs on GitHub and the page just *watches* a shared status row.

## One-time setup (3 things)

### 1. Supabase table — REQUIRED for the bar to show on the live site
The scrape runs on GitHub's runner but the web app runs on cPanel — they only share **Supabase**.
So the progress row must live in Supabase, or the bar won't appear online. In the Supabase SQL editor:

```sql
create table if not exists scrape_status (
  id text primary key,
  data jsonb,
  updated_at timestamptz
);
```
(Without this table, `set_scrape_status` falls back to a local file on whichever machine runs — fine
for local testing, invisible across machines in production.)

### 2. GitHub repo secrets — so the Action can write jobs + use Adzuna
GitHub → your repo → **Settings → Secrets and variables → Actions → New repository secret**, add:
- `SUPABASE_URL` — `https://<project>.supabase.co`
- `SUPABASE_KEY` — your Supabase secret key
- `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` — from developer.adzuna.com (without these the Adzuna
  boards/searches stay dormant; everything else still runs)

### 3. cPanel `.env` — so the button can *launch* the Action
Add a GitHub **fine-grained personal access token** (scoped to this repo, permission **Actions:
Read and write**) to the `stemjobs/.env`:
```
GH_TOKEN=github_pat_xxx
```
(Optional overrides if your repo/workflow names differ: `GH_REPO=owner/repo`, `GH_WORKFLOW=scrape.yml`.)

## How it behaves
- **No `GH_TOKEN`:** the button returns a message telling you to add it (you can still run the scrape
  from the repo's **Actions** tab or via cron).
- **Token set, no `scrape_status` table:** the Action runs and updates jobs, but the live bar won't
  appear (status can't be shared) — you'll just hit Reload when it's done.
- **All three set:** click Update jobs → bar shows *Starting → Scraping N/225 boards (X%, ~Y left) →
  Saving → Scoring → Done* and the feed refreshes itself.

## Notes
- The daily `schedule` in `scrape.yml` (13:00 UTC) also drives the bar — if you're on the feed when the
  cron run fires, the bar appears automatically. You can drop the separate cPanel cron to avoid double
  scraping.
- Phases: `queued → scraping → saving → scoring → done`. The bar treats a status with no update for
  >3 min as finished (in case a run is cancelled).
