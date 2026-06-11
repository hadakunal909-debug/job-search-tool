# Job-Search Tool — Handoff (paste into a new chat to continue)

> The memory files in this project auto-load in any new chat, so most of this carries over
> automatically. This is the full snapshot.

## Working style — GHOST MODE (read first)
Be terse and low-chatter: **lead with the result, minimal preamble/recap, batch tool calls,
short summaries, and don't over-ask** — pick sensible defaults and proceed. Skip the play-by-play.

## Who it's for
Kunal — international grad, M.S. Project Management (Northeastern, Dec 2025), Boston. Needs US
roles open to **OPT/STEM-OPT/H1B sponsorship**. Targets **entry-level PM / coordinator / analyst /
operations**. Can run/edit Python but isn't a pro dev — keep it practical.

## What it is
Personal job-search tool (no paid jobs API). Files:
- `scraper/` — the scraper package; its main module `scraper/__init__.py` scrapes ATS feeds → Supabase (or jobs.csv). Run with `python -m scraper`.
- `scraper/score_jobs.py` — computes the ATS-style match score per job; builds `idf.json`. Run with `python -m scraper.score_jobs`.
- `core.py` — matching / JD-fetch / resume logic (no Streamlit).
- `db.py` — storage: Supabase via PostgREST+`requests`, else local files.
- `app.py` — Streamlit card-feed UI (match rings, H1B badges, filters, Tailor view).
- helpers (in `scraper/`, run via `python -m scraper.<name>`): `build_sponsors.py` (DOL→sponsors.txt),
  `find_boards.py` (probe a company for a board), `make_careers.py`→`careers_us.md`
  (per-sponsor US careers + LinkedIn links).

## Current state (2026-05-31)
- **Supabase is LIVE** — project `oxvikayddpeczlrzanlb`, table `jobs`; creds in
  `.streamlit/secrets.toml` (gitignored). NOTE: db.py uses the **REST API via `requests`** because
  the `supabase` SDK won't build on Python 3.14.
- **~407 scored jobs across ~38 companies**: Amazon + 26 ATS boards (Samsara, Stripe, Palantir,
  Ramp, Datadog…) + 10 extras (Lyft, Pinterest, Block, Dropbox, LinkedIn, Snowflake, ServiceNow,
  Visa, Uber, ByteDance) + 2 Workday (Salesforce, Nvidia).
- **Scraper supports 6 ATS types:** greenhouse / lever / ashby / smartrecruiters (JSON APIs),
  amazon (search.json), workday (CXS JSON).
- **Filters:** entry-level title filter (specific role phrases + new-grad markers, in INCLUDE/EXCLUDE);
  US-only; `MAX_YEARS=3` experience cap (enforced where a JD is available — Amazon/Workday);
  H1B sponsor flag from `sponsors.txt`.
- **Matching = ATS-style** (`core.skill_match`): weighted % of the JD's keywords present in the
  resume — hard skills/tools/certs (`ATS_KEYWORDS`) ×2.5, requirement-section terms ×1.6. Scores
  spread ~9–65. The **Tailor view** headlines “🤖 ATS keyword match %” + “Add these keywords”
  (the missing-keyword tailoring list). Lexical only; a Claude semantic deep-match is wired to
  switch on when `ANTHROPIC_API_KEY` is set (currently key-free by choice).

## Run / refresh
1. `python -m scraper`  →  2. `python -m scraper.score_jobs`  →  3. `python -m streamlit run app.py` (localhost:8501)
Daily auto-scrape: `.github/workflows/scrape.yml` (needs GH repo secrets once deployed).

## Config knobs
- `scraper/__init__.py`: `SOURCES = AMAZON + ATS_BOARDS + EXTRA_BOARDS + WORKDAY_BOARDS`; `MAX_YEARS`;
  `INCLUDE`/`EXCLUDE`; `AMAZON_QUERIES` / `WORKDAY_QUERIES`; `US_ONLY`; `VERBOSE`.
- `core.py`: `ATS_KEYWORDS`, `SKILL_WEIGHTS`, the SKILLS aliases.
- `app.py`: ring tiers in `match_card_html` (Strong ≥55 / Good ≥42); min-match slider default 45.

## Open items
1. **Deploy** to Streamlit Cloud + add GitHub Actions repo secrets `SUPABASE_URL`/`SUPABASE_KEY`
   (see `DEPLOY.md`) → public URL + daily scrape.
2. **Rotate the Supabase secret key** (it was pasted in chat) → Supabase → Settings → API.
3. Optional **Claude deep-match** for real semantic matching (set `ANTHROPIC_API_KEY`).
4. More **Workday** companies (look up each `*.myworkdayjobs.com` URL) or other boards via `python -m scraper.find_boards`.
5. `README.md` is stale (new ATS types, Supabase, ATS matching, UI).

## Not scrapeable (use careers_us.md links + email alerts)
Google, Microsoft, Apple, Meta, Netflix, most consulting (Deloitte/Accenture/TCS…), banks, pharma —
custom/Eightfold portals with no public feed.
