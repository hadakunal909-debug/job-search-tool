# Job Scraper

Scrapes the career boards you list, keeps only entry-level postings (and, with
H1B data, only sponsoring companies), and appends **new** jobs to `jobs.csv`.

## 1. Install (one time)

```bash
pip install -r requirements.txt
```

That's all you need for Greenhouse/Lever boards. Playwright (for Workday/JS sites)
is optional — install it only if you add such a source.

## 2. Add your boards

Open `scraper/__init__.py` and edit the `SOURCES` list near the top. Each entry is
`(board_url, ats_type, company_name)`.

Find a company's ATS from its careers-page URL:

| URL looks like | ats_type |
|---|---|
| `job-boards.greenhouse.io/SLUG` | `greenhouse` |
| `jobs.lever.co/SLUG` | `lever` |
| `...myworkdayjobs.com/...` | `workday` (needs Playwright) |

`SOURCES` ships with one verified live board (Boulevard) so your first run scrapes
something real. Add your own boards and delete Boulevard when ready. A wrong or dead
source just logs `FAIL` or `0` and the run keeps going — that's the error-isolation
working: a bad source never stops the run.

## 3. Run it by hand

```bash
python -m scraper
```

It prints what it found per source and the new matches, then appends them to
`jobs.csv`. Open `jobs.csv` in Excel or Google Sheets to review. Run it again and
you'll only see jobs that are new since last time (it dedupes on the URL).

## 4. (Optional) Add H1B sponsor filtering

Create a file `sponsors.txt` with one employer name per line. Get the names from
the U.S. Dept. of Labor LCA disclosure data (or a site like h1bdata.info that
compiles it). With the file present, the script drops companies that have never
sponsored. Without it, every entry-level job is kept and marked `unknown`.

## 5. Run it on a schedule (so it runs itself)

### macOS / Linux — cron
```bash
crontab -e
```
Add one line (runs daily at 8am; use the full path to your folder and python):
```
0 8 * * * cd /full/path/to/job-scraper && /usr/bin/python3 -m scraper >> log.txt 2>&1
```

### Windows — Task Scheduler
1. Open **Task Scheduler** → **Create Basic Task**.
2. Trigger: **Daily**, 8:00 AM.
3. Action: **Start a program**.
   - Program/script: `python`
   - Add arguments: `-m scraper`
   - Start in: the full path to your job-scraper folder.
4. Finish.

**Caveat:** scheduled runs only happen when your computer is **on and awake** at
that time.

## 6. (Later) Run it in the cloud

If you want it running even when your laptop is closed, move it to **GitHub
Actions** — push this folder to a private GitHub repo and add a scheduled
workflow (`on: schedule: - cron: "0 13 * * *"`). It runs free in the cloud on a
timer; commit `jobs.csv` back, or have it email you the new rows. A small VPS or
PythonAnywhere works too.

## Files
- `scraper/` — the scraper package (edit `SOURCES` in `scraper/__init__.py`)
- `requirements.txt` — dependencies
- `jobs.csv` — created on first run; your growing master list
- `sponsors.txt` — optional H1B sponsor names
- `log.txt` — created by the scheduler; what ran and what failed
