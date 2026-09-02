#!/bin/bash
# Scheduled scrape, run by cPanel cron on the same box as the app and the database.
#
# THE SCHEDULE. Two slots, Mon-Fri. `crontab -l` over SSH reads the LIVE one -- cPanel's web
# form is a view onto it, not the only copy, which an earlier version of this comment claimed:
#
#   0 17,20 * * 1-5   /home/astrocha/stemjobs/bin/cron_scrape.sh
#
# THAT IS UTC, WHICH IS THIS BOX'S LOCAL TIME -- `date` and `date -u` print the same thing. So
# 17 and 20 UTC are 1pm and 4pm EDT (noon and 3pm under EST, the same DST drift scrape.yml
# documents and accepts).
#
# THIS COMMENT USED TO GIVE THE LINES AS `0 13` AND `0 16` "in the SERVER's local time", and
# both halves of that were wrong in the same direction: pasted literally they fire at 9am and
# noon ET, three and four hours early. Wrong schedules of this shape do not announce
# themselves -- the run succeeds, just not when anyone expected it. The live crontab was
# separately stuck at 21 UTC (5pm ET) until 2026-08-25, five days after 562b63f moved the
# documented slot to 4pm, because the crontab is a THIRD place the schedule lives and only the
# other two were edited.
#
# GitHub Actions covers the 09:00 ET slot (the heavy pass -- verify_dates, analytics, the
# digest); see .github/workflows/scrape.yml at the repo ROOT. Three runs a weekday in total,
# and none at the weekend: employers do not post then, and Actions minutes are capped.
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
# 12 -> 0. NO DEADLINE: scrape_all reads 0 as 'no cut' and dispatches every board. The 12
# minutes was sized against ~1,265 boards; the list is now 1,802, so it starved most of them
# every slot, and the daily rotation only changes WHICH ones get dropped.
#
# WORKERS STAY AT 6, deliberately. This box also serves the website, and the concurrency -- not
# the duration -- is what makes shared hosting throttle an account. Removing the deadline makes
# the run LONGER, not wider: expect roughly 80 minutes where CI takes 30 at 16 workers.
#
# That is still inside the gap between the 13:00 and 16:00 slots, and flock above is what makes
# an overrun safe anyway -- a late run skips its slot instead of stacking a second sweep on top
# of the first. If the run ever does outlast the gap, the log will say SKIP rather than double
# the outbound connections, which is the failure mode worth protecting.
export SCRAPE_BUDGET_MIN=0
# SLICING, and this is the line that makes the one above survivable HERE.
#
# Removing the deadline was right about the sweep and wrong about the box: on 2026-08-24 the
# first two unsliced runs were both SIGKILLed (rc=137), the second after a COMPLETE 48.4-minute
# sweep of 1,736 boards. Neither banked a single row, because the scraper held every posting in
# memory and wrote once at the very end -- so a kill anywhere before that line cost the whole
# run. The 12-minute budget had been hiding that by never letting the sweep get big enough.
#
# 150 BOARDS A SLICE, AND THE NUMBER IS MEASURED. A 100-board slice scanned 78,926 postings on
# 2026-08-24, so the unsliced 1,802-board sweep was holding roughly 1.4 MILLION posting dicts
# plus their descriptions before it wrote anything -- which is the rc=137 in one line. 150 keeps
# the peak near the size that has actually been observed to survive here, at ~12 slices of ~4
# minutes. An earlier 300 was a guess made before that count existed; smaller slices cost only
# extra round trips, and buy a smaller loss when a run is killed.
# 150 -> 100 on 2026-08-31, after two consecutive runs were SIGKILLed again -- the cron mailed
# "line 131: 177876 Killed" and the same for a second pid. Same failure as 2026-08-24, and the
# reason it came back is above: 150 was measured against a 1,802-board list and the list is now
# 1,947. Most of that growth is one batch -- 81 boards adopted from the ranked-sponsor probe on
# 2026-08-31 -- so this is the cost of that batch, not drift. 100 is not a guess either: the
# note above records a 100-board slice scanning 78,926 postings on the day the slicing was
# added, which is the largest peak this box has been observed to survive.
#
# WORKERS ARE DELIBERATELY NOT TOUCHED. The note above says 6 is a throttling decision about
# concurrency, not a memory one, and lowering it would trade a documented judgement for an
# undocumented guess. Slice size is the lever that bounds the peak.
export SCRAPE_SLICE=100
# Bounds the DESCRIPTION LOOKUP, which SCRAPE_BUDGET_MIN above does not -- that one stops the
# board sweep. Same shape as the SCORE_BUDGET_MIN / SCORE_ANALYZE_BUDGET_MIN pair below. 3 rather
# than the 2 CI uses: there is no step timeout out here, only the gap to the next cron slot.
export JD_LOOKUP_BUDGET_MIN=3
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
#
# NOT GATED ON $rc, AND THAT GATE WAS THE SINGLE LARGEST SOURCE OF "JD pending" ROWS.
#
# It read `[ $rc -eq 0 ] && [ -f resume.txt ]`, which sounds careful and is exactly backwards.
# SCRAPE_SLICE banks every slice before the next one starts (that is the whole point of the
# 2026-08-24 slicing fix), so a sweep killed at slice 3 of 21 has ALREADY written its jobs to
# the database. Refusing to score them does not undo the sweep; it just leaves the rows that
# did land with a NULL match_score and no jd_terms, which the feed renders as "JD pending"
# with no number. The next run then re-scrapes those same postings, sees them in `seen`, and
# skips them — so nothing ever comes back for them except the arrears pass.
#
# MEASURED 2026-09-01: eight of the last nine cron runs exited rc=137 (SIGKILL at the ~1.2 GB
# CloudLinux LVE cap), so eight consecutive scoring passes were skipped. That left 1,539 rows
# with a NULL match_score, every one of them first_seen that day — and 1,135 of those already
# HELD a full description in the jd column. The text was sitting there and nothing analysed it.
#
# .github/workflows/scrape.yml hit the identical bug on 2026-08-21 and fixed it with
# `!cancelled()` on the steps after the scrape. This is that fix, out here.
#
# Scoring a corpus the sweep did not manage to add to is cheap and idempotent: new-only mode
# reuses the stored IDF, and _new_only_targets falls back to the NULL-match_score backlog when
# last_new_jobs.json is missing or truncated, so a killed sweep still hands it real work.
if [ ! -f "$APP/resume.txt" ]; then
    # score_jobs aborts without this file and EXITS 0, so the run logs a success and silently
    # never scores. That is how the first cron test looked fine: scrape end rc=0, score start
    # and score end on the same second, nothing in between. Say it loudly instead — an
    # unscored job carries no match score and is invisible to the feed's filter, so a scraper
    # that cannot score is doing half a job while reporting a whole one.
    echo "$(date -u +%FT%TZ) WARNING: no resume.txt — SCORING DISABLED, new jobs will show" \
         "as 'JD pending' with no match score. Deploy resume.txt to $APP." >> "$LOG"
fi

if [ -f "$APP/resume.txt" ]; then
    export SCORE_NEW_ONLY=yes
    # 400 -> 1500, AND 8 -> 25 BELOW, because the sweep fix changed the arithmetic these two
    # were sized against. Under the old 12-minute deadline a run swept ~500 boards and added a
    # few hundred rows, so a 400-row bite drained the backlog faster than it grew. A complete
    # sweep adds ~5,000. On 2026-08-25 that left 6,441 rows with no description against a drain
    # of 800 a weekday -- a backlog that DIVERGES, and since a row with no JD scores 0, every
    # one of them is also invisible to the feed's match filter.
    #
    # 1500 x 2 slots = 3,000 a weekday, which roughly keeps pace with intake and pays the
    # arrears down slowly. Not higher: much of the backlog is unfetchable rather than unfetched
    # (a measured pass once attempted 2,640 detail fetches for 8 usable JDs, each dead URL still
    # costing a timeout), so past a point this buys timeouts rather than descriptions.
    export SCORE_MAX_FETCH=1500
    # 8 -> 25. The cap bounds how much we bite off, this bounds how long we chew, and the fetch
    # is mostly waiting on other people's servers. The sweep now runs ~75 min and the gap to the
    # next slot is 3 hours, so ~100 min total still leaves well over an hour of headroom.
    export SCORE_BUDGET_MIN=25
    # Bounds the ANALYSIS, which SCORE_BUDGET_MIN above does not — that one stops the JD fetch.
    # core.job_meta is ~206 ms/row, so the 1,754 new rows of a big sweep are ~6 minutes of solid
    # CPU on a shared box that is also serving the website. New-only leaves an unreached row's
    # match_score NULL and _new_only_targets picks it up on the next run, so cutting this off
    # costs a delay, never a score.
    # 4 -> 12, raised WITH the fetch cap above rather than after someone notices. core.job_meta
    # is ~206 ms/row, so 4 minutes analyses ~1,150 rows -- less than the 1,500 now being fetched,
    # which would just move the bottleneck one step down and leave the extra JDs sitting
    # unanalysed. 12 minutes covers the fetch plus a bite of the unscored arrears.
    export SCORE_ANALYZE_BUDGET_MIN=12
    # The sweep's rc rides along on this line so a killed sweep stays visible in the log even
    # though it no longer stops the scoring. Grepping "score start" now tells you scoring ran;
    # the rc next to it tells you whether the sweep that fed it completed.
    echo "----- $(date -u +%FT%TZ) score start (sweep rc=$rc) -----" >> "$LOG"
    "$PY" -u -m scraper.score_jobs >> "$LOG" 2>&1
    src=$?
    echo "----- $(date -u +%FT%TZ) score end rc=$src -----" >> "$LOG"

    # ---- SECOND PASS: ANALYSE WHATEVER THE FIRST ONE BANKED -------------------------------
    #
    # A SEPARATE PROCESS, and that is the entire point -- not a tidiness preference.
    #
    # score_jobs runs FETCH first and ANALYSE second, in one process, and only the second phase
    # writes jd_terms, which is what the feed reads to decide whether a card says "JD pending".
    # On this box the first phase is the one that gets SIGKILLed at the ~1.2 GB LVE cap, so the
    # phase that clears the badge sits behind the phase that kills the run.
    #
    # MEASURED 2026-09-02, by hand, on the live box:
    #
    #   pass with SCORE_MAX_FETCH=1500 / SCORE_BUDGET_MIN=25
    #       -> RSS 1.06 GB at 5 min, 1.12 GB at 7 min, killed during "Detail-fetching 926
    #          remaining JD(s)". No traceback, no summary line. It DID bank 391 descriptions
    #          (rows with no jd: 860 -> 469) because _persist_jds flushes every 150 -- and it
    #          analysed nothing at all: jd_terms and match_score were bit-for-bit unchanged.
    #   pass with SCORE_MAX_FETCH=1 / SCORE_BUDGET_MIN=0.5, run straight afterwards
    #       -> completed, exit 0, scored 1,626 rows. Rows the feed called "JD pending"
    #          went 2,851 -> 1,322 in one pass.
    #
    # So the fetch is SAFE TO LOSE and the analysis is not: a killed fetch leaves its
    # descriptions in the column, and the next run's analysis pass picks them up through
    # urls_missing_jd_terms(). Giving the analysis its own process means a fresh heap and the
    # 1.2 GB budget starts over, which is the only lever available from a shell script.
    #
    # Deliberately NOT done by lowering SCORE_BUDGET_MIN to some value that "should" survive:
    # nobody has measured where the ceiling actually is, and a guessed number that is slightly
    # too high fails exactly the same way while looking like it was reasoned about. This
    # arrangement does not need to know.
    #
    # Cost is one extra corpus + IDF load, ~1-2 min. SCORE_MAX_FETCH=1 rather than 0, because
    # score_jobs reads 0 as "no cap at all" and would refetch the whole backlog here.
    export SCORE_MAX_FETCH=1
    export SCORE_BUDGET_MIN=0.5
    export SCORE_ANALYZE_BUDGET_MIN=12
    echo "----- $(date -u +%FT%TZ) analyse start (fetch pass rc=$src) -----" >> "$LOG"
    "$PY" -u -m scraper.score_jobs >> "$LOG" 2>&1
    echo "----- $(date -u +%FT%TZ) analyse end rc=$? -----" >> "$LOG"
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
# Also no longer gated on $rc, for the reason above. The note about "republishing yesterday's
# map under today's date" assumed a failed sweep left the corpus untouched; with per-slice
# writes it does not. Clustering reads url/title/company/location/first_seen off the CORPUS, so
# on a sweep that banked nothing this is idempotent — it rewrites the same map — and on one that
# banked eleven slices it is the only thing that will cluster them before tomorrow.
echo "----- $(date -u +%FT%TZ) reposts start (sweep rc=$rc) -----" >> "$LOG"
"$PY" -u -m scraper.reposts --write --top 0 >> "$LOG" 2>&1
echo "----- $(date -u +%FT%TZ) reposts end rc=$? -----" >> "$LOG"

exit 0
