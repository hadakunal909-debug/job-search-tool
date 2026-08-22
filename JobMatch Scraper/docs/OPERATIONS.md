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
| `LOGODEV_KEY` | cPanel `.env` | Company logos. Optional — falls back to a letter avatar. |
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

`/healthz` is a public, no-database, two-byte endpoint for exactly this. In cPanel, under
**Cron Jobs**, every five minutes:

```bash
*/5 * * * * curl -fsS -m 20 -o /dev/null https://stemjobs1.astrochakra.co/healthz
```

A free external monitor (cron-job.org, UptimeRobot) does the same job and adds downtime alerting,
which cPanel cron cannot; cPanel cron has no third-party account that can lapse. Either works.

Two things worth being precise about:

- It **prevents** a cold worker, it cannot warm one. After a deploy (`touch tmp/restart.txt`)
  every worker is cold again and the next visitor pays for it regardless.
- Don't point it at `/` — that needs login and does real work.

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
| `python -m scraper.build_sponsor_counts` | `sponsor_counts.json`, `sponsor_years.json` | From DOL/USCIS xlsx. Manual. |
| `python -m scraper.build_visa_tags` | `visa_tags.json`, `visa_tags_report.csv` | From LCA/PERM/E-Verify xlsx. Manual. |
| `python -m scraper.build_sponsors` | `sponsors.txt` | |
| `python scripts/build_company_domains.py` | `company_domains.json` | |
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
