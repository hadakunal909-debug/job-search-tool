# Operations

Everything you *do* to this app, in one place: deploy, environment, the schedule, adding a board,
adding a user, running the tests, and regenerating the data files.

Companion docs: [ARCHITECTURE.md](ARCHITECTURE.md) for how it works, [INDEX.md](INDEX.md) for
"X is broken, which file", [SESSION_HANDOFF_PROMPT.md](SESSION_HANDOFF_PROMPT.md) for what
changed most recently and what is still open.

---

## 1. Deploy

**`git push` does not deploy. Nothing on the server hears about a push.**

cPanel's Git Version Control runs `.cpanel.yml` only for a repository *hosted on cPanel*, and this
project's origin is GitHub. `.cpanel.yml` exists, is correct, and never executes. Verified
2026-08-09: `main` was pushed with a full release on it and five minutes of polling
`stemjobs1.astrochakra.co` showed the served `style.css` unchanged and no reference to the new
`tip.js`. The git path had not run at all.

The deploy is a zip:

```bash
python scripts/build_deploy_zip.py
```

Writes `../stemjobs1_deploy.zip` (pass a path to override). Then:

1. cPanel → **File Manager** → the directory holding `passenger_wsgi.py` (`$HOME/stemjobs`)
2. **Upload** the zip → **Extract**
3. `touch tmp/restart.txt`

The archive is **flat** — entries sit at its root, so extracting inside the app directory lands
every file where Passenger expects it. It packs exactly the file list `.cpanel.yml` deploys, and
the script **refuses to build** if the two disagree about a module `web.py` imports. Don't defeat
that check; it is the only thing keeping the two lists in step.

Deliberately not in the zip: `.env` and any secret (they never leave your machine), and
`jdmeta.json` (30+ MB, and there is nowhere fresh to copy it *from* — it is built on the GitHub
Actions runner, whose filesystem is discarded when the run ends).

### First-time cPanel setup

Under **Setup Python App**:

- **Application startup file:** `passenger_wsgi.py`
- **Application Entry point:** `application`
- **Configuration files** → add `requirements-cpanel.txt` → **Run Pip Install**

`requirements-cpanel.txt`, not `requirements.txt` — the latter carries the scrape-side
dependencies that shared hosting can't build.

> The original `stemjobs.astrochakra.co` account was suspended around 2026-07-21 and the app
> moved to **`stemjobs1`** under a different cPanel user. `.cpanel.yml` derives its target from
> `$HOME`, so it follows whichever account owns the repo and won't need editing if that happens
> again — only the folder name (`stemjobs`) is hardcoded.

---

## 2. Environment

`.env` lives in the app directory on the server and is **read relative to the current working
directory**. Any script that doesn't `cd` here first gets a different backend and reports success
anyway. `DB_REQUIRE` is the guard — set it and a wrong backend fails loudly instead of silently.

### What the app actually needs

| Variable | Where | Why |
|---|---|---|
| `PG_DSN` | cPanel `.env` | The database. `host=127.0.0.1 dbname=astrocha_jobmatch` — loopback only, so this works **only** on the cPanel box. |
| `APP_SECRET` | cPanel `.env` | Session cookie signing. Also the HMAC key for extension bearer tokens, so changing it logs everyone out *and* invalidates every extension install. |
| `GH_TOKEN` | cPanel `.env` | Lets the in-app **Update jobs** button launch the Actions workflow. A GitHub fine-grained PAT scoped to this repo with **Actions: Read and write**. Optional overrides: `GH_REPO=owner/repo`, `GH_WORKFLOW=scrape.yml`. |
| `DB_PROXY_URL` + `DB_PROXY_SECRET` | GitHub secrets, and your laptop | How anything **off** the cPanel box reaches the database. Both or neither; a half-set pair raises rather than falling through to Supabase. |
| `SMTP_HOST` `SMTP_PORT` `SMTP_USER` `SMTP_PASS` `ALERT_TO` | GitHub secrets | The digest email. |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | per-session or `.env` | Resume Brain's AI rewrite. Optional; the offline grader needs neither. |

`SUPABASE_URL` / `SUPABASE_KEY` still appear in `scrape.yml` and are **vestigial** — the database
moved off Supabase on 2026-08-15. Don't add them to anything new.

### Reading production from this laptop

Transport 2, with the local secret:

```bash
DB_PROXY_SECRET="$(tr -d '\r\n' < .db_proxy_secret)" DB_PROXY_URL="https://stemjobs1.astrochakra.co/api/db" python your_script.py
```

`db.using_supabase()` means "is there a remote database at all" and answers **True for all three**
transports — the name is historical. `db.backend_name()` is the one that tells you which.

---

## 3. The schedule — three slots, two runners

| When (ET) | Runner | What it does |
|---|---|---|
| **09:00** Mon–Fri | GitHub Actions — `.github/workflows/scrape.yml`, at the **repo root** | The heavy pass: sweep, **full** score, `verify_dates`, analytics rollup, repost detection, digest email |
| **13:00** Mon–Fri | cPanel cron — `bin/cron_scrape.sh` | sweep, **new-only** score, reposts |
| **16:00** Mon–Fri | cPanel cron — `bin/cron_scrape.sh` | same |

Nothing runs at the weekend: employers don't post then, and Actions minutes are capped.

**The two cron rows live in cPanel → Cron Jobs and nowhere else.** The repo cannot enforce them,
which is why they're recorded in the header of `bin/cron_scrape.sh`:

```
0 13 * * 1-5   /bin/bash $HOME/stemjobs/bin/cron_scrape.sh
0 16 * * 1-5   /bin/bash $HOME/stemjobs/bin/cron_scrape.sh
```

`cron_scrape.sh` is not just `python -m scraper` — it uses `flock -n` so runs skip rather than
stack, truncates its own log at 5 MB (`/home` has run 99% full), `cd`s to the app dir so `.env`
resolves, and uses a gentler worker count and time budget than CI.

> ⚠ **Known broken:** the Actions `DB_PROXY_SECRET` has been wrong since ~2026-08-15, so runs die
> on `HTTP 401 {"error":"bad signature"}`. `verify_dates`, the analytics rollup and the digest have
> not run there since. Repost detection is duplicated into `cron_scrape.sh` and that copy works —
> don't "clean up the duplicate", it's the only path that runs.

### The "Update jobs" button

Shared cPanel can't run a 15–30 minute scrape inside a web request, so the button fires the
Actions workflow and the page just *watches*. Progress goes into a shared `scrape_status` row that
the feed polls, drawing a live bar (boards done, jobs found, elapsed, ETA) and refreshing itself
when the run ends. Phases: `queued → scraping → saving → scoring → done`. A status with no update
for over 3 minutes is treated as finished, so a cancelled run doesn't hang the bar.

Without `GH_TOKEN` the button returns a message telling you to add it; you can still run the
scrape from the repo's Actions tab or wait for cron.

### Keep-warm

Passenger spins the app down after a few idle minutes and the next visitor waits through a
re-import and a cache refill. Measured: a **cold start is 3,230 ms alone, and 4,106-4,131 ms when
four workers start together, against 211 ms warm** (LOAD_SECURITY_QUALITY_REPORT.md §2.3).
Measured again from a laptop against production on 2026-08-20: the same page was **4,017 ms cold
and 307 ms warm**.

**Necessary, but NOT sufficient, and an earlier version of this section overstated it.** Keeping
the process alive avoids the re-import. It does nothing for `web._score_cache`, which is keyed per
(user, résumé) and per PROCESS: the pinger hits `/healthz`, which has no user and builds nothing,
and the pool is 2-6 workers. So even on a permanently warm app the first request each worker
serves for each user paid a full re-score of the corpus — 5-15 seconds, and the cause of a 13.8 s
LCP reported on the live site. That is fixed by the stored score files (`score_cache/`, see
`web.user_scores`), not by this cron. Both are wanted; neither replaces the other.

#### `/warm` — the half that builds something (2026-08-31)

The paragraph above is still true of `/healthz`, and it is why `/warm` exists. Until
`web._base_rows()` landed, the shared work could not be warmed without a logged-in user, because
the row build was keyed per (user, résumé). It is keyed per CORPUS now, so one anonymous call can
fill everything that is the same for everybody:

| stage | what it costs cold |
|---|---|
| `get_jobs()` | 320 ms (snapshot decode) |
| `sponsor_counts()` | 80 ms (3.3 MB) |
| `visa_index()` | 79 ms (2.9 MB) |
| `_logo_manifest()` | 4 ms |
| `_base_rows()` | **~1,400 ms at 21,960 rows** |
| every account's score file | ~2.2 s per user with a résumé (see below) |

**It warms the PER-USER half too, and that is the half that was hurting.** The stored score
files are keyed on (user, résumé) with the corpus fingerprint inside, so **every scrape
invalidates all of them** and the next person to open the feed paid a full scoring pass. Measured
on the live site before this: **46% of feed renders (52 of 114 over a week) took two seconds or
more, median 5.0 s**, with one at 15.7 s. `/warm` now writes every live account's file, so nobody's
first render pays it.

Safe on every tick, by construction: `user_scores` returns the stored file whenever the
fingerprint still matches, so a repeat call costs ~80 ms per user and only does real work in the
one window it exists for -- right after a scrape moved the corpus. Skip it with `&users=0`;
bound it with `WARM_USER_MAX` (default 50).

Measured first render per user, worker warm on shared state, file on disk -- extrapolated to
38,805 rows: **p50 628 ms, p90 879 ms**, against 15,659 ms observed live beforehand.

**Gated on a shared secret, and it 404s without one** — it is seconds of CPU on a shared host, so
an open URL would be a free way to pin a worker. Set `WARM_TOKEN` in `.env` to any long random
string and put the same value in the cron. Point the cron at `/warm`, not `/healthz`: it keeps
the process alive *and* fills the caches, so `/healthz` becomes redundant for this purpose.

```bash
*/5 * * * * curl -fsS -m 60 -o /dev/null "https://stemjobs1.astrochakra.co/warm?t=YOUR_WARM_TOKEN"
```

Note `-m 60`, not `-m 20`: the very first call after a restart does the whole build and a 20 s
timeout would kill it partway. It answers JSON with per-stage milliseconds, so `-o /dev/null` can
be dropped when you want to see where the time goes.

`/healthz` is a public, no-database, two-byte endpoint. If you would rather not put a token in a
cron line, it still prevents the process being spun down:

```bash
*/5 * * * * curl -fsS -m 20 -o /dev/null https://stemjobs1.astrochakra.co/healthz
```

A free external monitor (cron-job.org, UptimeRobot) does the same job and adds downtime alerting,
which cPanel cron cannot; cPanel cron has no third-party account that can lapse. Either works.

Two things worth being precise about:

- `/healthz` **prevents** a cold worker, it cannot warm one; `/warm` warms one. After a deploy
  (`touch tmp/restart.txt`) every worker is cold again and the first visitor pays for it unless
  the cron gets there first — which is the case `/warm` is for.
- Don't point either at `/` — that needs login and does per-user work.

**`_base_rows()` IS persisted, to `row_cache/`, and the argument that said otherwise was wrong.**

This section previously read "deliberately NOT persisted", on the grounds that the saving lands
on the one event this cron prevents. That premise does not hold, and it took a live measurement
to see it: **`/warm` is one HTTP request, so it reaches ONE worker.** Passenger runs several with
no session affinity and recycles them freely, so cold workers keep appearing and each one's first
request paid the full build. Measured 2026-09-01 against production: after warming four workers in
parallel, **three of the next eight probes still hit a cold worker at 6.3-7.0 s.** A cron cannot
win that race. A shared file removes it -- which is exactly why the score files, which are a
shared file, have worked all along.

| a cold worker | before | after |
|---|---|---|
| `_base_rows` | 4,126 ms | **305 ms** (1.4 MB gzip) |

The objection in the old text was the right one, and it is answered in the KEY rather than by
refusing the cache. `_rows_read` validates the corpus fingerprint **and** `_derived_signature()`:
mtime+size of `static/logos/index.json`, `sponsor_counts.json` and `visa_tags.json`, plus a hash
of the two KV maps (`repost_clusters`, `jd_host_verdicts`) that have no file to stat. Change any
of them and the file misses. `_invalidate_jobs()` deletes it outright, because the extension's JD
patch moves neither half of the fingerprint and a file, unlike a process, does not self-heal.

`row_cache/` is disposable and gitignored; `_ROWS_MAX_FILES` bounds it at 3.

**And the rows are built INCREMENTALLY, which is what makes a moving corpus survivable.**
`jobs_fingerprint()` is (row count, max first_seen), so one new posting invalidated the built
rows for 40,000 unchanged ones. Measured at 40,294 rows:

| after a scrape lands | before | after |
|---|---|---|
| a REQUEST | 5,425 ms | **55 ms** (rebuilds only the changed rows) |
| a cold worker | 6,221 ms | **503 ms** (reads the file) |

Reuse is **by value, not by url**, and that is the correctness argument rather than an
optimisation detail: a scrape does not only add rows, it flips `is_active` when a posting closes
and fills `posted_verified`. Reusing a built row because its url looked familiar would show a
closed job as open. Comparing the source dict is 28 ms over the whole corpus and short-circuits
on the first differing key.

**A request persists only when its build was FULL**, and the distinction matters more than it
looks. A worker with no prior to build from (fresh process, or a changed derived signature) does
the whole 7,101 ms build; writing costs ~2,100 ms once and saves every other cold worker all 7 s.
A worker that has a prior is on the ~55 ms incremental path, and writing there would put 2,100 ms
of gzip in front of a request to save almost nobody -- `/warm` has it within five minutes.

Getting that wrong is not theoretical: making requests never persist meant each cold worker
rebuilt independently after a restart, and a real LCP measured **7.36 s**, worse than before the
cache existed.

**THE FIRST LOAD AFTER A DEPLOY IS ALWAYS A FULL BUILD, and that is correct rather than a bug.**
Extracting the zip replaces `static/` wholesale, which moves `static/logos/index.json`'s mtime,
which is in the signature -- so the cache invalidates because the logos genuinely might have
changed. Budget one ~7 s build per deploy and spend it deliberately: hit `/warm` yourself after
`touch tmp/restart.txt` and before opening the feed, rather than letting a page load pay it.

A plain restart with no deploy does NOT invalidate it: the file mtimes are unchanged, so a cold
worker reads the file instead of rebuilding.

`/warm` reports `base_rows.rebuilt` for exactly this reason. On an ordinary tick after a scrape it
should read a few hundred; if it ever reports the whole corpus, the incremental path has stopped
working and the cron log is where that is visible.

---

## 4. Running it locally

```bash
python web.py
```

Serves on :5000. With `PG_DSN` unset and `DB_PROXY_*` set it reads production through the HMAC
proxy — convenient, and worth remembering before you click anything destructive.

`app.py` is the **retired** Streamlit UI. It still boots, which is the trap; nothing imports it and
it is in no deploy list. `npm run streamlit` if you want it for archaeology.

---

## 5. Tests

```bash
python scripts/run_tests.py
```

Runs every offline suite in parallel. The ones that matter day to day:

| Command | When |
|---|---|
| `python scripts/run_tests.py --changed` | after any edit — picks only the suites your diff can affect |
| `python scripts/run_tests.py --only feed` | while iterating on one thing |
| `python scripts/run_tests.py --list` | what exists, grouped, with what each needs |
| `python scripts/run_tests.py --db` | adds the suites needing a live database (refuses without credentials) |

There is no pytest here. Every suite is a plain script and can be run bare:
`python test_title_filter.py`.

**`EV_OFF=1` must be set before anything imports `web`.** The runner does it for you.
`analytics.py` reads that variable once at import, so a suite that imports `web` first will emit
real analytics events — one unguarded parity run wrote 98.8% of all recorded `feed_view` events.

After changing any `.py`, regenerate the docs or CI will fail:

```bash
python scripts/build_docs.py
```

---

## 6. Adding a board

Paste a careers URL into **/add** in the app. It runs the full detect chain (Greenhouse, Lever,
Ashby, SmartRecruiters, Workday, SuccessFactors, Phenom, Oracle, Jibe, Paylocity, PeopleSoft,
UltiPro, JobDiva, Avature, Recruitee, Breezy, BambooHR, Pinpoint, Rippling, Workable, generic
JSON-LD), probes it for real postings, and only then writes a `boards` row. New boards are picked
up by the *next* scrape.

Built-in boards live in `SOURCES` in `scraper/__init__.py` — 1,173 entries as of writing,
composed from 19 named lists. App-added boards come from the `boards` table and are merged on top.

### When an employer has no board

Some sit behind an Akamai bot-wall (Tesla is the classic) where every server-side request — plain
`requests`, a spoofed Chrome TLS fingerprint, headless *and* visible Playwright — gets 403/429.

**Before assuming an employer can't be scraped, run:**

```bash
python scripts/probe_adzuna_replacements.py
```

It walks the full detect chain and has found real SuccessFactors boards for EY, Capgemini and
Birlasoft that nobody had looked for.

For a genuinely bot-walled employer, use the Chrome extension: visit their careers page and click
**Import all jobs on this page**. It reads the live listing from inside your own browser, which
already passed the bot-wall. See [extension/README.md](../extension/README.md).

> **Don't reach for an aggregator.** Adzuna was removed on 2026-08-16. It worked in the narrow
> sense that rows arrived, but its API returns a truncated blurb instead of a description and its
> redirect pages refuse a server-side fetch — so an Adzuna row could never carry a real JD or a
> real apply form. At 6% of the feed it was 38% of every job with no description. The measurements
> are in the comment where `ADZUNA_BOARDS` used to live in `scraper/__init__.py`.
>
> Seven companies still have no board at all as a result: ASML, Deutsche Bank, LTIMindtree,
> Marlabs, Qualcomm, Renesas, Tradeweb. Their `boards` rows still say `ats_type="adzuna"` and are
> inert. Guessed Workday/Greenhouse tenants didn't resolve — each needs its real tenant id.

---

## 7. Adding a user

Accounts are admin-created; there is no public sign-up.

```bash
python manage_users.py add <username> <password>
python manage_users.py list
```

Also `passwd <user> <new>`, `remove <user>`, `resume <user> <file.txt>`. Passwords are stored only
as salted hashes. Or use **/admin/users** in the app, which additionally handles disabling an
account and revoking its extension token.

Each account keeps its own résumé, its own match scores, and its own liked/hidden/applied jobs.
The scraped job pool is shared.

---

## 8. Regenerating the data files

These are **committed source-of-truth**, not caches. The server runs no build step, so whatever is
in the repo is what production uses.

| Command | Produces | Notes |
|---|---|---|
| `python scripts/convert_hub_crosstab.py "Employer Information.csv" --out "../USCIS H-1B Data Hub/raw_csv"` | `h1b_<FY>.csv` | **Run this first.** The USCIS static per-year CSVs stop at FY2023 and that FY2023 file is a *partial year* (33,332 rows against 57,415 today — published four months before FY2023 closed). The only current source is the Hub's Tableau viz → *Crosstab View → Download to Excel → CSV*, which comes out UTF-16, tab-delimited, with different column names and the literal string `Null` for a missing employer. This converts it and **refuses to write a fiscal year that looks truncated**. |
| `python -m scraper.build_sponsor_counts --years 2021-2025` | `sponsor_counts.json`, `sponsor_years.json` | From the converted USCIS CSVs. **Needs `DB_REQUIRE=proxy` + `DB_PROXY_*`** — `our_universe()` reads the jobs table and the boards table through a bare `except`, so without them it resolves against a fraction of the corpus and says nothing. Keep the window **five years wide**: `core.sponsor_strength` tiers on absolute counts, so narrowing it to three cuts the `high` tier from 169 employers to 95. Never include a partial fiscal year. Read the per-FY canary it prints. |
| `python -m scraper.build_visa_tags` | `visa_tags.json`, `visa_tags_report.csv` | From LCA/PERM/E-Verify xlsx. Manual download (dol.gov 403s scripted clients from PowerShell/.NET but serves `curl` fine). Also **needs `DB_REQUIRE=proxy`**: with no corpus it silently drops every employer that only resolves fuzzily — ~1,500 of them, including Walmart and JPMorgan. |
| ~~`python -m scraper.build_sponsors`~~ | ~~`sponsors.txt`~~ | ⛔ **Do not run.** It merges *every* raw `EMPLOYER_NAME` from the LCA file into `sponsors.txt`, which would take it from 630 hand-curated names to tens of thousands. `scripts/build_companies.py::_universe()` feeds all of them into `companies.json` — a **required** shipped asset — so `/companies` would go from 3,093 rows to ~35,000 and `--check` would fail instantly on the 5% unsorted ceiling. The docs used to describe this merge as prescribed-but-never-done; it was never done because it must not be. Edit `sponsors.txt` by hand. |
| `python scripts/build_logos.py` | `static/logos/*`, `static/logos/index.json`, `logo_harvest.json` | Harvests real brand logos from Wikidata P154 and each employer's own site icon, and judges every candidate on its **pixels** rather than its status code. Sequential on purpose and there is no `--workers`: 12 threads measured 84% MISS against 96% paced, and a throttled fetch is recorded as a verdict. Resumable, so an interrupted run costs nothing. `--check` is the CI gate. |
| `python scripts/build_logos.py --audit-domains` | `company_domains_audit.csv` | Stored domain vs Wikidata's curated P856, for review. Writes no JSON. |
| `python scripts/build_logos.py --discover-domains` | `company_domains.json`, `company_domains_discovered.csv` | For the ~800 employers with **no** domain at all, guesses one from the name and then makes the page prove it: `<title>`, `og:site_name` or JSON-LD `name` must corroborate. **Merges**, never rebuilds — entries already in the map are left alone, new ones are tagged `discovered`. **Read the CSV before committing any asset this unlocks.** It is sorted by H-1B filings and carries a `distinctive_tokens` column, because a one-word employer name is the case a page cannot disambiguate: "Alphabet" corroborates perfectly against BMW's `alphabet.com`. That one lives in `DOMAIN_OVERRIDE`. |
| `python scripts/build_logos.py --write-domains` | `company_domains.json` | Rewrites the domain map from P856, keyed on `core.norm_company`. **Replaces `build_company_domains.py`,** whose entire verification was `status_code == 200 and len(content) > 100` -- which accepted `appleinc.com` for Apple and `adp.com` for two employers who merely post through ADP. |
| `python scripts/build_resume_vocab.py` | `resume_vocab.json`, `resume_keywords.json` | |
| `python scripts/build_companies.py` | `companies.json` | ⚠ A **shipped runtime asset** and the data behind `/companies`. **Needs `DB_REQUIRE=proxy` + the `DB_PROXY_*` pair** — without them `db` falls back to `.streamlit/secrets.toml`, the retired Streamlit app's credentials for the Supabase this project left on 2026-08-15, and the build silently omits every employer added since. `--report` prints the sector histogram; `--check` fails if a high-traffic employer is unsorted or the bucket exceeds 5%. |
| `python scripts/build_careers_md.py` | `careers_us.md` | Hand-edited careers/LinkedIn URLs, and a build input to the above. |

`idf.json` (17 MB) is written by the **full** scoring pass only. ⚠ A partial rebuild silently
re-weights every score in the corpus, so don't interrupt one and commit the result.

Disposable and gitignored: `jd_cache.json.gz`, `jdmeta.json`, `jobs_snapshot.json.gz`,
`last_new_jobs.json`, `*_local.json`, the `everify_*` probe CSVs, every `*.log`.

---

## 9. Useful one-offs

| Command | What it does |
|---|---|
| `python scripts/smoke_app.py -v` | Walks every user-facing route through Flask's test client, PASS/FAIL/SKIP each |
| `python scripts/audit_jd_coverage.py` | Census of which jobs have a usable description |
| `python scripts/dump_schema.py` | Regenerates `schema.sql` by asking Postgres to describe itself |
| `python scripts/dump_titles.py` | Every scraped title plus the filter's verdict, no database |
| `python scripts/probe_db_proxy.py` | Read-only check that the HMAC proxy transport works |
| `python scripts/close_dead_jds.py` | Marks postings the employer has taken down |
| `python -m scraper.reposts --write --top 0` | Refreshes repost clusters |

> ⚠ **Never load-test production.** Shared cPanel enforces account-level throttling that no
> restart clears, and the previous account was suspended once.
