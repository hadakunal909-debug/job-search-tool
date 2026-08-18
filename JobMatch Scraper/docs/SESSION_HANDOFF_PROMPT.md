# Handoff prompt — JobMatch, session of 2026-08-18

Paste this at the start of a new chat to resume with full context. Everything below is fact
established by measurement against the live database or by reading the code in this session, not
recollection. It replaces the 2026-08-12 version, which predates the move off Supabase.

---

## The project

**JobMatch** — a personal job-search tool at `C:\Users\k.signhhada\Desktop\Job Planning & Research`.
The Flask app lives in `JobMatch Scraper/` (`web.py`, ~6,300 lines), serving **23,153 scraped jobs**.
Live at **stemjobs1.astrochakra.co** on shared cPanel hosting (LiteSpeed + Passenger). A Chrome
extension in `JobMatch Scraper/extension/` fills application forms.

### Where the database is — this changed on 2026-08-15
**cPanel Postgres, not Supabase.** `.pg_dsn` is `host=127.0.0.1 dbname=astrocha_jobmatch`, so it is
reachable ONLY from the cPanel box. Three transports sit behind one `db.py` (`_LazyHTTP`, `db.py:47`):

1. `PG_DSN` set → `pgrest.Session` (direct psycopg). This is the cPanel app.
2. `DB_PROXY_URL` + `DB_PROXY_SECRET` → `dbproxy.Session`, HMAC-signed HTTPS to `POST /api/db`.
   This is how GitHub Actions and your laptop reach it.
3. Neither → Supabase, then a local-CSV fallback.

**To query production read-only from here**, use transport 2 with the local secret:
```bash
cd "JobMatch Scraper" && DB_PROXY_SECRET="$(tr -d '\r\n' < .db_proxy_secret)" \
  DB_PROXY_URL="https://stemjobs1.astrochakra.co/api/db" python your_script.py
```
`db.using_supabase()` means "is there a remote database at all" and answers True for all three —
the name is historical. `db.backend_name()` is the one that says which.

### Deploy — read this before advising anything
**Pushing to GitHub does NOT deploy.** cPanel only runs `.cpanel.yml` for a repo hosted on cPanel,
and origin is GitHub. The deploy mechanism is a **zip**:
```bash
cd "JobMatch Scraper" && python scripts/build_deploy_zip.py "../stemjobs1_<sha>.zip"
```
then cPanel → File Manager → the directory holding `passenger_wsgi.py` → Upload → Extract →
`touch tmp/restart.txt`.

### The daily schedule is split three ways
| When (ET) | Runner | What |
|---|---|---|
| 09:00 Mon–Fri | GitHub Actions (`.github/workflows/scrape.yml`, repo ROOT) | heavy pass: sweep, full score, **verify_dates**, **analytics rollup**, **digest email** |
| 13:00 | cPanel cron (`bin/cron_scrape.sh`) | sweep + new-only score |
| 17:00 | cPanel cron | sweep + new-only score |

---

## What this session found and fixed

### 1. The Actions run had been failing for four days — and it is still failing
Three scrape runs died on their first read with `HTTP 401 {"error":"bad signature"}`.
`scripts/probe_db_proxy.py` (read-only) was run against the live endpoint and proved the secret in
`JobMatch Scraper/.db_proxy_secret` (`sha256[:12] = 7ef302e0f70d`) is **accepted at every body
size**. So the value in the GitHub secret differs.

**STILL OPEN — only you can fix it:** re-paste `DB_PROXY_SECRET` in GitHub → Settings → Secrets →
Actions, with no trailing newline. Until then there is no `verify_dates`, no analytics rollup and
no digest email. Measured consequence: **0 verified posting dates since 2026-08-15**,
`events_daily` frozen at 2026-08-14.

### 2. Analytics were silently dead — fixed
`events.id` is a bigserial with `events_pkey PRIMARY KEY (id)`. The migration copied 13,293 rows
**with their ids** (up to 13,481) and there was no `setval` anywhere in the repo, so the sequence
still pointed at 1 and every insert since collided. `db.insert_events` swallows failures by design,
so nothing said a word. Newest event was `2026-08-15T02:07:09`.

Fixed three ways: `pgrest.Session.repair_sequences()`, called self-healingly from
`db.insert_events` on the first duplicate-key failure; the same repair added to
`scripts/migrate_project.py` so a future copy cannot leave it behind; and a loud one-shot warning
plus an "Analytics writes" check in `/admin/health.json` keyed on the newest event's age.

### 3. Data Health showed no tables — fixed, and it was never a missing table
`pgrest.py`'s RPC branch emitted `SELECT * FROM fn()` and **threw the POST body away**, so
`db.ev_usage(30)` silently became the function's 7-day default. Worse, `SELECT * FROM db_stats()`
returns `[{"db_stats": {...}}]` — a list — and `db.db_stats()` does `return d if isinstance(d, dict)
else {}`, so it always got `{}`, `have_rpc` was False, and `admin_data.html:9` hid the whole Tables
section against a database that was answering correctly the entire time.

`pgrest.build_rpc()` now passes named arguments and `pgrest.unwrap_rpc()` unwraps the scalar the way
PostgREST does. The branch had **zero** test coverage; `scripts/test_pgrest.py` now has 11 cases for
it (49 checks total).

`brain_companies` genuinely does not exist, and that is deliberate and documented
(`web.py:3239`) — Resume Brain falls back to `brain_companies_local.json`. The health check saying
so is correct; only its wording was Supabase-era.

### 4. "Only 30–40 jobs a day" — two different answers
**The scrape is healthy.** ~1,370 boards a sweep, ~366k postings scanned, **300–1,800 genuinely new
rows a day**.

**For most accounts and for the digest, the limiter was the match floor.** `core.score_against`
answers "what share of this JD's weighted keywords does the résumé contain", which is structurally
bounded — nothing in 22,424 rows scored above 69, the mode was 30–39, and the default floor of 45
sat at the **94th percentile**. So the score is now calibrated (`core.calibrate_score`,
`score_calibration.json`, rebuilt by `scripts/build_score_calibration.py`) and the default floor is
**70 on the new scale** (`core.DEFAULT_PREFS["min"]`, `MIN_SCALE = 2`). Measured effect: **4.8×
more rows visible** — e.g. 2026-08-16 went 28 → 158.

Applied in exactly one place, `web._build_row`, so `_filter_rows` and `app.js::matches()` — the
deliberate twins — inherit it and `scripts/feed_parity.py` still agrees 79/79.

`normalize_prefs` migrates a pre-v2 stored floor to the new default once and stamps `min_scale`.
**A stored 0 is preserved**: "no floor" means the same thing on every scale.

**But your own account was never limited by the floor.** `Kunal08singh` has `min: 0` stored. Its
real limiters are `date: "1"` (posted within ONE day) and `track: "mgmt"`, plus `exp: "2"` and
`intern: "no"`. Widening the date window is the single biggest lever on that account, and it is a
one-click change in the feed's filter rail — deliberately not changed for you.

### 5. Logos — 92.2% of rows now resolve to a verified domain
Measured before: 76.9% of companies got their domain from `strip-non-alphanumerics + ".com"`, and
probing 249 of them found ~18% monogram, ~10% black glyph, ~5% blurry upscale — about **1 card in
3**. The black is NOT a CSS filter; there is no invert or blend mode anywhere. Those are real
monochrome favicons composited onto `.logo img{background:#fff}`.

- `scripts/build_company_domains.py` resolves every employer by **asking**: candidates from the
  name and from the posting host, each verified against an icon probe. `company_domains.json` now
  holds **1,446 verified domains for 1,832 companies**; 201 differ from the old guess.
  **Ordering matters and is counter-intuitive** — the NAME is tried before the posting host.
  Host-first produced `udemy → careerpuck.com` and `amazon → amazon.jobs` on the first run, because
  an ATS host is a real domain with a real icon and verification cannot reject it.
- `web.logosrc` / `web.logofavicon` own the URL in one place; it had been hand-copied into
  `static/app.js`, `templates/company.html` and `templates/job.html`.
- The `data-fallback` slot in `app.js:375` had existed and been **wired to nothing** since it was
  written. It now carries the second provider; `jobpage.js` walks the same chain.
- `wireLogos()` defaulted to the `feed` element, so the company header and employer modal were
  never given a fallback. Now `document`.
- `/company` recomputed the domain **without the posting URL**, so 219 companies showed one logo on
  the card and a different one on the employer page. It now reuses the row's answer.
- `templates/company.html`'s employer modal had a `.logo` div with **no `<img>` at all**.

**STILL OPEN — needs you:** set `LOGODEV_KEY` (a publishable `pk_…`) in the cPanel Python App env.
Without it the chain degrades to the favicon service, which is what shipped before. With it, the
~10% black-glyph class gets real brand marks. CSP `img-src` already allows `https://img.logo.dev`.

### 6. Work at a Startup — there was never a scraper
The 38 `workatastartup.com` rows were a one-off `jsonld` import from 2026-08-02, all filed under the
single company `"Y Combinator's Work at a Startup"`, all with NULL location and NULL date, half of
them 404, none clearing the floor. `SOURCES` and `SCRAPERS` had no entry for it.

`scrape_workatastartup` now reads the Inertia `data-page` payload. **Pagination is by ROLE, not by
page number**: `?page=2` returns byte-identical ids and bare `/jobs` is just the engineering facet,
so the ten paths in `props.roleLinks` are the pagination. Measured live: **246 jobs across 141 real
startups**, every one with a location.

### 7. Two silent ingest bugs, and closed postings
- `scrape_jsonld` **set** `found_date = ""`, which defeats `main()`'s `setdefault` and then gets
  stripped by `db.add_jobs`, so every jsonld row landed with a NULL date. It now omits the key.
- A blank location bypassed the US filter (`is_us_location("")` is True **on purpose** — only 0.8%
  of rows have none and dropping them would lose real Uber/Synopsys/McKinsey jobs). The leak was
  the country being in the TITLE. `title_says_non_us()` is a **veto only** — routing the
  parenthetical through `is_us_location` instead marks "(Senior)" and "(Contract)" as non-US.
- `RECONCILE_CLOSED` was never set anywhere, so closed-posting retirement had only ever DRY RUN in
  the life of the project — 5,178 rows sat under the miss threshold. Now `1` in both schedulers.
  Its guards are all intact: a failed fetch proves nothing, a board returning a small fraction of
  what we store is skipped, three consecutive misses required, rows are never deleted.
- **New: per-board health is persisted.** `scraper.save_board_health` writes a rolling 8-run window
  per board into the `scrape_status` kv table (no DDL — there is no DDL path from the scraper) and
  prints a FAILED / SILENT triage list each run. This is the durable answer to "is every company
  still being scraped".

### 8. Scores could be stranded forever
`_new_only_targets` looked only at what the run touched. A run ingesting more than
`SCORE_MAX_FETCH` (400 on cPanel; 843 and 1,833 arrived on two days this month) left the excess
with a NULL `match_score`, and those rows were in neither set — the full pass that was supposed to
sweep them up runs only in the Actions job, which was dead. 729 rows were unscored and therefore
invisible to the match filter at any floor. It now also targets rows whose stored score is NULL.

### 9. Extension
No update mechanism existed at all: no `update_url`, no Web Store listing, no version endpoint, no
packaging step. Added `GET /api/ext/version` (**unauthenticated on purpose** — an extension stale
enough to have a broken token contract is exactly the one that needs to hear it) and a popup banner.
Manifest is now **1.36.0**; `web.EXT_MIN_VERSION` matches. Note the bootstrap gap: installs at
1.35.0 have no check, so the first reload has to be manual.

`scripts/test_ext_contract.py` is now in CI. It was excluded because with no credentials it printed
"No accounts" and exited 0 — a green tick that proved nothing. It now runs its data-free half
(routes, token rejection, the version endpoint, CORS preflight) and skips only the response-shape
section that genuinely needs an account.

### 10. JD backlog is now fully characterised
`scripts/audit_jd_coverage.py` had never produced a report (`jd_coverage_audit.csv` was 64 bytes —
headers only). It now has: **2,031 rows, of which 1,822 are bot-walled (403/405) and 209 are gone
(404, or a Workday page that answers 200 with no description)**. There is no
extractable-but-missing class left.

---

## Current state

- **All 26 suites pass** (16 under `scripts/`, 10 at the app root; no runner exists, run each).
- `scripts/feed_parity.py`: **79/79 filter cases agree**.
- Nothing is committed — the work is in the working tree on `tools/db-proxy-probe`.
- Nothing has been deployed. The live site is serving the previous build.

## Outstanding, in order

1. **Re-paste `DB_PROXY_SECRET` in GitHub.** Nothing else restores verify_dates, the analytics
   rollup or the digest email.
2. **Upload the deploy zip**, then `touch tmp/restart.txt`.
3. **Set `LOGODEV_KEY`** in the cPanel env for real brand logos.
4. Widen `date` on `Kunal08singh` from 1 day if you want more roles on your own feed.
5. **Seven companies have no board at all**: ASML, Deutsche Bank, LTIMindtree, Marlabs, Qualcomm,
   Renesas, Tradeweb. They were Adzuna-only and Adzuna was removed on 2026-08-16; their `boards`
   rows still say `ats_type="adzuna"` and are inert. Guessed Workday/Greenhouse tenants did not
   resolve — each needs its real tenant id. Ten more Adzuna orphans are covered thinly elsewhere.
6. `_research_for` (`web.py:1982`) still calls `logodomain(display)` with no URL. Left alone
   deliberately: it is a research cache key, and changing it invalidates stored research.

## Gotchas that cost real time — don't rediscover them

- **`EV_OFF=1` before importing `web`**, always. One unguarded parity run once wrote 98.8% of all
  recorded `feed_view` events.
- **The repo has mixed line endings.** `db.py` and `pgrest.py` are LF; `web.py`, `core.py` and most
  of `scripts/` are CRLF. A patch script that reads with universal newlines and writes `\n` will
  rewrite a whole file and bury the real diff.
- **`web.py::_filter_rows` and `static/app.js::matches()` are deliberate twins.** Change one, change
  the other, and re-run `scripts/feed_parity.py` — it lifts functions from `app.js` **by source
  text**, so a helper must be a top-level `function`, not a `var` const.
- **`db._fetch_all` overwrites `limit`** with its 1000-row page size. Asking it for one row walks the
  whole table. `db.newest_event_ts()` is the shape to copy for a one-row select.
- **A calibrated score that saturates destroys the feed's ranking.** The first curve mapped every
  raw ≥ 60 to 100 and the top 40 rows of a real account all read 100 — the feed sorts on this
  number. The tail above raw 45 is spread by hand for that reason.
- **`db.list_users()` returning nothing is not "nothing to test".** That assumption is why the
  extension contract test sat outside CI.
- **Verify by behaviour, not substring.** Several assertions in past sessions printed false failures
  when the code was fine and the check was wrong.
- **Load-testing production is off-limits.** Shared cPanel enforces account-level throttling that no
  restart clears, and the previous account was suspended once.
