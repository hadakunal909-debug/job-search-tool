#!/usr/bin/env python3
"""
build_docs.py: generate docs/INDEX.md and docs/MAP.md from the code itself.

WHY THIS EXISTS. Hand-written line numbers rot in days. db.py's own docstring pointed at
scraper/__init__.py:4715-4730 for a function that lives at 6287, and docs/ARCHITECTURE.md
described a Streamlit app reading jobs.csv for three months after that stopped being true.
Nothing was wrong with the prose; nothing checked it.

So the split, and it is the whole design:

    GENERATE what changes on the scale of a commit  -- line numbers, symbol lists,
                                                       section boundaries, route tables.
    HAND-WRITE what changes on the scale of a quarter -- why a thing exists, what breaks
                                                       when you touch it, which test guards it.

Then make CI refuse a mismatch. The hand-written half lives in INDEX below, keyed by
`module::symbol` and NEVER by a line number, and this script resolves each key to its
current line via ast. That is what makes it un-rottable and what gives --check teeth: a
renamed function fails the build naming the row that mentions it, instead of quietly
becoming a lie.

    python scripts/build_docs.py              write docs/INDEX.md + docs/MAP.md
    python scripts/build_docs.py --check      fail if they are stale or name something gone
    python scripts/build_docs.py --list-refs  every {mod::sym} the prose uses, resolved

DELIBERATELY DOES NOT IMPORT THE MODULES IT MAPS. Importing web builds the Flask app, reads
.env, and can write analytics rows. Text-scanned via ast instead, the same reasoning
scripts/build_deploy_zip.py gives for its own import check.
"""
import argparse
import ast
import collections
import difflib
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import docdiagrams as dg                                    # noqa: E402
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_OUT = os.path.join("docs", "INDEX.md")
MAP_OUT = os.path.join("docs", "MAP.md")

# Rooting every walk at APP is what excludes ../.claude/worktrees -- three complete stale copies
# of this tree, which make any repo-wide glob return 4x hits -- and the sibling Resume Tailoring
# project, which has its own web.py. Structural, so it cannot be forgotten.
SKIP_DIRS = {".claude", "node_modules", "__pycache__", "dist", ".vite", ".tectonic-cache",
             ".streamlit", "egress_probe", "fixtures", "vendor", ".git", "docs"}

# ---------------------------------------------------------------------------------------------
# WHAT TO MAP, in the order a newcomer should meet it.
# ---------------------------------------------------------------------------------------------
MODULES = (
    ("web.py",                  "The Flask app: every route, every request hook, the feed."),
    ("core.py",                 "The shared domain library. Imported by the app, the scraper, "
                                "the scorer and the digest, so nothing presentational lives here."),
    ("db.py",                   "Storage. One PostgREST-shaped interface over four backends."),
    ("scraper/__init__.py",     "The sweep and the intake filter, plus every ATS adapter."),
    ("scraper/score_jobs.py",   "Fetches descriptions and scores them against the resume."),
    ("resume_score.py",         "The offline resume rubric -- no network, no model."),
    ("resume_keywords.py",      "Which curated skills a track is expected to show."),
    ("resume_bullets.py",       "Per-bullet review: verb, scope, result."),
    ("jdrender.py",             "Job description -> HTML. Kept out of web.py and core.py."),
    ("norms.py",                "What the corpus knows about a ROLE and an EMPLOYER. Kept out "
                                "of core.py for the reason jdrender is: the scraper and the "
                                "digest import core and need neither."),
    ("pgrest.py",               "Transport 1: PostgREST verbs reimplemented over psycopg."),
    ("dbproxy.py",              "Transport 2: HMAC-signed HTTPS, how off-host code reaches the DB."),
    ("analytics.py",            "Event capture. Reads EV_OFF once, at import."),
    ("auth.py",                 "Password hashing and the login rule."),
    ("cpanelapi.py",            "cPanel UAPI calls for the admin disk/usage panel."),
    ("static/app.js",           "The client feed. Twin of web.py's filter and card builder."),
)

# Everything under these gets mapped after MODULES, alphabetically.
PACKAGES = ("scraper", "resume_brain")

# ---------------------------------------------------------------------------------------------
# THE HAND-WRITTEN HALF.
#
# {module::symbol} anywhere in the prose is resolved to `file.py:LINE` at render time. Never
# write a line number here -- that is the entire point. --check fails if a key stops resolving.
#
# sev  R = changing this alone breaks production SILENTLY
#      Y = lives in two or more places that must agree
#      B = one place, safe to change, but non-obvious to find
# ---------------------------------------------------------------------------------------------
Row = collections.namedtuple("Row", "sev symptom target coupled guard")

INDEX = (
    # ---- the feed ---------------------------------------------------------------------------
    Row("R", "The feed shows a job it shouldn't, or hides one it should",
        "{web.py::_filter_rows} -- the server-side filter",
        "{static/app.js::matches} (client) and {core.py::prefs_match} (email digest). All three "
        "must agree. The switch is {web.py::_FEED_INLINE_MAX} = 4000: below it the browser "
        "filters the whole corpus, above it the server does.",
        "python scripts/feed_parity.py"),
    Row("Y", "An employer tile shows a monogram where it should show a logo",
        "{web.py::logo_url} -- resolves one same-origin path from static/logos/index.json",
        "the asset is harvested by scripts/build_logos.py and judged on its PIXELS, not on its status code. Nothing is fetched from a third party at request time, and the CSP's img-src enforces that. A missing tile means the manifest and the directory disagree.",
        "python scripts/build_logos.py --check"),
    Row("Y", "A field is missing from a feed card, or I added one",
        "{web.py::_build_row} -- one dict per card, and every key is consumed by the client",
        "{static/app.js::cardHTML}. ~16 function-for-function twins exist between these two "
        "files; this pair and the filter above are the ones that bite.",
        "python scripts/test_card_meta.py"),
    Row("R", "A job's keywords name the wrong things, or the match % looks wrong",
        "{core.py::analyze_jd} -- the résumé-independent half: which terms a posting names, "
        "and what each is worth",
        "ATS keywords are matched by {core.py::_term_in}, NOT by `kw in jd_low`. That substring "
        "test invented a hard skill in 89% of stored postings -- `visio` from \"division\" "
        "(59.5% of the corpus), `excel` from \"excellence\" (37.1%), `sla` from \"translate\", "
        "`git` from \"digital\", `safe` from \"safety\", `lean` from \"cleaning\" -- and each "
        "took x2.5 for being a hard skill and x1.6 again for the requirements section, so the "
        "phantoms outweighed the real terms INSIDE core_terms and moved the percentage itself. "
        "Do not \"simplify\" it to a word boundary on both sides: that also throws away "
        "`stakeholders` (7,165 postings), `budgets`/`budgeting`, `kpis` and `roadmaps`, which "
        "the substring rule was legitimately earning. phrase_exact=True is the other half -- on "
        "the JD side a phrase must be NAMED, because the scattered-stem rule that is right for "
        "a résumé fired `business requirements` on 63.8% of postings. An unseen term takes "
        "{core.py::_UNSEEN_W}, never max(idf): 54.4% of idf.json's entries sit at the maximum, "
        "so the median of its value list IS the maximum and the correction the old comment "
        "described never took effect. Measure with scripts/measure_jd_reading.py, which keeps "
        "its own copy of the old rule so the number stays meaningful.",
        "python test_scoring.py"),
    Row("R", "A description reads as one run-on wall, or carries page furniture",
        "{core.py::_soup_text} -- the one place HTML becomes a stored description",
        "block tags become NEWLINES and <li> becomes a bullet, because {jdrender.py::jd_nodes} "
        "is a newline-driven parser and was only guessing since this function had deleted the "
        "signal it needs. A newline in the jd column needs no migration -- 9.4% of stored rows "
        "already carry one (the jobspy path stores markdown). Source-formatting whitespace is "
        "collapsed FIRST, so only real block boundaries survive. {core.py::fetch_jd} is the "
        "only step in score_jobs.detail_jd's chain that can SUCCEED AT READING THE WRONG THING, "
        "so it prefers a content region via {core.py::_main_region} before falling back to the "
        "whole document. html.unescape stays BEFORE the parse: Greenhouse's content field is "
        "HTML-escaped HTML.",
        "python scripts/test_html_to_text.py"),
    Row("Y", "A keyword panel offers something absurd as a skill to add",
        "{core.py::display_terms} -- one filter, three surfaces",
        "it lives in core because web imports resume_brain and not the reverse, which is why "
        "/job used to filter and /tailor and /brain/tailor did not -- and the unfiltered list "
        "was what brain_tailor.html posted back into apply_feedback, so noise became permanent "
        "trigger keys in users.brain_kb. {core.py::ELIGIBILITY_TERMS} is separate from "
        "PERK_TERMS on purpose: a clearance is a condition you meet or do not, not a skill you "
        "can choose to add. Short names that ARE real skills (c#, go, bi, qa, ux) survive the "
        "three-character floor because ATS_KEYWORDS exempts them.",
        "python test_scoring.py"),
    Row("Y", "A role norm or an employer's tool list looks wrong, stale or empty",
        "{norms.py::load_norms} -- reads norms.json, built by scripts/build_norms.py",
        "the statistic is a PREVALENCE DIFFERENCE, share_in_family minus share_in_corpus, and "
        "not a lift ratio: lift saturates, so its top terms for `ops` were \"salaried\", "
        "\"stairs\", \"mile\" and \"shifts\". Three corrections the measurement forced and "
        "none of which is optional -- an EMPLOYER CAP, because one employer held 328 of the "
        "1,822 `systems` postings and without it their template became \"what the role asks "
        "for\"; the company layer restricted to {core.py::ATS_TOOLS}, because over every term "
        "the honest answer is a boilerplate paragraph (Northrop \"employees 94%\", Amazon "
        "\"onboarding 97%\"); and the company baseline being that employer's OWN role mix, "
        "not the corpus, or it just re-describes who they hire. Three of twenty-four families "
        "are under {norms.py::MIN_FAMILY} and correctly get no norm at all. _meta.role_keys is "
        "asserted equal to core.ROLE_KEYS, so editing ROLE_FAMILIES FAILS THE TEST rather than "
        "silently re-weighting every share a reader is shown.",
        "python scripts/test_norms.py"),
    Row("B", "\"What are my chances\" -- why the app refuses to give a probability",
        "{norms.py::coverage} -- a count of the role's usual ask that the résumé holds",
        "there is nothing to calibrate a probability against: the applications table holds 233 "
        "rows, every one still `applied`, with no interview, offer or rejection recorded and "
        "match_score NULL on all of them, and ev_usage's apply-rate-by-score curve runs "
        "41.8% -> 30.2% -> 27.5% -> 6.9% -> 0%, i.e. INVERTED. Until that curve rights itself "
        "and outcomes are actually recorded, \"you hold 7 of the 12 things this role usually "
        "asks for, and here are the five you do not\" is the strongest claim the data supports "
        "-- and it is more actionable than a number.",
        "python scripts/test_norms.py"),
    Row("B", "Search misses an obvious hit, or ranks badly",
        "{web.py::searchHit} and {web.py::searchRank} -- typo-tolerant matching",
        "mirrored in app.js function-for-function. {web.py::_search_tol} sets how much typo "
        "slack a term gets by length.",
        "python scripts/test_search_and_similar.py"),
    Row("B", "The feed is slow, or shows stale jobs after a scrape",
        "{web.py::get_jobs} -- TTL cache over the corpus, and {web.py::ranked_rows}",
        "{web.py::_invalidate_jobs} is what a write must call. The on-disk gzip snapshot is "
        "{web.py::_snapshot_read} / {web.py::_snapshot_write}, shared across Passenger workers.",
        "python scripts/test_jobs_cache.py"),
    Row("Y", "The FIRST page load is slow and every one after it is fast",
        "{web.py::_base_rows} -- every card field except the score, built once per CORPUS",
        "of the 41 keys {web.py::_build_row} emits, exactly one -- score -- depends on who is "
        "asking, so {web.py::ranked_rows} overlays that onto shallow copies of a shared list. "
        "Two rules hold it together. {web.py::_dedupe_rows} must run AFTER the overlay, because "
        "{web.py::_dupe_rank} tie-breaks on the score and folding duplicates while every base "
        "score is still 0 keeps a different copy. And the cache refuses to key on "
        "jobs_fingerprint()'s \"don't know\" answer, (None, \"\"), which is a NON-EMPTY and "
        "therefore truthy tuple -- {web.py::_invalidate_jobs} clears it outright because the "
        "extension's JD patch moves neither half of that fingerprint. {web.py::warm} builds the "
        "shared half off the user's path; {web.py::_cache_max} charges it one entry.",
        "python scripts/test_speed_caches.py"),
    Row("Y", "A COLD worker is slow -- so the site is slow right after a restart or a deploy",
        "{web.py::_corpus_fp} -- the corpus KEY without the corpus, which is what the whole "
        "cold path now turns on",
        "everything a cold render needs is already on disk in row_cache/ and score_cache/, and "
        "both are keyed on the fingerprint -- so the worker needed the KEY, not the 46 MB the "
        "key is stored inside. {web.py::_snapshot_fp} reads it from a 120-byte sidecar stamped "
        "with the snapshot's own (mtime_ns, size); if that cannot be trusted, db's fingerprint "
        "probe answers; only then does {web.py::get_jobs} run. Three things must hold. "
        "{web.py::_snapshot_touch} and {web.py::_snapshot_write} must RESTAMP the sidecar, or "
        "os.utime silently kills the fast path it feeds. {web.py::_derived_signature} must hash "
        "file CONTENT, not mtime, or a deploy -- which rewrites every file and changes no byte "
        "-- invalidates all 40k rows and charges the first visitor ~7 s. And "
        "{web.py::_apply_dedupe_plan} must stay equal to {web.py::_dedupe_rows} over the whole "
        "corpus INCLUDING order, because ranked_rows sorts on score right after and that sort "
        "is stable. Measured 2026-09-01 at 40,294 rows: 2,143 ms -> ~500 ms.",
        "python scripts/test_speed_caches.py"),
    Row("R", "The first feed load after a scrape takes many seconds",
        "{web.py::_warm_user_scores} -- every account's score file, written off the request path",
        "the shared half was never the dominant term here. Score files are keyed on (user, "
        "résumé) with the corpus fingerprint inside, so a scrape invalidates all of them and the "
        "next visitor paid a full pass over every row: 46% of live renders were over 2 s, median "
        "5.0 s, one at 15.7 s. {web.py::warm} writes them now, and it is safe on every keep-warm "
        "tick because a still-valid file short-circuits. The pass itself is ~4x cheaper too: "
        "{core.py::_term_present} is memoised (76% of the pass, 96.8% hit rate) and "
        "{core.py::score_pct} skips the have/missing sorts {web.py::user_scores} throws away. "
        "score_pct MUST stay equal to {core.py::score_against}[0] -- that is every match "
        "percentage in the product.",
        "python scripts/test_speed_caches.py"),
    Row("B", '"Similar roles" on a job page looks unrelated',
        "{web.py::_title_index} and {web.py::_similar_roles}",
        "{web.py::_TITLE_STOP} is the rail's list and is deliberately SHORT -- seniority and "
        "level words are real signal and IDF already discounts them for being common. Do not "
        "confuse it with {web.py::_USAGE_STOP}, which is /admin/usage's token-lift list and "
        "stops the opposite things on purpose. They shared a name until 2026-08-20; see B1.",
        "python scripts/test_search_and_similar.py"),

    # ---- intake -----------------------------------------------------------------------------
    Row("R", "A job exists on the employer's board but never reaches my feed",
        "the keep loop in {scraper/__init__.py::main} -- nine gates in order, each one tallied",
        "the drop reason is printed at the end of a run. Read the count, then find that exact "
        "string in the funnel diagram in docs/ARCHITECTURE.md, which is generated from the same "
        "dict. {core.py::admits_on_description} is the rescue path for a job whose TITLE matched "
        "nothing.",
        "python scripts/dump_titles.py"),
    Row("R", "A US job never reaches the feed although its board is scraped fine",
        "{scraper/__init__.py::is_us_location} -- the US-only gate, applied at INGEST",
        "so the CORPUS is the wrong place to measure it: it only holds rows that already "
        "passed (6 rejects in 42,180). Measure on RAW board output. Unknown or blank is KEPT; "
        "the function only drops what it can place abroad. Two twins must move with it -- "
        "{scraper/__init__.py::title_says_non_us} and adopt_everify_boards._names_non_us, both "
        "of which read {scraper/__init__.py::_NON_US_RE} directly, and the second is a VETO "
        "that must answer False for anything it cannot place rather than True. The veto runs "
        "BEFORE the state check, so a foreign city name beats a US state code unless the name "
        "is in {scraper/__init__.py::_US_NAMESAKE_CITIES}: no positional rule can separate "
        "\"Lima, OH\" from \"Indore, IN\", because in all ten real collisions the two-letter "
        "code FOLLOWS the name and IN, OR and DE are India, Odisha and Germany colliding with "
        "Indiana, Oregon and Delaware. STILL UNFIXED and the biggest known loss: a bare city "
        "(\"San Francisco\", \"Austin\", \"Bay Area\") is not placed at all, which cost one "
        "75-board batch ~70 on-target US roles across 10 real US employers. That needs a "
        "gazetteer; do not guess it.",
        "python test_us_location.py"),
    Row("Y", "A whole board suddenly returns nothing",
        "the adapter for its ATS in {scraper/__init__.py::SCRAPERS} -- 29 entries, ats_type -> "
        "function",
        "{scraper/__init__.py::SOURCES} holds the built-in boards; app-added ones come from the "
        "boards table and are merged on top. {scraper/__init__.py::save_board_health} keeps an "
        "8-run rolling window per board, visible in /admin/data.",
        "python scripts/verify_parsers.py"),
    Row("R", "Jobs are disappearing that should still be live",
        "{scraper/__init__.py::reconcile_closed} -- the 3-consecutive-miss rule",
        "every guard around it moves together: CLOSED_AFTER_MISSES, RECONCILE_MIN_ROWS, "
        "RECONCILE_MIN_RATIO. Loosen one and a bot-walled board retires its whole inventory in "
        "a single pass. It sets is_active=false and never deletes.",
        "python test_liveness.py"),
    Row("Y", "The corpus is growing or shrinking unexpectedly",
        "{scraper/__init__.py::MAX_AGE_DAYS} on the way in, {db.py::prune_old_jobs} on the way out",
        "MAX_AGE_DAYS, PRUNE_DAYS and {db.py::AGE_LONG_DAYS} must move together. If the intake "
        "gate and the prune disagree, the corpus drifts to whichever is looser.",
        "python scripts/prune_stale.py --dry-run"),
    Row("B", "The same job appears twice",
        "{web.py::_dedupe_rows} for display, {core.py::posting_key} for identity",
        "{scraper/__init__.py::canonical_url} runs before insert, so the writer and the table "
        "cannot disagree about the key. Employer floods are GROUPED, never deduped -- that is "
        "deliberate.",
        "python test_canonical_url.py"),
    Row("B", "Adding a board from the app says it can't detect the ATS",
        "{scraper/__init__.py::detect_board} -- the detect chain, then "
        "{scraper/__init__.py::probe_board} which validates before any insert",
        "digitas is deliberately NOT emitted by detect_board: it reads a 14 MB sitemap and must "
        "not be pointable at an arbitrary host.",
        "python scripts/verify_parsers.py"),
    Row("Y", 'Adding a board from the EXTENSION says "Couldn\'t add" and no reason',
        "{web.py::ext_detect_board} -- the extension's half of /add-board",
        "the two must agree, and for months they did not: this route refused on a falsy "
        "probe_board count and returned no `error` at all, so popup.js printed the same generic "
        "hint for every failure it has. It also re-ran the whole detect chain on the ADD click, "
        "half of which is a live fetch, and it stored {scraper/__init__.py::detect_board}'s "
        "name suggestion verbatim -- see {scraper/__init__.py::name_is_sluglike} and "
        "{scraper/__init__.py::board_display_name}, the guards /add-board already used.",
        "python scripts/test_add_board_api.py"),

    # ---- scoring ----------------------------------------------------------------------------
    Row("Y", "Match percentages look wrong across the board",
        "{core.py::score_against} and {core.py::core_terms}",
        "{core.py::CORE_WEIGHT_FRACTION} weights the terms that matter. If you change what the "
        "score MEANS, bump {core.py::MIN_SCALE} or every saved search keeps filtering on the old "
        "scale. Do not make the number relative -- see the note in CLAUDE.md.",
        "python test_scoring.py"),
    Row("R", "A new scoring field isn't persisting",
        "{scraper/score_jobs.py::_persist_derived}",
        "{db.py::COLS_SCORE} must contain every column the diff compares, or have.get(k) is None "
        "forever and every row re-upserts on all three runs a day.",
        "python test_scoring.py"),
    Row("R", "Scores changed and nobody touched the scorer",
        "idf.json, written by {core.py::save_idf} on the FULL pass only",
        "a partial rebuild silently re-weights every score in the corpus. It is committed source-"
        "of-truth, not a cache. {core.py::load_idf} is every reader.",
        "python test_scoring.py"),
    Row("Y", "A job page shows no description although we fetched one",
        "{scraper/score_jobs.py::_jd_corpus} -- the two-store reconciliation",
        "jd_cache.json.gz and the jobs.jd column are written at different moments and HAVE "
        "diverged, to 48% of pending rows. Any new writer of a description must touch both.",
        "python test_jd_persist.py"),
    Row("B", "A description is present but useless -- a cookie banner, a nav bar",
        "{scraper/score_jobs.py::_is_thin_jd} and the per-host retry ledger around it",
        "three different thresholds exist: {core.py::_MIN_JD_CHARS}, score_jobs' own page "
        "minimum, and the thin test. Editing score_jobs.py at all reopens every backed-off host "
        "for one round, because the ledger key hashes the file.",
        "python scripts/refetch_thin_jds.py --dry-run"),
    Row("R", "jd_terms round-trips wrong, or every row re-upserts every run",
        "{core.py::pack_analyzed} / {core.py::unpack_analyzed}",
        "jobs.jd_terms is TEXT, not jsonb, on purpose, and the KEY ORDER is semantic: it is "
        "analyze_jd's frozen term order, it breaks ties in the skill panel, and score_jobs diffs "
        "the stored string to decide whether to write at all.",
        "python test_jd_persist.py"),

    # ---- the database -----------------------------------------------------------------------
    Row("R", 'A scrape reported success but the database did not change',
        "{db.py::_LazyHTTP} -- which of the four backends you actually got",
        ".env is read relative to the CURRENT WORKING DIRECTORY, so a script that does not cd "
        "here first silently writes somewhere else and exits 0. {db.py::_check_backend_intent} "
        "plus DB_REQUIRE is how you make that fail loudly. {db.py::backend_name} tells you which "
        "backend you have; has_remote_db() answers True for three of them and will not.",
        "python scripts/probe_db_proxy.py"),
    Row("Y", "A query works locally but 403s from GitHub Actions",
        "{dbproxy.py::ALLOWED_TABLES} -- the proxy's allowlist",
        "it must track db.py's table constants. A new table works on PG_DSN and is refused "
        "through the proxy, so the failure only ever appears in CI.",
        "python scripts/test_dbproxy.py"),
    Row("R", "A script hangs, or the egress bill jumps",
        "{db.py::load_jobs} called with no cols -- that is ~168 MB of descriptions at 26k rows",
        "{db.py::_warn_full_jd_read} prints the caller; it exists because four scripts did it. "
        "Also {db.py::_fetch_all} OVERWRITES limit with its 1000-row page size, so asking it for "
        "one row walks the whole table -- copy {db.py::newest_event_ts} for a one-row select.",
        "python scripts/probe_db_proxy.py"),
    Row("R", "I DID pass cols= and it still read the whole corpus",
        "{db.py::load_jobs} again -- the narrow select is best-effort and falls back SILENTLY",
        "ONE column name that does not exist on the table fails the entire select, and the "
        "fallback reads every description. The only signal is {db.py::_warn_full_jd_read} on "
        "stderr, which is easy to scroll past. DERIVED FIELDS ARE THE TRAP: role_track looks "
        "like a column and is computed by {core.py::role_track} per request, so asking for it "
        "costs a full-corpus read -- that is how this row got written. Prefer a "
        "{db.py::COLS_SCORE}-style constant, or check one name first with "
        "db._fetch_all(db.TABLE, {'select': name, 'limit': 1}).",
        "python scripts/dump_schema.py"),
    Row("B", "A column exists in code but not in the live database",
        "schema.sql, regenerated by asking Postgres to describe itself",
        "{db.py::JOBS_DERIVED_SQL} describes the same table a second time. resume_files and "
        "brain_companies are in db.py and the proxy allowlist but absent from schema.sql -- "
        "brain_companies deliberately, see the admin health check.",
        "python scripts/dump_schema.py"),

    # ---- the app surface --------------------------------------------------------------------
    Row("B", "Text renders in a fallback face, or a font is refused in the console",
        "the @font-face block at the top of static/style.css -- six woff2 in static/fonts/",
        "the fonts are OURS, exactly like the logos: nothing is fetched from Google at request "
        "time and {web.py::_CSP_TEMPLATE} says font-src 'self' with no remote origin left in "
        "style-src either, so a re-added <link> to fonts.googleapis.com fails visibly instead of "
        "quietly putting two third-party handshakes in front of first paint. A preload href in "
        "templates/base.html must equal its @font-face src BYTE FOR BYTE, ?v= included, or the "
        "file is fetched twice; and ?v= is also what earns the immutable Cache-Control.",
        "python scripts/test_fonts.py"),
    Row("B", "Load more, a tab switch or a filter change gives no feedback",
        "{static/app.js::skeletonHTML}, {static/app.js::animateIn} and "
        "{static/app.js::moreLoading}",
        "skeletonHTML is the twin of the eight .skel tiles in templates/_feedgrid.html, which "
        "the server paints once and the first render then wipes for good. animateIn is called "
        "with the index of the first NEW card, so a Load more animates the page it appended "
        "rather than re-flashing the feed, and its delay is capped -- uncapped, card 60 waits "
        "1.1 s. moreLoading must be cleared on EVERY exit from {static/app.js::renderServer}, "
        "including the 429 and 401 branches, or a failed page leaves the button spinning. Any "
        "new animation needs its own prefers-reduced-motion rule: the global one in style.css "
        "clamps animation-duration and says nothing about animation-DELAY.",
        "python scripts/run_tests.py --only card_meta"),
    Row("Y", "A POST returns 400, or a new form silently fails",
        "{web.py::_require_csrf} -- runs before EVERY cookie-authenticated write",
        "{web.py::_CSRF_EXEMPT} has four entries and each carries its justification. New routes "
        "are protected by default; adding to that set needs the same standard of reasoning.",
        "python scripts/smoke_app.py"),
    Row("R", "Analytics numbers look inflated, or a test wrote real events",
        "{analytics.py::emit} and the auto page_view in {web.py::_ev_page_view}",
        "EV_OFF=1 must be in the environment BEFORE anything imports web -- analytics.py reads "
        "it once, at import. One unguarded parity run wrote 98.8% of all recorded feed_view "
        "events. scripts/run_tests.py sets it per subprocess so this cannot recur.",
        "python scripts/run_tests.py --only card_meta"),
    Row("Y", "The Chrome extension stopped working, or says it is out of date",
        "{web.py::EXT_MIN_VERSION} -- the server's floor",
        "extension/manifest.json's version must match, and NOTHING enforces that. Bumping the "
        "manifest alone tells every install it is current. The response carries no update_url "
        "on purpose -- the extension is side-loaded, so Chrome can never update it; the `how` "
        "string tells the user to pull and reload, and extension/popup.js is what shows it.",
        "python scripts/test_ext_contract.py"),
    Row("Y", "An aggregator relist reached the apply queue",
        "{web.py::_QUEUE_SKIP_HOSTS}",
        "a verbatim retyped copy of {core.py::AGGREGATOR_HOSTS}. Adding a sixth aggregator "
        "updates the feed's dedupe and misses the queue. No test covers this yet.",
        None),
    Row("B", "A job description renders badly -- lost headings, mangled lists",
        "{jdrender.py::render_jd} -- typed nodes, not markup, then one HTML pass",
        "byte-frozen against scripts/fixtures/jd_html_baseline.json. Regenerating that baseline "
        "needs a reviewed diff; it is a snapshot, so it will happily bless a regression.",
        "python scripts/test_jdrender.py"),
    Row("B", "The resume grader scores something oddly",
        "{resume_score.py::score_resume} -- ~25 checks behind a weight table",
        "{resume_keywords.py::evaluate} owns the Skills category and decides presence via "
        "core's term matcher, so it agrees with the feed's match %. Voice rules are shared with "
        "the AI rewriter, so the grader and the rewriter cannot disagree about a filler word.",
        "python test_resume_score.py"),
    Row("B", "Sponsorship colour or tier looks wrong on a card",
        "{core.py::visa_tags} for the posting's own claim, {core.py::sponsor_strength} for "
        "federal history",
        "absence of a record is NOT evidence of non-sponsorship, and the feed is never filtered "
        "on it. {core.py::is_cap_exempt} is the separate universities-and-hospitals path.",
        "python test_sponsor_flag.py"),
    Row("B", "A management role shows in a dev search, or vice versa",
        "{core.py::role_track} -- management wins ties, deliberately",
        "{core.py::roles_match} and ROLE_FAMILIES drive the role picker. "
        "{core.py::reads_like_pm} is the description-level rule with its own anchor/point gates.",
        "python test_pm_rule.py"),
    Row("B", "grep or ripgrep finds nothing in static/app.js",
        "a raw NUL byte at offset 46372 (line 733) makes ripgrep classify the file as binary and "
        "return no line numbers; git marks it -text for the same reason",
        "use `grep -a`, `git grep`, or the app.js section of docs/MAP.md. It is the second-most-"
        "coupled file in the repo, so this matters more than it sounds.",
        None),
)

# ---------------------------------------------------------------------------------------------
# KNOWN-WRONG. A symptom row is allowed to point at a bug instead of pretending there is a fix.
# ---------------------------------------------------------------------------------------------
KnownWrong = collections.namedtuple("KnownWrong", "tag title detail workaround")

KNOWN_WRONG = (
    KnownWrong(
        "B1", "_TITLE_STOP was defined twice and the live copy was the wrong one (FIXED 2026-08-20)",
        "{web.py::_TITLE_STOP} was bound once for the similar-roles title index and again, 2,500 "
        "lines later, with a different word list for /admin/usage. Python resolves globals at "
        "call time, so {web.py::_title_tokens} used the SECOND list -- which strips engineer, "
        "manager, analyst, developer, senior and lead, exactly the tokens that distinguish one "
        "role from another, and keeps job, role, remote, hybrid and usa, which the intended list "
        "removes. Measured effect: \"Senior Project Manager, Remote - USA\" matched on "
        "{project, remote, usa}, so it was ranked similar to any remote US job; it now matches "
        "on {manager, project, senior}. The first definition had no effect at all.",
        "Fixed by renaming the admin one to {web.py::_USAGE_STOP}. Both lists were individually "
        "correct and self-documenting -- only the shared name was wrong. build_docs.py now "
        "detects module-level shadowing on every run and tells a real shadow apart from a "
        "deliberate compile-in-place rebind, so this class cannot return silently."),
    KnownWrong(
        "B2", "/extension was advertised as the update URL and 404'd (FIXED 2026-08-20)",
        "{web.py::ext_version} returned request.host_url + \"/extension\" to every extension "
        "that checked its version, and no route serves that path. Three things were wrong at "
        "once: the URL 404'd, extension/popup.js never read the field (it shows the `how` "
        "string), and {web.py::EXT_MIN_VERSION}'s own comment 40 lines above says the extension "
        "is side-loaded unpacked with \"no update_url\" precisely because Chrome can never "
        "update it. The field contradicted the design it sat next to.",
        "Fixed by removing the field rather than adding a page nobody would visit. `how` was "
        "already the real answer and was already being displayed."),
    KnownWrong(
        "B3", "Two test suites could not fail (FIXED 2026-08-20)",
        "scripts/test_visa.py and scripts/test_notify.py printed \"N FAILED\" and then exited 0. "
        "Both run in python-tests.yml under `set -e`, so neither could ever have failed a build. "
        "Verified with a forced failure: identical stdout, exit 0 before and exit 1 after.",
        "Fixed. scripts/run_tests.py also treats a FAILED line as failure regardless of exit "
        "code, because the bug class is cheap to keep catching."),
    KnownWrong(
        "B4", "GitHub Actions cannot reach the database",
        "The Actions DB_PROXY_SECRET has not matched the server's since roughly 2026-08-15, so "
        "scheduled runs die on HTTP 401 {\"error\":\"bad signature\"}. verify_dates, the "
        "analytics rollup and the digest email have not run there since.",
        "Repost detection is duplicated into bin/cron_scrape.sh and THAT copy works -- do not "
        "\"clean up the duplicate\". Fix by resetting the repository secret to match "
        ".db_proxy_secret."),
    KnownWrong(
        "B5", "Seven employers have no board at all",
        "ASML, Deutsche Bank, LTIMindtree, Marlabs, Qualcomm, Renesas and Tradeweb were reachable "
        "only through Adzuna, which was removed on 2026-08-16. Their boards rows still say "
        "ats_type=\"adzuna\" and are inert.",
        "Each needs its real ATS tenant id; guessed Workday and Greenhouse tenants did not "
        "resolve. `python scripts/probe_adzuna_replacements.py` walks the detect chain."),
)

REF = re.compile(r"\{([^{}]+?)::([^{}]+?)\}")
BANNER = re.compile(r"^#\s*[-=]{3,}\s*(.*?)\s*[-=]*\s*$")
JS_FUNC = re.compile(r"^\s*function\s+([A-Za-z_$][\w$]*)", re.M)
JS_VAR = re.compile(r"^\s{0,4}(?:var|const|let)\s+([A-Za-z_$][\w$]*)\s*=", re.M)
# One optional leading underscore: module-private constants like _FEED_INLINE_MAX and
# _CSRF_EXEMPT are the idiom throughout web.py, and they are exactly the ones worth indexing.
UPPER = re.compile(r"^_?[A-Z][A-Z0-9_]*$")

Sym = collections.namedtuple("Sym", "name kind line end doc routes")
Section = collections.namedtuple("Section", "label start end")


def read(rel):
    """Read a source file without letting anything translate its newlines.

    errors="replace" because static/app.js contains a raw NUL; newline="" so the CRLF working
    copy is seen as it is on disk. Callers normalise before parsing.
    """
    with open(os.path.join(APP, rel), encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def symbols(rel, src):
    """Top-level defs, classes, UPPER_SNAKE constants and Flask routes.

    Routes fall straight out of the AST: @app.route("/x", methods=[...]) is a Call whose func is
    an Attribute named route, so the path, the methods and the handler name all come from one
    walk with no regex and nothing to drift.
    """
    if rel.endswith(".js"):
        out = []
        for pat, kind in ((JS_FUNC, "function"), (JS_VAR, "var")):
            for m in pat.finditer(src):
                out.append(Sym(m.group(1), kind, src[:m.start()].count("\n") + 1, 0, "", ()))
        seen, uniq = set(), []
        for s in sorted(out, key=lambda s: s.line):
            if s.name not in seen:
                seen.add(s.name)
                uniq.append(s)
        return uniq
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print("  ! %s: %s" % (rel, e))
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            routes = []
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "route" and dec.args
                        and isinstance(dec.args[0], ast.Constant)):
                    methods = ""
                    for kw in dec.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                            methods = ",".join(e.value for e in kw.value.elts
                                               if isinstance(e, ast.Constant))
                    routes.append((dec.args[0].value, methods or "GET"))
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            out.append(Sym(node.name,
                           "class" if isinstance(node, ast.ClassDef) else "def",
                           node.lineno, node.end_lineno or node.lineno,
                           " ".join(doc[0].split()) if doc else "",
                           tuple(routes)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and UPPER.match(t.id):
                    out.append(Sym(t.id, "const", node.lineno, node.lineno, "", ()))
    return out


def sections(src):
    """Turn the author's own banner comments into a navigation table.

    Two styles are in use and both matter: inline `# ---- label ----`, and a sandwich where a
    rule, a `# text` line and another rule surround the label. Lifting the existing banners
    rather than inventing a taxonomy means the map matches how the code is already organised.
    """
    lines = src.split("\n")
    found = []
    i = 0
    while i < len(lines):
        m = BANNER.match(lines[i])
        if not m:
            i += 1
            continue
        label = m.group(1).strip()
        if not label and i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt.startswith("#") and not BANNER.match(nxt):
                label = nxt.lstrip("#").strip()
                if i + 2 < len(lines) and BANNER.match(lines[i + 2]):
                    i += 2
        label = label.strip(" -=").strip()
        if label and len(label) < 130:
            found.append((i + 1, label))
        i += 1
    out = []
    for n, (line, label) in enumerate(found):
        end = found[n + 1][0] - 1 if n + 1 < len(found) else len(lines)
        out.append(Section(label, line, end))
    return out


def collect():
    """{rel: (src, [Sym], [Section])} for every mapped file, in reading order."""
    rels = [m[0] for m in MODULES]
    for pkg in PACKAGES:
        d = os.path.join(APP, pkg)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            rel = "%s/%s" % (pkg, f)
            if f.endswith(".py") and rel not in rels:
                rels.append(rel)
    data = collections.OrderedDict()
    for rel in rels:
        if not os.path.exists(os.path.join(APP, rel)):
            print("  ! mapped file missing: %s" % rel)
            continue
        src = read(rel)
        data[rel] = (src, symbols(rel, src), sections(src))
    return data


def resolve(text, data, errors, where):
    """Replace every {module::symbol} with `module:LINE` `symbol`.

    An unresolvable key is an error, not a silent passthrough -- that is the mechanism that
    keeps the hand-written prose honest.
    """
    def sub(m):
        rel, name = m.group(1), m.group(2)
        if rel not in data:
            errors.append("%s references unmapped file %s" % (where, rel))
            return "`%s::%s`" % (rel, name)
        hits = [s for s in data[rel][1] if s.name == name]
        if not hits:
            errors.append("%s names %s::%s -- not found" % (where, rel, name))
            return "`%s::%s` **(MISSING)**" % (rel, name)
        out = "[`%s:%d`](../%s#L%d) `%s`" % (rel, hits[0].line, rel, hits[0].line, name)
        if len(hits) > 1:
            # Bound more than once at module level. Python resolves globals at call time, so the
            # LAST binding is the one that runs -- flagging it inline is how B1 stops being
            # invisible.
            out += " (also at %s — **shadowed, the last one wins**)" % ", ".join(
                "[%d](../%s#L%d)" % (h.line, rel, h.line) for h in hits[1:])
        return out
    return REF.sub(sub, text)


def shadowed(data):
    """Names bound more than once at module level, last-wins.

    Not a style nit: web.py binds _TITLE_STOP twice with different word lists 2,500 lines apart,
    and the copy that runs is NOT the one sitting next to the code that uses it. That was found
    by reading; this finds it by construction.

    Two shapes have to be told apart or the table is noise. `X = tuple(f(p) for p in X)` is a
    deliberate compile-in-place -- the second binding CONSUMES the first, so nothing is lost.
    A rebind whose value never mentions the name is the dangerous kind. Returns
    (rel, name, lines, benign).
    """
    out = []
    for rel, (src, syms, _secs) in data.items():
        if rel.endswith(".js"):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        # line -> whether that assignment's value references the name being assigned
        selfref = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                nm = node.targets[0].id
                selfref[node.lineno] = any(
                    isinstance(x, ast.Name) and x.id == nm for x in ast.walk(node.value))
        seen = collections.defaultdict(list)
        for s in syms:
            seen[s.name].append(s.line)
        for name, lines in sorted(seen.items()):
            if len(lines) > 1:
                benign = all(selfref.get(n) for n in lines[1:])
                out.append((rel, name, lines, benign))
    return out


def suite_names():
    """Suite names known to scripts/run_tests.py, for validating the guard column."""
    try:
        src = read("scripts/run_tests.py")
    except OSError:
        return set()
    return set(re.findall(r'Suite\("([^"]+)"', src))


def render_index(data, errors):
    SEV = {"R": ("\U0001F534", "changing this alone breaks production **silently**"),
           "Y": ("\U0001F7E1", "lives in two or more places that must agree"),
           "B": ("\U0001F535", "one place, safe to change, but non-obvious to find")}
    suites = suite_names()
    L = ["# Index — symptom to source",
         "",
         "<!-- GENERATED by scripts/build_docs.py. Do not edit: your changes will be "
         "overwritten and CI will fail. The prose lives in the INDEX tuple in that script, "
         "keyed by symbol so the line numbers here are always current. -->",
         "",
         "Start here when something is wrong. Every line number below is resolved from the code "
         "at generation time, so it is correct by construction rather than by anyone's diligence.",
         "",
         "| | meaning |", "|---|---|"]
    for k in ("R", "Y", "B"):
        L.append("| %s | %s |" % (SEV[k][0], SEV[k][1]))
    L += ["", "| | Symptom | Go here | Must stay in step with | Guard |",
          "|---|---|---|---|---|"]
    for n, r in enumerate(INDEX):
        where = "INDEX row %d (%s)" % (n + 1, r.symptom[:44])
        guard = r.guard or "*none — write one*"
        if r.guard:
            # Every script a guard names must exist, and if it is a test suite it must be in
            # run_tests.py's SUITES -- otherwise the index promises a check that CI never runs.
            for script in re.findall(r"[\w/]+\.py", r.guard):
                if not os.path.exists(os.path.join(APP, script)):
                    errors.append("%s guard names %s, which does not exist" % (where, script))
                    continue
                stem = os.path.basename(script)[:-3]
                if stem.startswith(("test_", "feed_", "smoke_", "verify_")) \
                        and stem not in suites:
                    errors.append("%s guard names %s, which is not in run_tests.py SUITES"
                                  % (where, script))
            guard = "`%s`" % r.guard
        L.append("| %s | %s | %s | %s | %s |" % (
            SEV[r.sev][0],
            resolve(r.symptom, data, errors, where),
            resolve(r.target, data, errors, where),
            resolve(r.coupled, data, errors, where),
            guard))
    L += ["", "## Known-wrong",
          "",
          "Things that are broken or misleading right now. A symptom above may point here "
          "instead of at a fix — that is deliberate, because an index you can't trust is worse "
          "than no index.",
          ""]
    for kw in KNOWN_WRONG:
        L += ["### %s — %s" % (kw.tag, kw.title), "",
              resolve(kw.detail, data, errors, "Known-wrong %s" % kw.tag), "",
              "**Workaround:** %s" % resolve(kw.workaround, data, errors,
                                             "Known-wrong %s" % kw.tag), ""]
    dup = shadowed(data)
    if dup:
        L += ["## Shadowed definitions", "",
              "Bound more than once at module level. Python resolves globals at call time, so the "
              "**last** binding is the one that runs — even if the code using it sits thousands "
              "of lines above. Found automatically, which is how B1 stopped being invisible.",
              "",
              "*Rebind* means the second binding consumes the first (`X = tuple(f(p) for p in "
              "X)`) — deliberate, nothing lost. *Shadow* means it does not, so the earlier "
              "definition is dead code and whoever wrote it did not get what they intended.",
              "", "| File | Name | Bound at | |", "|---|---|---|---|"]
        for rel, name, lines, benign in dup:
            L.append("| `%s` | `%s` | %s | %s |" % (
                rel, name, ", ".join("[%d](../%s#L%d)" % (n, rel, n) for n in lines),
                "rebind" if benign else "⚠ **shadow**"))
        L.append("")
    L += ["---", "",
          "Full symbol map: [MAP.md](MAP.md) · How it works: "
          "[ARCHITECTURE.md](ARCHITECTURE.md) · How to do things: "
          "[OPERATIONS.md](OPERATIONS.md)", ""]
    return "\n".join(L)


def render_map(data):
    total_sym = sum(len(v[1]) for v in data.values())
    L = ["# Map — every file, section and symbol",
         "",
         "<!-- GENERATED by scripts/build_docs.py. Do not edit. -->",
         "",
         "%d files, %d top-level symbols. Section names are the code's own banner comments, not "
         "a taxonomy invented for this document." % (len(data), total_sym),
         "",
         "Tier 1 answers *which 300-line neighbourhood of a 7,000-line file do I want*. Tier 2 is "
         "the appendix — Ctrl+F it.",
         "", "## Contents", ""]
    roles = dict(MODULES)
    for rel, (src, syms, secs) in data.items():
        L.append("- [`%s`](#%s) — %d lines, %d symbols%s"
                 % (rel, anchor(rel), src.count("\n") + 1, len(syms),
                    (" — " + roles[rel]) if rel in roles else ""))
    L += ["", "---", "", "# Tier 1 — sections", ""]
    for rel, (src, syms, secs) in data.items():
        L += ["## `%s`" % rel, ""]
        if rel in roles:
            L += ["*%s*" % roles[rel], ""]
        L.append("%d lines · %d top-level symbols · %d sections"
                 % (src.count("\n") + 1, len(syms), len(secs)))
        L.append("")
        if not secs:
            L += ["*No banner comments in this file.*", ""]
            continue
        L += ["| Lines | Section | Symbols |", "|---|---|---|"]
        for s in secs:
            n = sum(1 for y in syms if s.start <= y.line <= s.end)
            L.append("| [%d–%d](../%s#L%d) | %s | %d |"
                     % (s.start, s.end, rel, s.start, md(s.label), n))
        L.append("")
    routes = [(rel, y) for rel, (_s, syms, _c) in data.items() for y in syms if y.routes]
    if routes:
        L += ["---", "", "# Routes", "",
              "%d rules on %d handlers. No blueprints — this is one module."
              % (sum(len(y.routes) for _r, y in routes), len(routes)),
              "",
              "*Section* is the banner span a handler physically sits in, not a curated feature "
              "grouping — so a route added at the end of one section shows that section's name "
              "even if it belongs to another. Positional, and honest about it.", ""]
        rel0 = routes[0][0]
        secs0 = data[rel0][2]
        cur = None
        L += ["| Route | Methods | Handler | Section |", "|---|---|---|---|"]
        for rel, y in routes:
            sec = next((s.label for s in secs0 if s.start <= y.line <= s.end), "")
            for path, methods in y.routes:
                L.append("| `%s` | %s | [`%s:%d`](../%s#L%d) `%s` | %s |"
                         % (path, methods, rel, y.line, rel, y.line, y.name, md(sec)))
        L.append("")
    L += ["---", "", "# Tier 2 — every symbol", ""]
    for rel, (src, syms, secs) in data.items():
        # "— symbols" keeps this heading's anchor distinct from Tier 1's, so the Contents links
        # above land on the section table rather than whichever heading GitHub deduplicated.
        L += ["## `%s` — symbols" % rel, ""]
        if not syms:
            L += ["*none found.*", ""]
            continue
        L += ["| Symbol | Kind | Line | What |", "|---|---|---|---|"]
        for y in sorted(syms, key=lambda y: y.line):
            note = md(y.doc)[:150] if y.doc else ""
            if y.routes:
                note = ("`" + "` `".join(p for p, _m in y.routes) + "` " + note).strip()
            L.append("| `%s` | %s | [%d](../%s#L%d) | %s |"
                     % (y.name, y.kind, y.line, rel, y.line, note))
        L.append("")
    return "\n".join(L)


def md(s):
    """Neutralise a docstring so it cannot become markup when dropped into a table cell.

    Pipes break the table. Brackets can form a link: web.py::_md_to_html's docstring contains
    the literal example "[text](url)", which rendered as a real -- and broken -- link. Angle
    brackets could inject raw HTML.
    """
    out = (s or "").replace("\r", " ")
    for a, b in (("|", "\\|"), ("[", "\\["), ("]", "\\]"), ("<", "&lt;"), (">", "&gt;")):
        out = out.replace(a, b)
    return out.strip()


def anchor(rel, suffix=""):
    """GitHub's heading-anchor rules: lowercase, drop non-word chars, KEEP underscores.

    An earlier version mapped _ to -, which silently broke every link to scraper/__init__.py.
    """
    base = re.sub(r"[^\w-]", "", rel.replace("/", "").replace(".", "")).lower()
    return base + suffix


def write(rel, text, check, errors):
    """Compare or write. LF unconditionally -- see .gitattributes for why that is load-bearing."""
    path = os.path.join(APP, rel)
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as fh:
            old = fh.read()
    rel = rel.replace(os.sep, "/")          # messages read the same on Windows and the runner
    if check:
        if old is None:
            errors.append("%s does not exist -- run: python scripts/build_docs.py" % rel)
        elif old != text:
            d = list(difflib.unified_diff(old.splitlines(), text.splitlines(),
                                          "committed", "regenerated", lineterm="", n=1))
            errors.append("%s is stale (%d diff lines). Run: python scripts/build_docs.py\n%s"
                          % (rel, len(d), "\n".join("    " + x for x in d[:20])))
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if old == text:
        print("unchanged %s" % os.path.abspath(path))
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("wrote %s" % os.path.abspath(path))
    return True


# =============================================================================================
# DIAGRAM FACTS. Every number and label on every diagram is extracted here, so a diagram cannot
# say something the code stopped doing -- which is the exact failure the old ARCHITECTURE.md had.
# =============================================================================================
def _module_consts(src):
    """{name: value} for module-level assignments literal_eval can evaluate."""
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            try:
                out[n.targets[0].id] = ast.literal_eval(n.value)
            except Exception:
                pass
    return out


def _env_default(src, name):
    """The literal default in `NAME = int(os.environ.get("X", "4000"))`.

    Tuning knobs here are env-overridable, so literal_eval cannot see them. The DEFAULT is the
    honest thing to put on a diagram -- it is what production runs, and it is what a reader
    needs in order to reason about the threshold at all.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    for n in tree.body:
        if not (isinstance(n, ast.Assign) and len(n.targets) == 1
                and getattr(n.targets[0], "id", None) == name):
            continue
        for sub in ast.walk(n.value):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and sub.func.attr == "get" and len(sub.args) == 2 \
                    and isinstance(sub.args[1], ast.Constant):
                v = sub.args[1].value
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return v
    return None


def _sources_count(src):
    """len(SOURCES) without importing. It is a + chain of names bound to list literals.

    JOBSPY_BOARDS is a comprehension rather than a literal, so it contributes 0 -- which is
    also what it contributes at runtime unless JOBSPY_SITES is set.
    """
    consts = _module_consts(src)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return 0
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 \
                and getattr(n.targets[0], "id", None) == "SOURCES":
            names, stack = [], [n.value]
            while stack:
                node = stack.pop()
                if isinstance(node, ast.BinOp):
                    stack += [node.left, node.right]
                elif isinstance(node, ast.Name):
                    names.append(node.id)
            return sum(len(consts.get(nm) or ()) for nm in names)
    return 0


def _gates(src):
    """The intake gates, as (printed label, line where it is counted).

    Lifted from the `tally` dict literal in scraper.main, which is also what the run prints at
    the end -- so the funnel's chute labels are the log's own strings and cannot drift from it.
    A key built by % formatting is rendered with the CONSTANT NAMES left in, which is both
    stable against an env change and more useful: it names the knob to turn.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    node = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.Assign) and len(n.targets) == 1
                 and getattr(n.targets[0], "id", None) == "tally"
                 and isinstance(n.value, ast.Dict)), None)
    if node is None:
        return []
    lines = src.split("\n")
    out = []
    for k in node.value.keys:
        if isinstance(k, ast.Constant):
            label, needle = k.value, k.value
        elif isinstance(k, ast.BinOp) and isinstance(k.left, ast.Constant):
            tmpl = k.left.value
            args = (k.right.elts if isinstance(k.right, ast.Tuple) else [k.right])
            names = [ast.unparse(a).split(".")[-1] for a in args]
            label, needle = tmpl, tmpl.split("%")[0].strip()
            for nm in names:
                label = label.replace("%d", nm, 1).replace("%s", nm, 1)
        else:
            continue
        # The line where it is COUNTED, not where the dict declares it.
        at = ""
        for i in range(node.lineno, len(lines)):
            if "tally[" in lines[i - 1] or (needle and needle in lines[i - 1]
                                            and i > node.end_lineno):
                if needle and needle in lines[i - 1]:
                    at = i
                    break
        out.append((label, at or node.lineno))
    # SORT BY THE LINE THAT COUNTS IT, not by the order the dict declares them. The two differ:
    # the dict lists "blocked company" fifth but it is applied second, and a funnel drawn in
    # declaration order would be telling the reader the wrong sequence of gates.
    return sorted(out, key=lambda g: (isinstance(g[1], str), g[1]))


def diagram_facts(data):
    def lines_of(rel):
        return "{:,}".format(data[rel][0].count("\n") + 1) if rel in data else "?"

    def line_of(rel, name):
        if rel not in data:
            return "?"
        return next((s.line for s in data[rel][1] if s.name == name), "?")

    web = data.get("web.py", ("", [], []))
    routes = [s for s in web[1] if s.routes]
    scr_src = data.get("scraper/__init__.py", ("", [], []))[0]
    consts_scr = _module_consts(scr_src)
    zip_consts = _module_consts(read("scripts/build_deploy_zip.py"))
    inline = _env_default(web[0], "_FEED_INLINE_MAX")

    def count_dir(d, exts=None):
        p = os.path.join(APP, d)
        if not os.path.isdir(p):
            return "?"
        return sum(1 for f in os.listdir(p)
                   if os.path.isfile(os.path.join(p, f))
                   and (exts is None or f.rsplit(".", 1)[-1] in exts))

    scrapers = 0
    for n in ast.walk(ast.parse(scr_src)) if scr_src else []:
        if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) == "SCRAPERS" \
                and isinstance(n.value, ast.Dict):
            scrapers = len(n.value.keys)
            break

    return {
        "web_lines": lines_of("web.py"),
        "core_lines": lines_of("core.py"),
        "db_lines": lines_of("db.py"),
        "scraper_lines": lines_of("scraper/__init__.py"),
        "score_lines": lines_of("scraper/score_jobs.py"),
        "core_sections": len(data.get("core.py", ("", [], []))[2]),
        "routes": sum(len(s.routes) for s in routes),
        "handlers": len(routes),
        "ext_routes": sum(1 for s in routes for p, _m in s.routes if p.startswith("/api/ext/")),
        "templates": count_dir("templates"),
        "ext_files": count_dir("extension"),
        "adapters": scrapers,
        "sources": "{:,}".format(_sources_count(scr_src)),
        "lazyhttp_line": line_of("db.py", "_LazyHTTP"),
        "filter_rows_line": line_of("web.py", "_filter_rows"),
        "matches_line": line_of("static/app.js", "matches"),
        "prefs_match_line": line_of("core.py", "prefs_match"),
        "inline_max": inline if inline is not None else "?",
        "inline_max_name": "_FEED_INLINE_MAX",
        "gates": _gates(scr_src),
        "zip_files": len(zip_consts.get("FILES") or ()),
        "zip_dirs": len(zip_consts.get("DIRS") or ()),
        "schedule": (
            ("09:00 M-F", ".github/workflows/scrape.yml",
             "heavy: sweep, full score, verify_dates, analytics, digest email", "trap"),
            ("13:00 M-F", "bin/cron_scrape.sh", "sweep, new-only score, reposts", "sched"),
            ("16:00 M-F", "bin/cron_scrape.sh", "sweep, new-only score, reposts", "sched"),
        ),
        "max_age": _env_default(scr_src, "MAX_AGE_DAYS") or consts_scr.get("MAX_AGE_DAYS", "?"),
    }


# =============================================================================================
# THE LEDGER. Which data files are source-of-truth and which are disposable -- the single easiest
# fact here to get backwards, so it is asserted against .gitignore rather than trusted.
# =============================================================================================
TRUTH = (
    ("idf.json", "term weights for every score",
     "a PARTIAL rebuild silently re-weights the whole corpus"),
    ("sponsor_counts.json", "federal petition volume per employer", "built by hand from DOL xlsx"),
    ("sponsor_years.json", "petition history per employer", "built by hand from DOL xlsx"),
    ("visa_tags.json", "per-employer visa route tags", "built by hand from LCA/PERM xlsx"),
    ("sponsors.txt", "the sponsor name index", ""),
    ("resume_vocab.json", "spell-check vocabulary for the grader", ""),
    ("resume_keywords.json", "curated skills per track", ""),
    ("company_domains.json", "the employer -> domain map",
     "rebuilt by scripts/build_logos.py --write-domains from Wikidata P856; keyed on core.norm_company, NOT the raw name -- the old raw keying let 'apple' and 'apple, inc.' disagree and build_companies.py kept whichever it read last"),
    ("static/logos/index.json", "which employers have a harvested logo, and its shape",
     "built by scripts/build_logos.py alongside the assets beside it; the page renders a monogram for anything absent, so a stale manifest silently blanks tiles"),
    ("logo_harvest.json", "why each employer has or lacks a logo",
     "the harvest ledger. COMMITTED but never deployed; delete it and the next run re-resolves all 2,695 companies from scratch"),
    ("careers_us.md", "the hand-edited careers/LinkedIn URL list",
     "a SHIPPED RUNTIME ASSET, not a doc; feeds scripts/build_companies.py, markers from scripts/build_careers_md.py"),
    ("companies.json", "the /companies directory",
     "built by scripts/build_companies.py; the sector map is curated -- run --report before editing it"),
    ("resume.txt", "the resume the scraper widens its terms from", "hand-edited, no generator"),
)
CACHE = (
    ("jd_cache.json.gz", "fetched descriptions", "second store of jobs.jd; they have diverged"),
    ("jdmeta.json", "precomputed per-job term maps",
     "built on the Actions runner, whose filesystem is discarded"),
    ("jobs_snapshot.json.gz", "cross-worker feed cache", ""),
    ("jobs_snapshot.json.gz.fp.json", "its fingerprint, without the 46 MB around it",
     "stamped with the snapshot's stat; a mismatch is refused, never repaired"),
    ("last_new_jobs.json", "one run's new rows", "the digest's only input"),
    ("jobs.csv", "the no-credentials fallback store", ""),
)


def _gitignored(names):
    """Ask git which of these it ignores. One call, so it is cheap and authoritative."""
    try:
        r = subprocess.run(["git", "check-ignore", "--no-index"] + list(names),
                           cwd=APP, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return {ln.strip().replace("\\", "/") for ln in (r.stdout or "").splitlines() if ln.strip()}


def ledger(errors):
    """The two columns, plus the assertion that git agrees with the claim."""
    ignored = _gitignored([n for n, _w, _x in TRUTH] + [n for n, _w, _x in CACHE])
    if ignored is not None:
        for name, _w, _x in TRUTH:
            if name in ignored:
                errors.append("ledger says %s is source-of-truth, but .gitignore excludes it"
                              % name)
        for name, _w, _x in CACHE:
            if name not in ignored and os.path.exists(os.path.join(APP, name)):
                errors.append("ledger says %s is a disposable cache, but git does NOT ignore it"
                              % name)
    return TRUTH, CACHE


# =============================================================================================
# EMITTERS. One structural description in docdiagrams.py, rendered as SVG for the offline page
# and as mermaid for the markdown.
# =============================================================================================
TEXT_EL = re.compile(r"<text\b([^>]*)>(.*?)</text>", re.S)
ATTR = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')


def svg_collisions(svg):
    """Text boxes that overlap. An estimate, but it catches the failures that matter.

    Hand-placed coordinates collide silently: the funnel's rescue note was drawn on top of two
    chute labels and looked fine in the source. Width is approximated at 0.56em per character,
    which is close enough for Inter and IBM Plex Mono at these sizes to catch a real overlap
    without flagging things that merely sit close.

    Attributes are parsed one at a time rather than matched in one pattern. A single regex with
    an optional text-anchor group reports every centred label as left-aligned, because the lazy
    quantifier before it would rather skip the group than fill it -- which produced four
    confident false positives the first time this ran.
    """
    boxes = []
    for m in TEXT_EL.finditer(svg):
        a = dict(ATTR.findall(m.group(1)))
        body = re.sub(r"<[^>]+>", "", m.group(2))
        if not body.strip() or "x" not in a or "y" not in a:
            continue
        try:
            x, y, size = int(a["x"]), int(a["y"]), float(a.get("font-size", 12))
        except ValueError:
            continue
        anchor = a.get("text-anchor", "start")
        w = len(body) * size * 0.56
        x0 = x - w / 2 if anchor == "middle" else (x - w if anchor == "end" else x)
        # y is a baseline; the visual box sits above it.
        boxes.append((x0, y - size * 0.82, x0 + w, y + size * 0.22, body.strip()))
    hits = []
    for i in range(len(boxes)):
        ax0, ay0, ax1, ay1, at = boxes[i]
        for j in range(i + 1, len(boxes)):
            bx0, by0, bx1, by1, bt = boxes[j]
            ox = min(ax1, bx1) - max(ax0, bx0)
            oy = min(ay1, by1) - max(ay0, by0)
            # Require a real 2-D overlap, not a shared edge.
            if ox > 4 and oy > 3:
                hits.append((at[:40], bt[:40]))
    return hits


def emit_svgs(facts, check, errors):
    changed = False
    for name, _title, svg_fn, _mm_fn, _cap in dg.DIAGRAMS:
        svg = svg_fn(facts)
        for a, b in svg_collisions(svg)[:4]:
            errors.append("%s.svg: text overlaps -- %r sits on top of %r" % (name, a, b))
        changed = write("docs/img/%s.svg" % name, svg + "\n", check, errors) or changed
    return changed


def emit_map_html(facts, data, check, errors):
    """One file, no network, no build step. Opens by double-click, on a plane, forever."""
    css = read("docs/doc.css")
    truth, cache = ledger(errors)
    n_sym = sum(len(v[1]) for v in data.values())

    def rows(items):
        return "\n".join(
            "<tr><td><code>%s</code></td><td>%s%s</td></tr>"
            % (n, w, (' <em>&mdash; %s</em>' % x) if x else "") for n, w, x in items)

    body = ["<div class=wrap>",
            "<h1>JobMatch &mdash; the shape of it</h1>",
            "<p class=lede>Four pictures. Everything on them is extracted from the code by "
            "<code>scripts/build_docs.py</code>, so a number here cannot be older than the last "
            "commit. %d files, %s top-level symbols, %s routes, %s boards.</p>"
            % (len(data), "{:,}".format(n_sym), facts["routes"], facts["sources"]),
            "<div class=key>",
            "<span><i class=web></i>&#9635; request-scoped &mdash; has session and request</span>",
            "<span><i class=sched></i>&#9719; scheduled &mdash; no session, filesystem "
            "discarded</span>",
            "<span><i class=client></i>&#9723; browser &mdash; someone else's machine</span>",
            "<span><i class=trap></i>&#9888; a documented trap</span>",
            "</div>",
            "<p class=sub>Colour means <strong>where the code runs</strong>. That is not the "
            "app's rule &mdash; in the product, colour means sponsorship &mdash; so the doc "
            "layer deliberately spends the two hues the app gave up, and no hue means two "
            "things anywhere in the system. Every box also carries a glyph and a word, so "
            "nothing depends on colour alone.</p>"]

    for name, title, _svg_fn, _mm_fn, caption in dg.DIAGRAMS:
        body += ['<h2 id="%s">%s</h2>' % (name, title),
                 "<figure>", read("docs/img/%s.svg" % name).strip(),
                 '<figcaption><span class="prov gen">&#10216;generated&#8201;&middot;&#8201;'
                 'build_docs.py&#10217;</span> &nbsp; %s</figcaption>' % caption,
                 "</figure>"]

    body += ['<h2 id="ledger">Source of truth, or disposable cache</h2>',
             "<p class=sub>The easiest fact here to get backwards, so it is not trusted: the "
             "generator asks git whether each file is ignored and fails if the two columns "
             "disagree with this table.</p>",
             "<div class=two>",
             "<div><h3>&#9635; Committed &mdash; source of truth</h3><table><tbody>",
             rows(truth), "</tbody></table></div>",
             "<div><h3>&#9723; Ignored &mdash; disposable</h3><table><tbody>",
             rows(cache), "</tbody></table></div>",
             "</div>",
             '<div class="note">Losing anything in the left column loses work. Deleting '
             'anything in the right column costs one run.</div>']

    body += ['<h2 id="where">Where to look first</h2>',
             "<p class=sub>This page is for the shape. For &ldquo;X is broken, which "
             "file&rdquo;, the lookup table is "
             '<a href="INDEX.md">INDEX.md</a>, and the full symbol map is '
             '<a href="MAP.md">MAP.md</a>.</p>',
             "<table><thead><tr><th>Question</th><th>Answer</th></tr></thead><tbody>",
             "<tr><td>What must I know before touching anything?</td>"
             '<td><a href="../CLAUDE.md">CLAUDE.md</a></td></tr>',
             "<tr><td>How does it work, where do I start reading?</td>"
             '<td><a href="ARCHITECTURE.md">ARCHITECTURE.md</a></td></tr>',
             "<tr><td>X is broken &mdash; which file?</td>"
             '<td><a href="INDEX.md">INDEX.md</a></td></tr>',
             "<tr><td>Where is a specific function?</td>"
             '<td><a href="MAP.md">MAP.md</a></td></tr>',
             "<tr><td>How do I deploy, what env var, what runs at 13:00?</td>"
             '<td><a href="OPERATIONS.md">OPERATIONS.md</a></td></tr>',
             "<tr><td>What changed recently, what is still open?</td>"
             '<td><a href="SESSION_HANDOFF_PROMPT.md">SESSION_HANDOFF_PROMPT.md</a></td></tr>',
             "</tbody></table>",
             "<footer>Generated by <code>scripts/build_docs.py</code> from the source in this "
             "repository. Self-contained: no network, no build step, no CDN. "
             "Regenerate with <code>python scripts/build_docs.py</code>; CI fails if this file "
             "and the code disagree.</footer>",
             "</div>"]

    html = ("<!doctype html>\n<html lang=en>\n<head>\n<meta charset=utf-8>\n"
            '<meta name=viewport content="width=device-width,initial-scale=1">\n'
            "<title>JobMatch &mdash; the shape of it</title>\n"
            "<!-- GENERATED by scripts/build_docs.py. Do not edit. -->\n"
            "<style>\n%s\n</style>\n</head>\n<body>\n%s\n"
            "<script>\n"
            "/* Follow the OS theme. Three lines, no dependency, and it degrades to light. */\n"
            "var m = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)');\n"
            "function sync(){ document.documentElement.setAttribute('data-theme',"
            " m && m.matches ? 'dark' : 'light'); }\n"
            "sync(); if (m && m.addEventListener) m.addEventListener('change', sync);\n"
            "</script>\n</body>\n</html>\n" % (css.strip(), "\n".join(body)))
    return write("docs/map.html", html, check, errors)


MARK = re.compile(r"(<!-- DIAGRAM: (\w+) -->)(.*?)(<!-- /DIAGRAM -->)", re.S)


def emit_architecture(facts, check, errors):
    """Inject the mermaid blocks into the hand-written ARCHITECTURE.md, between markers.

    The prose stays an ordinary markdown file you can edit; only the fenced blocks are managed.
    --check then verifies the managed regions, so the diagrams cannot drift while the prose
    around them stays free.
    """
    rel = "docs/ARCHITECTURE.md"
    if not os.path.exists(os.path.join(APP, rel)):
        errors.append("%s is missing -- it holds the prose the diagrams sit in" % rel)
        return False
    src = read(rel)
    mm = {name: fn for name, _t, _s, fn, _c in dg.DIAGRAMS}
    seen = set()

    def sub(m):
        name = m.group(2)
        seen.add(name)
        if name not in mm:
            errors.append("%s has a DIAGRAM marker for unknown diagram '%s'" % (rel, name))
            return m.group(0)
        return "%s\n\n```mermaid\n%s\n```\n\n%s" % (m.group(1), mm[name](facts), m.group(4))

    out = MARK.sub(sub, src)
    for name in mm:
        if name not in seen:
            errors.append("%s has no <!-- DIAGRAM: %s --> marker, so that diagram is not in the "
                          "prose" % (rel, name))
    return write(rel, out, check, errors)


def unmapped():
    """Modules in the app root that nothing maps -- a soft nudge, never a failure."""
    mapped = {m[0] for m in MODULES} | {"app.py", "manage_users.py", "speedtest.py",
                                        "passenger_wsgi.py"}
    out = []
    for f in sorted(os.listdir(APP)):
        if f.endswith(".py") and f not in mapped and not f.startswith("test_"):
            out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser(description="Generate docs/INDEX.md and docs/MAP.md.")
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed docs are stale or name something gone")
    ap.add_argument("--list-refs", action="store_true",
                    help="print every {module::symbol} the prose uses, resolved")
    args = ap.parse_args()

    data = collect()
    errors = []

    if args.list_refs:
        seen = set()
        for r in INDEX:
            for field in (r.symptom, r.target, r.coupled):
                for m in REF.finditer(field):
                    seen.add((m.group(1), m.group(2)))
        for kw in KNOWN_WRONG:
            for field in (kw.detail, kw.workaround):
                for m in REF.finditer(field):
                    seen.add((m.group(1), m.group(2)))
        bad = 0
        for rel, name in sorted(seen):
            line = None
            if rel in data:
                line = next((s.line for s in data[rel][1] if s.name == name), None)
            print("%-4s %-26s %-34s %s" % ("ok" if line else "MISS", rel, name,
                                           line if line else "-"))
            bad += 0 if line else 1
        print("\n%d refs, %d unresolved" % (len(seen), bad))
        return 1 if bad else 0

    facts = diagram_facts(data)

    # The diagram assertions. A picture whose numbers came from the code can still be wrong if
    # the THING it depicts stopped existing, so each one is checked rather than assumed.
    if not facts["gates"]:
        errors.append("could not find the tally dict in scraper/__init__.py -- the funnel's "
                      "chute labels come from it, so the diagram would be inventing them")
    if facts["inline_max"] == "?":
        errors.append("could not read _FEED_INLINE_MAX's default from web.py -- the triplet "
                      "diagram claims a threshold it cannot verify")
    for key, what in (("filter_rows_line", "web.py::_filter_rows"),
                      ("matches_line", "static/app.js::matches"),
                      ("prefs_match_line", "core.py::prefs_match")):
        if facts[key] == "?":
            errors.append("the triplet diagram names %s, which no longer resolves" % what)
    if not facts["zip_files"]:
        errors.append("could not read FILES from scripts/build_deploy_zip.py -- the deploy "
                      "diagram would understate what ships")
    if facts["routes"] != 82:
        print("note: the route count moved to %s (the docs will say so)." % facts["routes"])

    idx = render_index(data, errors)
    mp = render_map(data)

    changed = write(INDEX_OUT, idx, args.check, errors)
    changed = write(MAP_OUT, mp, args.check, errors) or changed
    changed = emit_svgs(facts, args.check, errors) or changed
    changed = emit_map_html(facts, data, args.check, errors) or changed
    changed = emit_architecture(facts, args.check, errors) or changed

    extra = unmapped()
    if extra:
        print("\nnote: %d module(s) in the app root are not in MODULES: %s"
              % (len(extra), ", ".join(extra)))

    if errors:
        print("\n%d problem(s):" % len(errors))
        for e in errors:
            print("  - %s" % e)
        if args.check:
            print("\n::error::docs are stale or inconsistent. Run "
                  "'python scripts/build_docs.py' and commit the result.")
        return 1
    print("\nok: %d files, %d symbols, %d index rows, %d known-wrong entries."
          % (len(data), sum(len(v[1]) for v in data.values()), len(INDEX), len(KNOWN_WRONG)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
