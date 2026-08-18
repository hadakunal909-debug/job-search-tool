#!/bin/bash
# Hourly scrape, run by cPanel cron on the same box as the app and the database.
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
    echo "----- $(date -u +%FT%TZ) score start -----" >> "$LOG"
    "$PY" -u -m scraper.score_jobs >> "$LOG" 2>&1
    echo "----- $(date -u +%FT%TZ) score end rc=$? -----" >> "$LOG"
fi

exit 0
