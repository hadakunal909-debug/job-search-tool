#!/bin/bash
# Scheduled scrape, run by cPanel cron on the same box as the app and the database.
#
# THE SCHEDULE. Two full slots and one analyse-only slot, Mon-Fri. `crontab -l` over SSH reads
# the LIVE one -- cPanel's web form is a view onto it, not the only copy, which an earlier
# version of this comment claimed:
#
#   0 17,20 * * 1-5   /home/astrocha/stemjobs/bin/cron_scrape.sh
#   30 * * * 1-5      /home/astrocha/stemjobs/bin/cron_scrape.sh --analyze-only
#
# THE HOURLY ONE EXISTS BECAUSE jd_terms IS WHAT THE FEED READS, NOT jd. The sweep writes the
# description; only the analyse pass writes the packed analysis, and a card with no analysis
# shows "Not scored" however good the description behind it is. At two slots a day a job found
# at 20:20 waited until 17:00 the next day for a number the job PAGE could already compute on
# demand -- measured 2026-09-04: 461 active rows unanalysed, 278 of them holding a readable
# description. The hourly pass costs one corpus + IDF load against a 4-minute analysis budget
# and takes the same lock as the full run, so it skips rather than stacks.
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

# --analyze-only: the hourly slot. Runs the ANALYSE pass and nothing else -- no sweep, no JD
# fetch, no repost clustering. Everything above it in this file (the log ceiling, the cd, the
# lock) is shared on purpose: a second script would have to duplicate all of it, and a
# duplicated schedule is how the documented slot and the live crontab drifted three hours apart.
ANALYZE_ONLY=0
if [ "${1:-}" = "--analyze-only" ]; then
    ANALYZE_ONLY=1
elif [ -n "${1:-}" ]; then
    echo "usage: $0 [--analyze-only]" >&2
    exit 2
fi

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
# Bounds the SPECULATIVE half of the description lookup -- the fetches that exist only to
# second-guess a title the filter rejected. SCRAPE_BUDGET_MIN above stops the board sweep; this
# stops that. Same shape as the SCORE_BUDGET_MIN / SCORE_ANALYZE_BUDGET_MIN pair below. 3 rather
# than the 2 CI uses: there is no step timeout out here, only the gap to the next cron slot.
export JD_LOOKUP_BUDGET_MIN=3
# AND THE OTHER HALF IS DELIBERATELY NOT CAPPED. JD_KEEP_BUDGET (count) is left at its default
# of 0 = unlimited: a posting that has passed the title filter is going into the feed, and it
# must not go in without a description because a counter ran out. Without one the card reads
# "JD pending" with no match number, and on a fast-turnover board the description is gone before
# any later pass can fetch it -- measured on Actalent, only 31 of 755 such rows were still on
# their board when we went back for them.
#
# The clock below is a runaway guard, not a budget. Measured 2026-09-02: a complete 21-slice
# sweep of 2,032 boards took 67 min and kept 917 rows, about 700 of which needed a fetch, at
# ~34 fetches/min on 6 workers -- so roughly 20 minutes of work against a 3-hour gap to the next
# slot. 45 leaves generous room and still refuses to run forever.
export JD_KEEP_BUDGET_MIN=45
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

if [ "$ANALYZE_ONLY" = 1 ]; then
    # rc is read by the log lines below and by nothing else in this mode. 0, not "skipped":
    # every later message spells the mode out, so a bare rc does not have to carry it.
    rc=0
else
    echo "===== $(date -u +%FT%TZ) scrape start =====" >> "$LOG"
    "$PY" -u -m scraper >> "$LOG" 2>&1
    rc=$?
    echo "===== $(date -u +%FT%TZ) scrape end rc=$rc =====" >> "$LOG"
fi

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
    if [ "$ANALYZE_ONLY" = 1 ]; then
        # The fetch phase is the expensive half and the one that gets SIGKILLed; the hourly
        # slot skips it entirely and analyses what the two full runs already banked.
        src=0
    else
        echo "----- $(date -u +%FT%TZ) score start (sweep rc=$rc) -----" >> "$LOG"
        "$PY" -u -m scraper.score_jobs >> "$LOG" 2>&1
        src=$?
        echo "----- $(date -u +%FT%TZ) score end rc=$src -----" >> "$LOG"
    fi

    # ---- SECOND PASS: ANALYSE WHATEVER THE FIRST ONE BANKED -------------------------------
    #
    # 2026-09-04: THE FIRST PASS NO LONGER DIES, SO rc=137 HERE IS A BUG REPORT AGAIN.
    #
    # Everything below this amendment is the history and it is worth keeping, but read it as
    # history. It says the fetch pass is SIGKILLed on every run and that this second process
    # is the compensation. That was true from 2026-09-02 and is not true now.
    #
    # The cause was one statement in score_jobs' analysis setup: it called _load_jd_cache(),
    # which materialises EVERY stored description into one dict, to supply text for the few
    # hundred rows the run still needed -- and db.load_jobs_by_urls, which asks for exactly
    # those urls, was already sitting underneath it as the fallback. Measured on this box:
    # 8 MB -> 492 MB peak RSS, 29,991 entries. The merge-and-save above it also held that
    # dict and never released it, so the old code rebound it while the first copy was still
    # live. Asking the database first, and a `del` at both retained sites, was the whole fix.
    #
    # Re-run here afterwards with this block's exact env -- SCORE_NEW_ONLY=yes,
    # SCORE_MAX_FETCH=1500, SCORE_BUDGET_MIN=25, SCORE_ANALYZE_BUDGET_MIN=12 -- sampling
    # /proc/$PID/status rather than ps -C or pgrep -f, both of which match the watching shell
    # here and lie:
    #
    #   rc=0, peak RSS 737 MB, and it did the analysis itself: 461 jobs scored, 447 rows of
    #   JD fields written. Against a ~1.2 GB account-wide LVE cap, with room to spare.
    #
    # SO: IF YOU SEE `score end rc=137` AGAIN, SOMETHING HAS REGRESSED. Do not read it as
    # normal because the text below calls it expected -- that is exactly the trap the stale
    # Actions-secret note in this file was, and it cost a later reader a hunt for a fault
    # that had been fixed weeks earlier.
    #
    # This pass STAYS, and not out of caution about the above. A fresh heap is still the only
    # lever a shell script has, the run costs ~48 s, and the first pass is still the one
    # carrying every board map and fetched description when it reaches the analysis. It is
    # cheap insurance that has already been proven to work; what changed is that it should
    # now be finding nothing left to do.
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
    # 500 -> 100 rows per banked upsert. The analysis loop persists every SCORE_ANALYZE_CHUNK
    # rows, and on this box the process is routinely SIGKILLed mid-loop by the ACCOUNT-wide LVE
    # budget -- the website's Passenger workers plus two sibling apps were measured at 1,355 MB
    # of it. At 500 a pass killed at row 400 banks nothing; measured 2026-09-02, exactly that
    # happened while 614 freshly-fetched descriptions sat waiting to be analysed. 100 costs four
    # extra upserts per 500 rows and turns "all or nothing" back into "forward progress".
    export SCORE_ANALYZE_CHUNK=100
    if [ "$ANALYZE_ONLY" = 1 ]; then
        # 12 -> 4. This slot runs twelve times a weekday against a backlog the two full runs
        # have already mostly cleared, so it wants to be small and frequent rather than long
        # and rare -- at ~206 ms/row, 4 minutes still covers ~1,150 rows, which is more than a
        # single slot has ever left behind. It also has to share the box with the website at
        # times of day the two full slots deliberately avoid.
        export SCORE_ANALYZE_BUDGET_MIN=4
        mode="analyse-only"
    else
        mode="fetch pass rc=$src"
    fi
    echo "----- $(date -u +%FT%TZ) analyse start ($mode) -----" >> "$LOG"
    "$PY" -u -m scraper.score_jobs >> "$LOG" 2>&1
    echo "----- $(date -u +%FT%TZ) analyse end rc=$? -----" >> "$LOG"

    # ---- THE PER-USER SCORES ---------------------------------------------------------------
    #
    # AFTER the analysis, always, and that ordering is the whole point: this reads jd_terms, so
    # running it first would score the previous run's corpus and then be immediately invalidated
    # by the pass above -- which DELETES the stored score of every job whose analysis it rewrote.
    #
    # It writes one row per (user, job) into user_scores, so the feed and the job page serve a
    # stored number instead of each deriving its own. Only users with a resume are scored; a user
    # without one gets no number anywhere in the product, and a stored 0 would read as "0% match"
    # rather than "we cannot answer this".
    #
    # There is no --new-only. The script asks the table which rows this user has no CURRENT score
    # for, which covers new jobs, re-analysed jobs and an edited resume with one query -- see its
    # docstring. Measured at 0.074 ms/row, so the compute is never the cost here; the reads and
    # writes are, and in the steady state both are a few hundred rows per user.
    #
    # 6 minutes because it shares the slot with everything above it. A run that does not finish
    # leaves the rest missing, the reader falls back to computing them exactly as it did before
    # this table existed, and the next run picks them up.
    #
    # `-m scraper.score_users`, not scripts/: .cpanel.yml copies scraper/ but NOT scripts/, so
    # the module form is the only one that exists on this box. Same reason scraper.reposts below
    # is spelled that way, and it says so too.
    export DB_REQUIRE=pg
    echo "----- $(date -u +%FT%TZ) user scores start -----" >> "$LOG"
    "$PY" -u -m scraper.score_users --budget-min 6 >> "$LOG" 2>&1
    echo "----- $(date -u +%FT%TZ) user scores end rc=$? -----" >> "$LOG"
fi

# The repost-cluster map the feed card's "Posted Nx" badge reads. Derived from the corpus, so it
# runs after the sweep; measured at 28 s over 25,180 rows for 528 keys, which is affordable even on
# a shared box. --top 0 prints no cluster listing, only the summary, because this log is truncated
# at 5 MB and a 528-line table nine times a day is how that ceiling gets hit.
#
# `-m scraper.reposts`, not scripts/: .cpanel.yml copies scraper/ but NOT scripts/, so the module
# form is the only one that exists on this box.
#
# IT LIVES HERE AS WELL AS IN THE ACTIONS WORKFLOW ON PURPOSE, and the reason is not the one this
# comment used to give. It said the Actions DB_PROXY_SECRET had been wrong since ~2026-08-15, so
# every later step of the 09:00 ET run failed `401 bad signature` and this cron was the only path
# that worked. THAT IS NO LONGER TRUE -- verified 2026-09-04, the Actions run completed all
# thirteen steps green through the proxy, this step among them. Do not reason from the old claim:
# a stale "the secret is broken" note sends the next person hunting a fault that was fixed weeks
# ago, which is the more expensive kind of wrong comment because it reads like hard-won knowledge.
#
# It stays duplicated because the two paths cover different slots -- Actions takes 09:00 ET, this
# cron takes 13:00 and 16:00 -- and because this one reaches the database over the loopback via
# PG_DSN rather than the HTTPS proxy, so it still refreshes the map on a day that hop is having
# trouble. Both writing it is harmless: the write is idempotent and replaces the whole row.
#
# Not gated on the score step: the clustering reads url/title/company/location/first_seen and needs
# neither a description nor a match score. Gated on the SWEEP, because clustering a corpus the
# sweep failed to update would just republish yesterday's map under today's date.
# Also no longer gated on $rc, for the reason above. The note about "republishing yesterday's
# map under today's date" assumed a failed sweep left the corpus untouched; with per-slice
# writes it does not. Clustering reads url/title/company/location/first_seen off the CORPUS, so
# on a sweep that banked nothing this is idempotent — it rewrites the same map — and on one that
# banked eleven slices it is the only thing that will cluster them before tomorrow.
#
# Skipped by --analyze-only: the map is derived from url/title/company/location/first_seen, none
# of which that mode touches, so running it twelve more times a day would rewrite the identical
# rows at 28 s a go.
if [ "$ANALYZE_ONLY" = 0 ]; then
    echo "----- $(date -u +%FT%TZ) reposts start (sweep rc=$rc) -----" >> "$LOG"
    "$PY" -u -m scraper.reposts --write --top 0 >> "$LOG" 2>&1
    echo "----- $(date -u +%FT%TZ) reposts end rc=$? -----" >> "$LOG"
fi

exit 0
