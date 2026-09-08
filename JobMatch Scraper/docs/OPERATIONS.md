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
4. **Warm it**, before you open the feed:

```bash
curl -fsS -m 300 "https://stemjobs1.astrochakra.co/warm?t=$WARM_TOKEN"
```

Step 4 is not optional politeness. A restart empties every *per-process* cache the app has —
the ~29 MB IDF table, the live-analysis memo, `_score_cache`, `_base_rows_cache` — and the first
person to open the feed rebuilds all of it on their own request.

**And a deploy is not the only thing that spawns a cold worker.** `stderr.log` on the box is a
list of `killed by signal: 9` — Passenger children hitting the account's LVE memory cap and
being replaced. Measured 2026-09-06: worker `3884533` started at 00:49:51 UTC and the first feed
render it served, at 00:53:11, took **17,216 ms** against a steady state of 80–170 ms. Nobody had
deployed for three and a half hours. So step 4 closes the deploy case, the five-minute cron
closes the recycle case, and neither is redundant. `/warm` pays it, off everybody's path. See
[`/warm`](#warm--the-half-that-builds-something-2026-08-31); the token is `WARM_TOKEN` in the
server's `.env`, the same value as the `?t=` in the keep-warm crontab line.

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

## 3. The schedule — four slots, two runners

| When (ET) | Runner | What it does |
|---|---|---|
| **~08:00-09:00** Mon–Fri | GitHub Actions — `.github/workflows/scrape.yml`, at the **repo root** | The heavy pass: sweep, **full** score, `verify_dates`, analytics rollup, repost detection, digest email |
| **13:00** Mon–Fri | cPanel cron — `bin/cron_scrape.sh` | sweep, **new-only** score, reposts, `scraper.score_users` |
| **16:00** Mon–Fri | cPanel cron — `bin/cron_scrape.sh` | same |
| **:30, hourly** Mon–Fri | cPanel cron — `bin/cron_scrape.sh --analyze-only` | analyse only: writes `jd_terms`, then refills the stored user scores |

Nothing runs at the weekend: employers don't post then, and Actions minutes are capped.

**Every time in this file is stated in ET and stored in UTC, and that gap has bitten twice.** The
box's local time IS UTC (`date` and `date -u` print the same thing), so a crontab line is written
in UTC while this table reads in ET. An earlier version of this section gave the lines as `0 13`
and `0 16` "in the server's local time" — both halves wrong in the same direction, firing three
and four hours early. A wrong schedule does not announce itself: the run succeeds, just not when
anyone expected it.

**The cron rows live in the live crontab and nowhere else.** The repo cannot enforce them, which
is why they are also recorded in the header of `bin/cron_scrape.sh`. Read the real one:

```bash
ssh -i ~/.ssh/id_ed25519_cpanel astrocha@stemjobs1.astrochakra.co "crontab -l"
```

As of **2026-09-07** that returns:

```
0 17,20 * * 1-5   /home/astrocha/stemjobs/bin/cron_scrape.sh
30 * * * 1-5      /home/astrocha/stemjobs/bin/cron_scrape.sh --analyze-only
*/5 * * * *       flock -n .../tmp/cron_scrape.lock true && \
                    for i in 1 2 3 4; do curl -fsS -m 240 -o /dev/null \
                      "https://stemjobs1.astrochakra.co/warm?t=..."; done
```

**THE rc=137 WAS NEVER THE SWEEP LEAKING.** Measured 2026-09-07: before a sweep starts the
account is already carrying **921 MB** -- 595 MB of warm `lswsgi` workers holding the corpus,
IDF and row cache, plus 326 MB of a sibling app that is not ours to touch. A complete sweep
peaks at 617 MB. 921 + 617 = 1,538 against a ~1.2 GB CloudLinux LVE cap, so it dies: eleven
sweep kills and seven score kills in twelve days, and they are the ONLY failures in the log.

A restarted worker is under 20 MB, which makes that sum 963 and fits. So `cron_scrape.sh`
touches `tmp/restart.txt` before the sweep and lets its own warm step rebuild afterwards.
`--analyze-only` deliberately does not: it runs hourly and needs little memory, and
restarting hourly would keep the site permanently cold.

The `flock` guard is the other half. Four warm calls every five minutes is right when nothing
else is running -- one call warms one worker, and there are two. During a 60-minute sweep it
is twelve rounds of re-fattening the workers the recycle just emptied. `flock -n <lock> true`
tests the scrape's own lock and releases immediately; it must NOT hold the lock across the
warm, because `cron_scrape.sh` uses `flock -n` too and would then skip its entire slot.

**Do not go looking for this in ulimits.** `_release_memory()`'s note records that `ulimit
-v`/`-m` both report unlimited and /proc/lve is unreadable, and that cutting SCRAPE_SLICE
from 150 to 100 died at the same ceiling. The cap is per-account and invisible from inside it.

```
```

**THE LOOP IS NOT REDUNDANCY -- one call warms one WORKER.** The app runs two `lswsgi` workers
and LiteSpeed recycles them freely, so a single ping left a standing chance that a visitor
landed on a cold one -- and a cold worker rebuilds the corpus, the IDF and the live analysis
before it can render, which is ~4 s. Measured 2026-09-07: 1 of 8 consecutive `/warm` calls took
4,111 ms while the other 7 took ~375 ms. After looping it four times, 10 consecutive checks all
returned 361-504 ms with `jobs.ms = 0`.

Which is also how to READ a `/warm` response: a low `total_ms` only proves the worker that
answered is warm. `jobs.ms = 0` means that worker holds the corpus; a few hundred ms means it
just fetched it.

**BACK THE CRONTAB UP BEFORE EDITING IT, AND DO NOT EDIT IT WITH `sed` OVER SSH.** On
2026-09-07 `crontab -l | sed "s|...\\(curl .*\\)$|...\\1...|" | crontab -` wrote an **empty
crontab**: the backslash backreferences did not survive the SSH transit, and both the scrape job
and the warm ping vanished silently -- `crontab -l` simply returned nothing. Recovered only
because the step before it was:

```bash
crontab -l > ~/crontab.backup.$(date +%Y%m%d_%H%M)
```

Take that backup first, every time. Then make the edit with a Python script uploaded to the box
(read via `crontab -l`, transform in Python, write via `crontab -`) and have it refuse to write
if the input was empty, if the scrape entry is missing from the result, or if the line count
changed. No shell quoting, and three guards.

> **INSTALLED 2026-09-07.** It ran unadded for three days after being documented, which is why
> the analyse pass kept reporting PARTIAL. The line is:
>
> ```
> 30 * * * 1-5   /home/astrocha/stemjobs/bin/cron_scrape.sh --analyze-only
> ```
>
> **Why it matters:** the feed reads `jd_terms`, not `jd`. The sweep writes the description; only
> the analyse pass writes the packed analysis, and a card with no analysis shows "Not scored"
> however good the description behind it is. Measured 2026-09-04 at two slots a day: 461 active
> rows unanalysed, **278 of them holding a perfectly readable description**. A job found at 20:20
> waited until the next afternoon for a number the job *page* could already compute on demand.
> The pass costs one corpus + IDF load against a 4-minute budget and takes the same lock as the
> full run, so it skips rather than stacks.

### Three sidecar workflows, all at the repo root

| Workflow | Schedule | What it is for |
|---|---|---|
| `scrape-watchdog.yml` | hourly | Dispatches `scrape.yml` if the scheduled event never arrived |
| `jobspy-sweep.yml` | `0 16 * * *` daily | The aggregator sidecar; writes a findings spreadsheet as an artifact |
| `jobspy-shadow.yml` | on demand | Compares an aggregator's results against our own corpus |

**A cron is not a guarantee, and that is measured, not theoretical.** `scrape.yml` asks for
`47 12 * * 1-5`; the first fire after the requested slot has run +43 min, +9h42, +6h19, +3h56 —
and on **2026-09-04 it never fired at all**, while push-triggered runs on the same repository
started normally the same morning. Actions was enabled, the workflow was active, minutes were
available; only the scheduled event was dropped. Nothing inside `scrape.yml` can fix that,
because `scrape.yml` never ran — which is the entire reason `scrape-watchdog.yml` exists.
`workflow_dispatch` is delivered immediately rather than queued.

**Indeed is a live source in Actions only.** `scrape.yml` sets `JOBSPY_SITES: indeed`. It is not
enabled on the cPanel box because that venv is Python 3.9 and the library ships no build for it.

`cron_scrape.sh` is not just `python -m scraper` — it uses `flock -n` so runs skip rather than
stack, truncates its own log at 5 MB (`/home` has run 99% full), `cd`s to the app dir so `.env`
resolves, and uses a gentler worker count and time budget than CI.

> **Fixed 2026-08-21, kept because the failure mode is worth recognising.** The Actions
> `DB_PROXY_SECRET` was a different value from the working one and failed every scheduled run
> with `401 {"error":"bad signature"}` from ~2026-08-15 — so `verify_dates`, the analytics
> rollup and the digest silently did not run for a week. Re-pasted and verified: 147 rows carry
> a `posted_verified` date of 2026-09-04, which only the Actions heavy pass writes. If a run
> fails again, **check the failing STEP before assuming this secret**, and use
> `scripts/probe_db_proxy.py` to diagnose a 401 rather than guessing.
>
> Repost detection is deliberately duplicated into `cron_scrape.sh`. Don't "clean up the
> duplicate" — both paths are wanted, and during that week the cron copy was the only one running.

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

**Superseded in part on 2026-09-04:** the per-(user, job) score now lives in the `user_scores` TABLE, so it survives a restart, a new worker and a different device rather than being re-derived per process. `web.user_scores` SEEDS from that table and computes only what is missing, so the file cache above is still the second tier and this section still describes the cold path correctly — there is just far less of it left.

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
| `core.load_idf()` | **~915 ms** — one `json.load` of the ~29 MB `idf.json` |
| `_live_analysis()` | up to 1.5 s (its own wall-clock budget) |
| every account's score file | ~2.2 s per user with a résumé (see below) |

**The last two stages were added 2026-09-05 and behave differently from the rest.** Everything
else here leaves a file behind (`row_cache/`, `score_cache/`) that any worker can read, so one
call warms them all. `load_idf` and `_live_analysis` are held in module globals with no file, so
they warm **only the worker that answered the call** — the five-minute cron is what reaches the
others. Weaker, and still worth having: Passenger keeps a worker for hours, and without them a
cold worker's first `/` measured **2,229 ms against 44 ms warm**, of which `cProfile` put 915 ms
in `load_idf` alone. With them the same first render is **433 ms**. `load_idf` is listed
separately because `_live_analysis` only reaches for it when it has rows to analyse, so on a
quiet corpus it would otherwise stay unpaid until a real visitor arrived — and `/job` and the
tailor routes load it through their own call sites too.

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
*/5 * * * * for i in 1 2 3 4; do curl -fsS -m 60 -o /dev/null \
              "https://stemjobs1.astrochakra.co/warm?t=YOUR_WARM_TOKEN"; done
```

Four calls, not one, because one call warms one worker -- see the crontab section above for the
measurement. Once they are warm the extra calls cost ~375 ms each.

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
`jobs_fingerprint()` is (row count, max first_seen, scored count), so one new posting — or one
flush of the score pass, which banks every `SCORE_JD_WRITE_CHUNK` (2,000) rows — invalidated the
built rows for 40,000 unchanged ones. Measured at 40,294 rows:

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

**A DEPLOY NO LONGER ALWAYS FORCES A FULL ROW REBUILD -- but it always empties the per-process
caches.** This paragraph used to say the first load after a deploy is always a full build,
because extracting the zip moves `static/logos/index.json`'s **mtime**. `_derived_signature()`
hashes file **content** now, memoised on the stat, so a release that changes only code and
templates leaves `row_cache/` valid: verified on the 2026-09-05 release, no rebuild window at
all. What a restart *does* invalidate unconditionally is everything held in a module global --
the IDF table, the live-analysis memo, `_score_cache` -- and that is still a first visitor
paying ~2 s, or ~12 s when the fingerprint moved as well. So the advice is unchanged and the
reason is narrower: hit `/warm` after `touch tmp/restart.txt` and before opening the feed
(step 4 of the deploy recipe), rather than letting a page load pay it.

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

Built-in boards live in `SOURCES` in `scraper/__init__.py` — **1,218** entries, composed from 19
named lists. App-added boards come from the `boards` table and are merged on top; that table holds
**991**, so the effective source count is **~2,209**, not 1,218.

Both numbers are measured, 2026-09-08 (`len(scraper.SOURCES)` and `db.table_count("boards")`).
They were 1,192 and 915 here and had drifted without anyone noticing, which is the reason to say
how to re-read them rather than to restate them: a count in prose is stale the next time a board
is adopted, and a stale number that reads like hard-won knowledge costs more than no number.

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

### Finding boards in bulk — the discovery pipeline

`/add` is one board at a time. To find employers we don't cover yet, start from a live posting
feed, keep only the employer NAME, and scrape that employer's own board. The posting text is
thrown away, which is what keeps the "don't reach for an aggregator" rule above intact: an
aggregator is a discovery *channel* here, never a feed source.

Four stages plus a review, none of which writes to the database except the last:

```bash
# 1+2  harvest and screen -> discovered_companies.csv
python -u scripts/discover_companies.py --channel linkedin,indeed --phrases all --hours 168 -v

# 3    probe each net-new name for a real board -> discovered_board_probe.csv
python scripts/probe_discovered.py --csv discovered_companies.csv --min-pm 1

# 3.5  read what each board actually contains -> candidate_review.csv
python scripts/review_candidates.py --csv discovered_board_probe.csv

# 4    grade identity, apply the blocklist and the vetoes. --dry-run writes nothing.
python -m scraper.adopt_everify_boards --csv discovered_board_probe.csv --dry-run \
  --added-by discover:li+indeed:YYYY-MM-DD --out discovered_adoption.csv
```

`--added-by` tags the batch so **rollback is one statement** —
`DELETE FROM boards WHERE added_by = '<tag>'`. Always pass it; it was hardcoded once and one
pipeline's dry run silently overwrote another's review artifact.

**Run both channels.** LinkedIn and Indeed overlap by only ~6% of employers, so they are close
to disjoint. Google returned 0 rows when tested and was dropped — verify it per-run rather than
assuming. Measured funnels, for calibration:

| Run | Postings | Employers | Net-new | Probed | Boards | Adopted |
|---|---|---|---|---|---|---|
| 2026-08-30 LinkedIn, 24 phrases | 2,368 | 1,406 | 1,092 | 943 | 153 (16.2%) | 125 |
| 2026-08-31 + Indeed | 4,184 | 1,933 | 1,480 | 1,163 | 120 (10.3%) | 85 |
| 2026-09-02 same shape | 4,512 | 1,977 | 1,494 | **720** | 113 (15.7%) | 75 |

**Skip the names you have already probed.** A 168h window run days after another re-surfaces the
same employers, and re-probing one cannot return a different answer unless the detect chain
changed. Union the `board_url`-empty rows of every probe CSV written since the last detection
fix — 3,214 names on 2026-09-02 — and drop them before stage 3. That cut the probe list by a
third and is why the 09-02 rate reads higher than 08-31's: the denominator no longer carries
last week's known dead ends.

**Two things the automated gates cannot decide, both of which have shipped a bad board:**

- **The titles, not the ratio.** `relevance_yield` auto-rejects only at ZERO survivors, so
  Domino's passed at 1 of 1,000 sampled against 24,663 postings. And it never samples a board
  under `YIELD_CHECK_MIN_POSTINGS = 500` at all. Run `scripts/review_candidates.py`, read
  `top_title_share`, and read the titles themselves — concentration beats ratio. Tapestry kept
  1.1% and every survivor was a distinct HQ role (keep); EoS Fitness kept 7.6% and 99% of those
  were one repeated store title (reject).
- **The tenant.** A real US employer can resolve to its FOREIGN tenant and grade `confirmed` —
  Conagra to `Careers_CAN`, Chart Industries to a board that read 8 rows, all Czech. Both were
  adopted and removed. `foreign_share` only vetoes when *every* posting is abroad.

**The blocklist is where a rejection is recorded, once** — `db.add_blocked(name, reason,
added_by)`, 38 entries, each carrying its measured reason, and `db.remove_blocked(name_key)`
undoes it. Adopt consults it, so blocklist *before* adopting and a bad row cannot slip through.
Watch the key: `block_key` keeps legal suffixes and turns `Domino's` into `domino s`, which does
**not** match `Dominos` — that spelling gap let previously-blocked Ulta and AutoZone be
re-adopted. Block both spellings and verify with `db.is_blocked`.

**Tooling traps that have each cost an hour:**

- `discover_companies.py` **banks nothing until every phrase completes**, so a kill loses the
  whole harvest. 24 phrases x 2 channels is ~60-90 minutes. Budget for it.
- jobspy logs `finished scraping` **once per process, not per query**, so it is useless as a
  progress counter. Run with `python -u` and count the per-phrase lines instead.
- A phrase whose titles can never pass `title_verdict` contributes nothing: "solutions
  architect", "cloud architect" and "product designer" are all excluded as off-target
  functions. Check a new phrase through `title_verdict` before adding it.

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
| `norms.json` | `python scripts/build_norms.py` | what each role usually asks for and which tools each employer leans on. Reads `jd_terms` from the database, so run it AFTER a scoring pass. Prevalence difference, not lift; employer-capped; the company half is tools only. `--check` gates it, `scripts/test_norms.py` guards the shares. |
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
| `python scripts/review_candidates.py --csv <probe or adopt csv>` | Fetches each board and reports kept/fetched, US survivors, distinct titles and the top title's share — the judgement adopt's yield check can't make, and the only one that looks at boards under 500 postings |
| `python scripts/dump_schema.py` | Regenerates `schema.sql` by asking Postgres to describe itself |
| `python scripts/dump_titles.py` | Every scraped title plus the filter's verdict, no database |
| `python scripts/probe_db_proxy.py` | Read-only check that the HMAC proxy transport works |
| `python scripts/close_dead_jds.py` | Marks postings the employer has taken down |
| `python -m scraper.reposts --write --top 0` | Refreshes repost clusters |

> ⚠ **Never load-test production.** Shared cPanel enforces account-level throttling that no
> restart clears, and the previous account was suspended once.
