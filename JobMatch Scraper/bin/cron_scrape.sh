#!/bin/bash
# Scheduled scrape, run by cPanel cron on the same box as the app and the database.
#
# THE SCHEDULE, because cPanel keeps it in a web form and nowhere else. Two slots, Mon-Fri, in
# the SERVER's local time — paste these into cPanel -> Cron Jobs, one row each:
#
#   0 13 * * 1-5   /bin/bash $HOME/stemjobs/bin/cron_scrape.sh
#   0 16 * * 1-5   /bin/bash $HOME/stemjobs/bin/cron_scrape.sh
#
# GitHub Actions covers the 09:00 slot (the heavy pass — verify_dates, analytics, the digest);
# see .github/workflows/scrape.yml at the repo ROOT. Three runs a weekday in total, and none at
# the weekend: employers do not post then, and Actions minutes are capped.
#
# It said "Hourly" here until 2026-08-20 and had not been hourly since the database moved to
# cPanel. Two runs a day is the real cadence, which matters because every budget below was
# sized against it.
#
# WHY THIS IS NOT JUST `python -m scraper`. Three things bite on shared hosting, and all three
# are silent:
#
#   * OVERLAP. The run is budgeted, not bounded — a slow sweep can outlast its hour. Two
#     scrapers at once means double the outbound connections from one account, which is the
#     shape of activity that gets a shared account suspended. flock makes a late run skip its
#     slot instead of stacking on top of the previous one.
#   * DISK. /home is 99% full (42 GB free of 3.5 TB). A log appended nine times a day forever is
#     a slow leak on a volume that has no room for one, so it is truncated when it passes 5 MB.
#   * ENVIRONMENT. cron gets almost none of it. db.py reads .env RELATIVE TO THE WORKING
#     DIRECTORY, so without the cd this runs with no database configuration at all and silently
#     falls back to jobs.csv — it would look like it worked and write nothing.
#
# Deliberately gentler than the GitHub Actions run: 6 workers rather than 16 and a 12-minute
# budget rather than 22. CI is a machine of its own for 20 minutes; this is a shared box that
# also has to serve the website while it runs.
#
# PG_DSN comes from .env, so this talks to Postgres over the loopback — no proxy hop, since the
# database is on this machine.

set -u
APP="$HOME/stemjobs"
PY="$HOME/virtualenv/stemjobs/3.9/bin/python"
LOG="$APP/logs/cron_scrape.log"
LOCK="$APP/tmp/cron_scrape.lock"

mkdir -p "$APP/logs" "$APP/tmp"

# 5 MB ceiling, checked before the run so the log always holds the MOST RECENT runs rather than
# the oldest ones. One truncation loses history; an unbounded file loses the website.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 5242880 ]; then
    tail -c 1000000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

cd "$APP" || { echo "$(date -u +%FT%TZ) FATAL: no $APP" >> "$LOG"; exit 1; }

# -n: fail immediately rather than queue. A run that cannot get the lock should be skipped, not
# delayed into the next hour's slot, which would just move the collision.
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date -u +%FT%TZ) SKIP: previous run still going" >> "$LOG"
    exit 0
fi

export SCRAPE_WORKERS=6
export SCRAPE_BUDGET_MIN=12
export MAX_AGE_DAYS=30
export PRUNE_DAYS=30
export DISCOVER_LIMIT=20          # board auto-discovery, small bite per run
# CLOSED-POSTING RETIREMENT, ON. It defaulted to a dry run and the variable was never set
# anywhere, so in the whole life of the project it has only ever printed what it would do --
# leaving 5,178 rows sitting under the miss threshold and 404 postings reading as open. The
# guards that made it worth being careful about are all in reconcile_closed and all still
# apply: a failed fetch proves nothing, a board returning a small fraction of what we store is
# skipped as having a bad day, and a posting must be missing three runs running before it is
# marked closed. Rows are never deleted and Saved/Applied are unaffected.
export RECONCILE_CLOSED=1

# STATE THE BACKEND. db.py falls back through PG_DSN -> DB_PROXY_* -> Supabase -> jobs.csv, and
# every fallback is SILENT. The failure that matters here is .env not being read: db.py loads it
# relative to the WORKING DIRECTORY, so if the cd above ever stops landing in $APP, PG_DSN comes
# back empty and this run writes to Supabase (a database the app stopped reading on 2026-08-15)
# or to jobs.csv, reports success, and the feed just quietly stops updating. That has happened
# once already for a different reason — see the PG_DSN-read-before-.env note in db.py.
#
# With this set, db._check_backend_intent raises immediately instead. A run that cannot reach the
# real database should fail: the sweep is stateless and the next slot re-scrapes from the boards,
# so a failed run costs a cycle and loses nothing.
export DB_REQUIRE=pg

echo "===== $(date -u +%FT%TZ) scrape start =====" >> "$LOG"
"$PY" -u -m scraper >> "$LOG" 2>&1
rc=$?
echo "===== $(date -u +%FT%TZ) scrape end rc=$rc =====" >> "$LOG"

# Score only what the sweep just found. Without this the new rows carry no description and no
# match score, so they are invisible to the feed's filter — the sweep alone is half a job.
# new-only mode reuses the stored IDF instead of re-reading every description.
if [ $rc -eq 0 ] && [ ! -f "$APP/resume.txt" ]; then
    # score_jobs aborts without this file and EXITS 0, so the run logs a success and silently
    # never scores. That is how the first cron test looked fine: scrape end rc=0, score start
    # and score end on the same second, nothing in between. Say it loudly instead — an
    # unscored job carries no match score and is invisible to the feed's filter, so a scraper
    # that cannot score is doing half a job while reporting a whole one.
    echo "$(date -u +%FT%TZ) WARNING: no resume.txt — SCORING DISABLED, new jobs will show" \
         "as 'JD pending' with no match score. Deploy resume.txt to $APP." >> "$LOG"
fi

if [ $rc -eq 0 ] && [ -f "$APP/resume.txt" ]; then
    export SCORE_NEW_ONLY=yes
    export SCORE_MAX_FETCH=400
    export SCORE_BUDGET_MIN=8
    # Bounds the ANALYSIS, which SCORE_BUDGET_MIN above does not — that one stops the JD fetch.
    # core.job_meta is ~206 ms/row, so the 1,754 new rows of a big sweep are ~6 minutes of solid
    # CPU on a shared box that is also serving the website. New-only leaves an unreached row's
    # match_score NULL and _new_only_targets picks it up on the next run, so cutting this off
    # costs a delay, never a score.
    export SCORE_ANALYZE_BUDGET_MIN=4
    echo "----- $(date -u +%FT%TZ) score start -----" >> "$LOG"
    "$PY" -u -m scraper.score_jobs >> "$LOG" 2>&1
    echo "----- $(date -u +%FT%TZ) score end rc=$? -----" >> "$LOG"
fi

# The repost-cluster map the feed card's "Posted Nx" badge reads. Derived from the corpus, so it
# runs after the sweep; measured at 28 s over 25,180 rows for 528 keys, which is affordable even on
# a shared box. --top 0 prints no cluster listing, only the summary, because this log is truncated
# at 5 MB and a 528-line table nine times a day is how that ceiling gets hit.
#
# `-m scraper.reposts`, not scripts/: .cpanel.yml copies scraper/ but NOT scripts/, so the module
# form is the only one that exists on this box.
#
# IT LIVES HERE AS WELL AS IN THE ACTIONS WORKFLOW ON PURPOSE. The Actions DB_PROXY_SECRET has been
# wrong since ~2026-08-15, so every later step of the 09:00 ET run fails `401 bad signature`. This
# cron runs on the same box as the database and talks to it over the loopback via PG_DSN, so it is
# the path that actually works today. When the secret is fixed both will refresh it, which is
# harmless: the write is idempotent and replaces the whole row.
#
# Not gated on the score step: the clustering reads url/title/company/location/first_seen and needs
# neither a description nor a match score. Gated on the SWEEP, because clustering a corpus the
# sweep failed to update would just republish yesterday's map under today's date.
if [ $rc -eq 0 ]; then
    echo "----- $(date -u +%FT%TZ) reposts start -----" >> "$LOG"
    "$PY" -u -m scraper.reposts --write --top 0 >> "$LOG" 2>&1
    echo "----- $(date -u +%FT%TZ) reposts end rc=$? -----" >> "$LOG"
fi

exit 0
