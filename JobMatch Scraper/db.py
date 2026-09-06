"""
db.py — storage layer for the job tool.

One PostgREST-shaped interface over four possible backends, resolved lazily by
_LazyHTTP.__getattr__ below. Callers never know which one they got. No SDK — plain
`requests` and psycopg — so it installs cleanly everywhere, including Python 3.14.

Resolution order, most specific first:
  1. PG_DSN                          -> pgrest.Session, direct psycopg. THIS IS THE CPANEL APP.
  2. DB_PROXY_URL + DB_PROXY_SECRET  -> dbproxy.Session, HMAC-signed HTTPS to POST /api/db.
                                        How GitHub Actions and a laptop reach the database,
                                        since PG_DSN is loopback-only.
  3. nothing                         -> local jobs.csv + user_jobs.json, zero setup.

There was a step between 2 and 3 until 2026-09-01: SUPABASE_URL + SUPABASE_KEY, a Supabase
PostgREST session. The database moved to cPanel Postgres on 2026-08-15 and it sat there
vestigial for two weeks, still resolving from a stale .streamlit/secrets.toml, still the
DEFAULT destination for any process that had not configured 1 or 2. Removed.

A HALF-SET DB_PROXY_* pair raises rather than silently falling through — see
_check_backend_intent, and set DB_REQUIRE to pin the backend you meant.

There was a third transport, an unauthenticated Supabase REST session, and it was the
default fall-through: every misconfiguration landed there silently. Removed 2026-09-01.

Note `has_remote_db()` answers "is there a remote database at all" and is True for
BOTH remotes. `backend_name()` is the one that says which.

Setup, schema and env vars: docs/OPERATIONS.md. Live DDL: schema.sql.
"""
import os
import csv
import json
import re
import sys
import time
import functools
import collections
import datetime
import hashlib



_http_session = None


class _LazyHTTP:
    """Defers building the requests.Session (and the ~1 s `import requests`) until the
    first actual DB call, so importing db.py stays cheap on a cold Passenger start. All
    `_http.get/post/...` call sites keep working unchanged."""
    def __getattr__(self, name):
        global _http_session
        if _http_session is None:
            # TWO transports, one interface, checked most-specific first. PG_DSN wins because
            # a process that can reach the database directly should never route through HTTP to
            # reach itself — the cPanel app sets PG_DSN, the scraper sets DB_PROXY_*, and
            # neither should ever have both.
            #
            # There used to be a third: an unauthenticated fall-through to Supabase REST, which
            # is what made every misconfiguration silent. It is gone (2026-09-01), and so is the
            # `or _make_http()` that expressed it — a caller that reaches here without a
            # configured backend gets an exception naming the two variables, not a session
            # pointed at a database this project left on 2026-08-15.
            if PG_DSN:
                import pgrest
                _http_session = pgrest.Session(PG_DSN)
            else:
                import dbproxy
                _check_backend_intent()
                _http_session = dbproxy.client_from_env()
                if _http_session is None:
                    raise RuntimeError(
                        "no database backend is configured: set PG_DSN (on the box) or both "
                        "DB_PROXY_URL and DB_PROXY_SECRET (off it). has_remote_db() answers "
                        "False in this state, so a caller that guards on it never reaches "
                        "here — if you are seeing this, that guard is missing.")
        return getattr(_http_session, name)


def _check_backend_intent():
    """Refuse to silently downgrade to a backend nobody asked for.

    `dbproxy.client_from_env()` returns a Session only when BOTH DB_PROXY_URL and
    DB_PROXY_SECRET are non-empty. A missing or typo'd secret is therefore indistinguishable
    from "no proxy configured" at the call site above.

    WHY THIS STILL MATTERS NOW THAT THE SILENT FALLBACK IS GONE. Until 2026-09-01 that None was
    read as "use Supabase", so a half-set pair redirected every write to the database this
    project left on 2026-08-15 and the run reported success — measured 2026-08-19, with
    DB_PROXY_URL set and DB_PROXY_SECRET empty, `backend_name()` answered "Supabase". That
    specific hazard is now impossible; what remains is that a half-set pair would otherwise
    raise deep inside the first query with a confusing message, or fall to the local CSV. Both
    are worse than failing here, by name, before anything is read or written.

    Same failure family as the PG_DSN-read-before-.env bug documented below: the configuration
    said one thing, the process did another, and nothing raised. Two rules:

      * HALF-SET IS A MISCONFIGURATION, never a request for the old backend.
      * DB_REQUIRE lets a caller state the backend it expects, so CI and cron fail in one second
        instead of spending 26 minutes writing somewhere harmless-looking. Unset, nothing changes.
    """
    url = os.environ.get("DB_PROXY_URL") or ""
    secret = os.environ.get("DB_PROXY_SECRET") or ""
    if bool(url) != bool(secret):
        have, missing = ("DB_PROXY_URL", "DB_PROXY_SECRET") if url else ("DB_PROXY_SECRET",
                                                                        "DB_PROXY_URL")
        raise RuntimeError(
            "DB_PROXY is half-configured: %s is set but %s is empty, so this process has no "
            "usable remote backend and would fall to the local CSV. Set %s, or unset both if "
            "the local fallback is what you meant." % (have, missing, missing))

    want = (os.environ.get("DB_REQUIRE") or "").strip().lower()
    if not want:
        return
    got = "proxy" if (url and secret) else "csv"
    # `supabase` is still ACCEPTED and always mismatches, deliberately: a cron or CI job pinned
    # to DB_REQUIRE=supabase must fail loudly saying the backend is gone, not be told its
    # configuration is malformed. Remove it once nothing in .github/workflows sets it.
    if want not in ("proxy", "pg", "supabase", "csv"):
        raise RuntimeError("DB_REQUIRE=%r is not one of proxy / pg / csv" % want)
    if want == "supabase":
        raise RuntimeError(
            "DB_REQUIRE=supabase, but the Supabase transport was removed on 2026-09-01. This "
            "process talks to %s. Use proxy, pg or csv." % backend_name())
    if want == "pg":
        # PG_DSN is handled by the branch above and never reaches here, so arriving with
        # DB_REQUIRE=pg means PG_DSN was empty — exactly the cPanel misconfiguration to catch.
        raise RuntimeError(
            "DB_REQUIRE=pg but PG_DSN is empty, so this process would talk to %s instead of a "
            "local Postgres. Set PG_DSN (cPanel sets it in .env)." % backend_name())
    if want != got:
        raise RuntimeError(
            "DB_REQUIRE=%s but this process resolved to %r (%s). Refusing to run against a "
            "backend the caller did not ask for." % (want, got, backend_name()))


_http = _LazyHTTP()


def _load_env_file(path=".env"):
    """Load KEY=VALUE pairs from a .env file into os.environ (set-if-absent), so every
    module reads ONE config source: db's PG_DSN / DB_PROXY_*, web's GH_TOKEN, the scraper's
    ADZUNA_APP_ID/ADZUNA_APP_KEY. No python-dotenv dependency; comments and blank
    lines ignored; real environment variables always win; never raises."""
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    k = k.strip()
                    if k and k not in os.environ:
                        os.environ[k] = v.strip().strip('"').strip("'")
    except Exception:
        pass


_load_env_file()       # db is imported first by every entry point (web, scraper, scorer)

# READ AFTER _load_env_file(), and that is the whole point of it being here rather than beside
# _LazyHTTP where it logically belongs. This was a module constant assigned ~45 lines above the
# call that loads .env, so a PG_DSN set in .env -- which is exactly how cPanel configures the
# app -- was read as empty every time, and the app silently kept talking to Supabase while its
# configuration said otherwise. Nothing failed; the row counts even matched, because the two
# databases are identical copies. DB_PROXY_* escaped the same bug only by accident: they are
# read inside functions, at call time, rather than at import.
# Set PG_DSN and this module talks to a Postgres server DIRECTLY instead of to Supabase's
# PostgREST — for a self-hosted database (cPanel, a VPS) or any managed Postgres that isn't
# Supabase. pgrest.py implements the five HTTP verbs over psycopg and translates the query
# language, so none of the 75 functions below change and neither does anything that calls them.
# Unset it and you are back on Supabase, which is what makes it safe to try.
#
#   PG_DSN="postgresql://user:pw@localhost:5432/dbname"
PG_DSN = os.environ.get("PG_DSN") or ""

JOBS_CSV = "jobs.csv"
ACTIONS_FILE = "user_jobs.json"
TABLE = "jobs"
FIELDS = ["found_date", "title", "company", "location", "url",
          "sponsors_h1b", "match_score", "status",
          "posted_verified", "posted_confidence",
          # --- derived by scraper/score_jobs.py so the feed can filter on them ---
          "loc_state", "loc_metro", "remote",
          "salary_min", "salary_max", "salary_period",
          "last_seen", "is_active", "miss_count",
          # --- derived from the JD, same reason: see JOBS_DERIVED_SQL below ---
          "exp_max_years", "sponsor_jd", "sponsor_reason", "jd_terms",
          # Written by the DATABASE (default + triggers), never by us — see add_jobs(). Listed
          # here so the CSV path round-trips it and dedupe_urls carries it across a URL move.
          "first_seen"]

# One-time SQL for the derived columns above. Surfaced in the app (and printed by
# score_jobs) when a write fails because they don't exist yet — same self-serve pattern
# as APPLICATIONS_SQL. All `if not exists`, so it's safe to re-run.
JOBS_DERIVED_SQL = (
    "-- Location + pay + liveness, derived from data already stored on each job.\n"
    "alter table public.jobs add column if not exists loc_state text;\n"
    "alter table public.jobs add column if not exists loc_metro text;\n"
    "alter table public.jobs add column if not exists remote boolean;\n"
    "alter table public.jobs add column if not exists salary_min integer;\n"
    "alter table public.jobs add column if not exists salary_max integer;\n"
    "alter table public.jobs add column if not exists salary_period text;\n"
    "alter table public.jobs add column if not exists last_seen date;\n"
    "alter table public.jobs add column if not exists is_active boolean default true;\n"
    "-- consecutive successful fetches of its own board a job has been absent from\n"
    "alter table public.jobs add column if not exists miss_count integer default 0;\n"
    "create index if not exists jobs_loc_state_idx on public.jobs (loc_state);\n"
    "create index if not exists jobs_is_active_idx on public.jobs (is_active);\n"
    "\n"
    "-- first_seen: the date a job first entered THIS database. NOT found_date, which is the\n"
    "-- publisher's posting date (or our scrape stamp). Some employers publish no posting date\n"
    "-- anywhere -- Tesla's careers API has no date field at all -- so without this their cards\n"
    "-- show no date and slip through every 'posted within' filter.\n"
    "alter table public.jobs add column if not exists first_seen date;\n"
    "-- Backfill: an existing date is the best evidence of when we first saw the row. found_date\n"
    "-- is TEXT, either 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM', hence substring rather than a cast.\n"
    "update public.jobs set first_seen = substring(found_date from 1 for 10)::date\n"
    " where first_seen is null and found_date ~ '^\\d{4}-\\d{2}-\\d{2}';\n"
    "-- Rows with no date at all: we only know it was on or before today, so today is the floor.\n"
    "update public.jobs set first_seen = current_date where first_seen is null;\n"
    "-- Default LAST. `add column ... default current_date` as ONE statement would have rewritten\n"
    "-- every existing row with today, which is the lie this column exists to avoid.\n"
    "alter table public.jobs alter column first_seen set default current_date;\n"
    "\n"
    "-- JD-derived signals the FEED filters on. Same reasoning as the location/pay block above,\n"
    "-- and the same reason match_score is a column: jdmeta.json is gitignored, is not in the\n"
    "-- cPanel file list, and scripts/build_deploy_zip.py excludes it. It is also NOT regenerated\n"
    "-- on the web host -- the scraper runs on an ephemeral GitHub Actions runner whose disk is\n"
    "-- discarded when the run ends. So live, web._jdmeta was empty, every row fell back to\n"
    "-- _EMPTY_META, and the experience and 'hide no-sponsorship' filters were silent no-ops.\n"
    "-- A column reaches the live site through Supabase with no file deploy.\n"
    "--\n"
    "-- exp_max_years is the HIGHEST year count the JD states, not the lowest: '8+ years of\n"
    "-- engineering experience; 2 years of SQL preferred' is an 8-year job, and reading the floor\n"
    "-- let a senior req hide behind its most junior line item. NULL means the JD states no\n"
    "-- number at all, which the filter must KEEP.\n"
    "alter table public.jobs add column if not exists exp_max_years integer;\n"
    "-- '' = no signal, 'blocked' = the JD rules a visa candidate out, 'open' = it sponsors.\n"
    "alter table public.jobs add column if not exists sponsor_jd text;\n"
    "-- LOAD-BEARING, not display copy: core.visa_tags_for_posting substring-matches this against\n"
    "-- core._BLOCKS_EVERYONE to decide whether a blocked posting keeps STEM-OPT or loses every\n"
    "-- route. The user-visible wording is fixed in static/app.js; this stays machine-readable.\n"
    "alter table public.jobs add column if not exists sponsor_reason text;\n"
    "-- No index on any of these: the feed loads the corpus and filters it in Python, which\n"
    "-- is also why jobs_loc_state_idx above goes unused.\n"
    "\n"
    "-- The JD's keyword weights (core.pack_analyzed), which is what lets the feed score a job\n"
    "-- against ANY resume. Without it web.user_scores has nothing to score with and falls back\n"
    "-- to match_score -- the baseline the cron computed against the repo's own resume.txt --\n"
    "-- so EVERY signed-in user saw the owner's match percentages instead of their own.\n"
    "-- ~600 B/row packed, ~3.8 MB gzipped across the corpus, read once per scrape.\n"
    "--\n"
    "-- TEXT, deliberately NOT jsonb. jsonb normalizes an object and does not preserve key\n"
    "-- order, which would break this twice over: score_jobs diffs the stored value against the\n"
    "-- one it just built to decide whether to write (a reordered read would re-upsert the whole\n"
    "-- corpus on every run), and the key order IS analyze_jd's frozen term order, which breaks\n"
    "-- ties between equal-weight terms in the panel's skill lists. Nothing ever queries inside\n"
    "-- this column, so jsonb buys nothing to pay for that with. Postgres TOASTs it out of the\n"
    "-- main row either way, so the columns above stay cheap to read on their own.\n"
    "alter table public.jobs add column if not exists jd_terms text;\n"
    "\n"
    "-- PROVENANCE, and the reason this migration exists. jd_fp is the fingerprint of the\n"
    "-- description this row STORES; facts_fp is the fingerprint of the text its derived\n"
    "-- columns were READ FROM. Equal means the reading is about the text we hold. Different\n"
    "-- means it is about a text we replaced and never re-read, which is what happened to\n"
    "-- ~2,300 rows on 2026-09-04 and took a 200-second corpus scan to find. Now it is:\n"
    "--     select url from public.jobs where jd_fp is distinct from facts_fp;\n"
    "--\n"
    "-- Both NULL is the normal state for a row we hold no description for, and `is distinct\n"
    "-- from` is the operator that reads that as agreement rather than as a mismatch.\n"
    "alter table public.jobs add column if not exists jd_fp text;\n"
    "alter table public.jobs add column if not exists facts_fp text;\n"
    "-- PARTIAL, because the only query anyone runs against this pair is the disagreement\n"
    "-- above, and on a healthy corpus that matches almost nothing. A full index on two\n"
    "-- 32-char columns would cost ~3 MB to answer a question about a handful of rows.\n"
    "create index if not exists jobs_facts_stale_idx on public.jobs (url)\n"
    "  where jd_fp is distinct from facts_fp;\n"
    "\n"
    "-- LAST. Without this PostgREST answers from its cached schema and every column added above\n"
    "-- reads as missing until it happens to reload.\n"
    "notify pgrst, 'reload schema';\n")

def has_remote_db():
    """True when there is a REMOTE database to talk to, of either kind.

    Called `using_supabase` until 2026-09-01, a name that had not been true since the move to
        cPanel Postgres on 2026-08-15: it answered yes for all three transports, so every caller read
    it as "not the local CSV fallback" while the name said something else. Its own docstring
    argued the rename was not worth ~40 call sites; deleting the Supabase transport settled that,
    because a predicate named after a backend that no longer exists is worse than a wide diff.
    It was 99 sites in the end.

    `db.backend_name()` is still the one that tells you WHICH backend."""
    return bool(PG_DSN or (os.environ.get("DB_PROXY_URL")
                           and os.environ.get("DB_PROXY_SECRET")))


def backend_name():
    """Where writes are actually going, for anything that prints it.

    Four scripts once ended a run with `"Supabase" if db.using_supabase() else "jobs.csv"`, which
    was true when Supabase was the only remote there was. It became the wrong question: that
    helper means "is there a remote database at all", so a run writing 714 descriptions into a
    Postgres on cPanel still signed off with "-> Supabase". The log was the only record of that
    run, and it named the wrong database. Both the helper and that backend are gone now; this
    function is what remains of the lesson.

    Short enough to sit at the end of a summary line, specific enough to be worth reading.
    """
    if PG_DSN:
        for part in PG_DSN.split():
            if part.startswith("dbname="):
                return "Postgres (%s)" % part.split("=", 1)[1]
        return "the local Postgres"
    proxy = os.environ.get("DB_PROXY_URL") or ""
    if proxy and os.environ.get("DB_PROXY_SECRET"):
        host = proxy.split("://", 1)[-1].split("/")[0]
        return "the app at %s" % host if host else "the app proxy"
    return JOBS_CSV                     # nothing remote configured: the local CSV fallback


def _rest(path=""):
    """The PostgREST-shaped path both transports parse.

    pgrest.Session and dbproxy.Session read only the part after /rest/v1/ to find the table, so
    the host is a placeholder and always has been for them. With Supabase gone there is no real
    host left to build, so the placeholder is unconditional and every call site is unchanged."""
    return "pg://local/rest/v1/%s" % path


def _headers(extra=None):
    """Request headers for the two transports.

    The apikey / Authorization pair went with Supabase. Neither remaining transport ever read
    them — checked: pgrest.Session and dbproxy.Session both pull `Prefer` and nothing else — so
    dropping them changes no behaviour, and it stops every call site passing a credential to a
    function that discards it."""
    h = {"Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _fetch_all(table, params):
    """GET every row from a PostgREST table, paging past the server's per-request row
    cap (default 1000). Without this, a table that grows beyond the cap silently
    truncates — the feed and scorer would just never see the newest rows. Ordered by
    a stable column so pages don't shift mid-walk."""
    rows, offset, page = [], 0, 1000
    while True:
        p = dict(params)
        p.setdefault("order", "url")
        p["limit"], p["offset"] = page, offset
        r = _http.get(_rest(table), headers=_headers(), params=p, timeout=30)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            return rows
        offset += page


def table_count(table, params=None):
    """Exact row count WITHOUT downloading a single row.

    PostgREST answers a HEAD carrying `Content-Range: 0-999/19268` when asked for count=exact;
    we take the tail. Everything in this file counted by walking the table until now
    (`len(existing_urls())` pages 19k rows to learn one integer), which is fine once a day in a
    scraper and wrong on a page render.

    Returns None — never 0 — when the count is unavailable (missing table, HTTP error, or
    PostgREST's `*/*` unknown form), so a caller can render "—" instead of a confident zero.
    Every admin panel below distinguishes those two, and a delete preview that reports 0 rows
    because the request failed is exactly how you delete the wrong thing on the retry.

    HEAD needs no special plumbing on either transport.
    `params` takes the usual PostgREST filters, e.g. {"company": "eq.Tesla"}.
    """
    if not has_remote_db():
        return None
    p = dict(params or {})
    # Any column works — HEAD returns no body regardless — but naming one keeps the request
    # small and avoids `select=*` expanding on wide tables.
    p.setdefault("select", "url" if table == TABLE else "*")
    try:
        r = _http.head(_rest(table), headers=_headers({"Prefer": "count=exact"}),
                       params=p, timeout=30)
        if r.status_code >= 400:
            return None
        rng = r.headers.get("Content-Range") or ""       # "0-999/19268" | "*/0" | "*/*"
        total = rng.rsplit("/", 1)[-1] if "/" in rng else ""
        return int(total) if total.isdigit() else None
    except Exception:
        return None


# jobs_fingerprint()'s "don't know". A NON-EMPTY and therefore TRUTHY tuple, so every caller
# tests fp[0] is not None rather than the tuple itself — see the note in web._base_rows.
FP_UNKNOWN = (None, "", None)


def jobs_fingerprint():
    """(row_count, newest_first_seen, scored_count) — a near-free "has the corpus changed?" probe.

    No part of it returns rows: the two counts ride HEADs in table_count(), and the date is a
    one-row ordered select. Together they let a caller revalidate a cache instead of re-reading
    it — the difference between ~0 bytes and the ~10.7 MB a full feed read costs at 19k rows.

    `first_seen` (the date a row entered THIS database), not `last_seen`: last_seen is NULL on
    every row in the live table — measured 0 of 19,268 non-null — so it fingerprints nothing.
    first_seen is populated on all of them and advances whenever a job is inserted, while the
    count moves on inserts and on the 30-day prune. A prune and an insert of the same size
    therefore still register, because the new rows carry a newer first_seen.

    THE THIRD COMPONENT IS THE ONE THAT CAN SEE AN UPDATE, and without it the feed stated a
    falsehood. The first two move on INSERTS only. A scrape inserts a bare row (url, title,
    company, location), which moves both and freezes a snapshot; the JD fetch and the score pass
    then write `jd`, `jd_terms` and `match_score` onto that same row with an UPDATE, which moves
    neither. So the snapshot kept jd_terms NULL, web._row_pending read that as "unreadable", and
    the card said "JD pending" at score 0 — while the job page, which reads the description live
    on the url key, showed a real percentage for the same posting. Worse than merely stale: past
    _JOBS_TTL the probe re-confirmed "unchanged" and restamped the sidecar, so the wrong answer
    renewed itself hourly and only an INSERT ever broke the loop.

    Measured on the live table 2026-09-04: all 4,371 rows inserted that day were fully scored in
    the database, and every one of them passed through that window. The filter is free — twelve
    interleaved reps put a filtered HEAD within noise of an unfiltered one (255 ms vs 274 ms min,
    both at the round-trip floor) — so this buys correctness for one extra request on a probe
    that only runs once per TTL.

    `not.is.null` rather than its inverse, because it decomposes cleanly: the count moves on
    inserts and prunes, first_seen moves on inserts, and this moves on score writes and nothing
    else. Rows that will never be scored (no description was ever fetched — 178 of them here)
    hold it still, which is correct: nothing about them has changed.

    Returns (None, "", None) when ANY part is unavailable. Callers MUST read that as "don't know"
    and refetch, never as "unchanged" — a probe that fails while the DB is briefly unreachable
    would otherwise pin a stale feed in place indefinitely. ALL OR NOTHING, because a partial
    tuple is worse than none: two different unknown scoring states would compare equal and the
    first one's rows would be served for the life of the worker.
    """
    if not has_remote_db():
        return FP_UNKNOWN
    n = table_count(TABLE)
    if n is None:
        return FP_UNKNOWN
    # THE THIRD COMPONENT counts rows carrying an analysis, and it has to keep counting
    # the same thing after the analysis moves tables -- otherwise the fingerprint stops
    # moving when a scoring run writes, and web's row cache serves rows whose
    # score_pending flag is a scrape old. n_terms > 0 is the same predicate as
    # `jd_terms is not null` was: mirror_job_terms writes NULL for an empty string.
    if job_terms_ready():
        scored = table_count(JOB_TERMS_TABLE, {"n_terms": "gt.0"})
    else:
        scored = table_count(TABLE, {"jd_terms": "not.is.null"})
    if scored is None:
        return FP_UNKNOWN
    try:
        r = _http.get(_rest(TABLE), headers=_headers(),
                      params={"select": "first_seen", "order": "first_seen.desc.nullslast",
                              "limit": 1}, timeout=15)
        if r.status_code >= 400:
            return FP_UNKNOWN
        rows = r.json() or []
        return (n, (rows[0].get("first_seen") or "") if rows else "", scored)
    except Exception:
        return FP_UNKNOWN


def _upsert(rows, chunk=200, keys=None, table=None, pk="url"):
    """Insert/merge rows on the `url` primary key (PostgREST upsert), in chunks with a soft retry.
    A single huge merge-upsert (a full re-score, or a big backlog of new jobs after the scheduled
    scrape has been down) is one giant statement; splitting it keeps each write small. On top of
    that, free-tier Supabase occasionally goes through a minute or two of HTTP 500s under load, so
    each chunk gets a couple of extra spaced-out attempts before we give up (and then we surface
    the real response body, not a bare RetryError). PostgREST needs every object in a bulk write
    to share the SAME keys, so we normalize to the union of keys (missing -> None) — or to the
    group `keys` names, which a caller writing one column group in several batches must pass.

    `table` and `pk` default to `jobs` / `url`, which is what every caller wanted while jobs
    was the only table written through here. They are parameters rather than a second
    function because the retry-with-backoff, the duplicate-key merge and the key-union
    normalisation are the hard parts and none of them are table-specific -- a second copy
    would be a second place for the "an all-None column is never sent" trap to be got wrong.

    THE THREE PLACES pk APPEARS ARE NOT INTERCHANGEABLE and all three used to say "url":
    the ON CONFLICT target, the merge below that collapses duplicate keys within one batch,
    and the passthrough test. Miss the merge and Postgres raises 21000 ("cannot affect row a
    second time") on the whole batch; miss the passthrough test and every row of a table
    with a different primary key is treated as un-mergeable and the duplicates reach the
    server anyway.
    """
    if not rows:
        return
    # A single PostgREST upsert can't touch the same `url` twice — Postgres raises 21000
    # ("ON CONFLICT DO UPDATE command cannot affect row a second time") and 500s the whole
    # batch. Two different boards can legitimately return the same job URL in one run, so
    # collapse duplicates here first, merging each url's non-null fields (later rows win).
    merged = {}
    order = []
    passthrough = []
    for r in rows:
        u = r.get(pk)
        if u is None:
            passthrough.append(r)
            continue
        if u not in merged:
            merged[u] = {}
            order.append(u)
        merged[u].update({k: v for k, v in r.items() if v is not None})
    rows = [merged[u] for u in order] + passthrough
    # A named group is sent on every row, whatever this batch happens to hold. Inferred instead,
    # the union is computed per CALL and the merge above has just dropped every None — so a batch
    # in which some column is None on all of its rows does not send that column at all, and a
    # stale value the caller meant to CLEAR survives. Not only a batching hazard: a 3-row payload
    # in which none of the three states an experience floor could never clear one either.
    keys = sorted(keys) if keys else sorted({k for r in rows for k in r})
    for i in range(0, len(rows), chunk):
        payload = json.dumps([{k: r.get(k) for k in keys} for r in rows[i:i + chunk]])
        last = ""
        for attempt in range(4):           # ~0 + 3 + 6 + 12s of backoff rides out a transient 500 window
            try:
                resp = _http.post(
                    _rest(table or TABLE),
                    headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                    params={"on_conflict": pk}, data=payload, timeout=60)
                if resp.status_code < 400:
                    break
                last = "%s: %s" % (resp.status_code, (resp.text or "")[:300])
            except Exception as e:         # RetryError / connection reset -> treat as retryable
                last = repr(e)[:300]
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError("Supabase upsert failed after retries: %s" % last)


# ---------------- local-file helpers (fallback) ----------------
def _read_csv():
    if not os.path.exists(JOBS_CSV):
        return []
    with open(JOBS_CSV, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("url")]


def _write_csv(rows):
    with open(JOBS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _load_actions():
    if os.path.exists(ACTIONS_FILE):
        try:
            return json.load(open(ACTIONS_FILE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_actions(a):
    json.dump(a, open(ACTIONS_FILE, "w", encoding="utf-8"))


# ---------------- public API (scraper / score_jobs / app use these) ----------------
# Columns the feed needs, named explicitly so the select can skip the huge `jd` text.
# _FEED_COLS_CORE has existed since the first schema; _FEED_COLS_OPT are added by later
# migrations, so a select naming them 400s until those have been run — load_jobs falls back
# to CORE in that case, which is what keeps the feed alive on an un-migrated database.
_FEED_COLS_CORE = "url,found_date,title,company,location,sponsors_h1b,match_score,status"
_FEED_COLS_OPT = ("posted_verified", "loc_state", "loc_metro", "remote",
                  "salary_min", "salary_max", "salary_period",
                  "is_active", "last_seen", "miss_count", "first_seen",
                  # The feed never shows this one; verify_dates reads it through
                  # load_jobs() to skip URLs the dating service already gave up on.
                  # Losing it only costs that skip (the step re-asks), so it was the
                  # safest column to add here.
                  "posted_confidence",
                  # JD-derived, added last on purpose: the fallback below pops from the
                  # END, so an un-migrated database drops exactly these and keeps
                  # posted_confidence. Put them any earlier and the fallback would give up
                  # a column verify_dates needs before it reached the real problem.
                  #
                  # jd_terms is last of all, and it is the one real payload here (~600 B/row
                  # against ~600 B for every other column combined). It is also the most
                  # degradable: without it the feed still renders, it just falls back to the
                  # baseline match_score instead of scoring against the viewer's own profile.
                  "exp_max_years", "sponsor_jd", "sponsor_reason", "jd_terms")
_FEED_COLS = _FEED_COLS_CORE + "," + ",".join(_FEED_COLS_OPT)


# Narrow column sets for the consumers that DON'T render feed cards. Measured at 19,268 rows:
# the full _FEED_COLS select is 601 B/row (11.6 MB a call), and these run 226-267 B/row
# (~4.4-5.1 MB). Each one is the EXACT set its consumer reads — audited at the call site rather
# than guessed, because a narrowed select turns a column the code forgot it needed into a
# MISSING KEY rather than an empty string, and `r.get("x") or ""` hides that perfectly.
COLS_RECONCILE = "url,is_active,miss_count,last_seen"
"""scraper.reconcile_closed — reads exactly these four (scraper/__init__.py:4715-4730)."""

COLS_DEDUPE = "url,title,company,location"
"""scraper.main's dedupe index when an aggregator source is enabled. It needs the posting
fingerprint (core.posting_key) as well as the url, and existing_urls() returns only urls.

Taken as ONE widened read rather than existing_urls() plus a second call: ~3 MB against ~1.2 MB
at 20k rows, so +1.8 MB per run, once. main() only asks for it when JOBSPY_BOARDS is non-empty —
with the feature dormant it keeps the narrow existing_urls() path and costs nothing."""

COLS_VERIFY = "url,posted_verified,posted_confidence,found_date"
"""scraper.verify_dates._candidates — reads exactly these four (verify_dates.py:132-145)."""

COLS_SCORE = ("url,found_date,location,first_seen,match_score,"
              "loc_state,loc_metro,remote,salary_min,salary_max,salary_period,"
              "exp_max_years,sponsor_jd,sponsor_reason")
"""scraper.score_jobs in new-only mode. The first four are read directly; the last NINE exist
because the same `rows` are passed to _persist_derived(current_rows=...), which diffs against
them to decide what to re-write (score_jobs.py:566-582). Drop those and every run would think
every derived field had changed and re-upsert the whole corpus.

That applies to the three JD-derived columns exactly as it does to the six location/pay ones:
omit them and `have.get(k)` is None on every row forever, so every row carrying any experience
or sponsorship signal diffs as changed on every one of the ~3 runs a day, permanently."""

COLS_COMPANY = "company"
"""Just the employer name, repeated once per posting — 22 B/row, ~0.4 MB for the whole corpus.

The narrowest useful read there is, and it has the widest use: every sponsorship and directory
tool works on the SET of employers we hold, not on the postings. Four of them
(scraper/build_sponsor_counts, scraper/build_visa_tags, scraper/mine_migratemate,
scripts/verify_visa_tags) reached for a bare load_jobs() to build that set, which downloads
every job description — ~130 MB to read one short string per row, a 300x overcharge on a
metered allowance, and the largest single item on the 2026-08-14 egress overage.

Deliberately NOT de-duplicated server-side: PostgREST has no DISTINCT, and every caller wants
the per-company job COUNT as well as the name, so the duplicates are the data."""


_warned_full_jd = False


def _warn_full_jd_read():
    """Say out loud that this call is about to download every stored description.

    THE MOST EXPENSIVE THING THIS CODEBASE CAN DO is also what `load_jobs()` does when called
    with no arguments — ~6.6 KB a row, ~130 MB at 20k rows, against a metered free-tier egress
    allowance. That is the shape a new script naturally reaches for, and four of them
    (mine_migratemate, build_sponsor_counts, build_visa_tags, prune_stale) were pulling the
    whole corpus, descriptions and all, to read the `company` column. Nothing in their output
    said so, so nothing ever prompted anyone to look; the cost only ever surfaced as a billing
    email at the end of the month, by which point it cannot be traced to a command.

    NOT an exception, and NOT a changed default. scraper/score_jobs.py genuinely needs every
    JD — it builds IDF over the whole corpus — and flipping the default would hand it a
    partial corpus, which does not fail, it silently scores jobs against different weights than
    yesterday's run. So a wrong call stays the caller's to fix; this only makes it impossible
    to miss. Once per process, and the count rides a HEAD, which returns no body.
    """
    global _warned_full_jd
    if _warned_full_jd:
        return
    _warned_full_jd = True
    try:
        f = sys._getframe(2)                   # _warn_full_jd_read <- load_jobs <- the caller
        where = "%s:%d" % (os.path.basename(f.f_code.co_filename), f.f_lineno)
    except Exception:
        where = "unknown caller"
    n = table_count(TABLE)
    size = ("~%.0f MB" % (n * 6664 / 1048576.0)) if n else "~130 MB at 20k rows"
    print("  [egress] %s is reading EVERY job description (%s). If it only needs a few\n"
          "           columns, pass cols= (see db.COLS_* / scripts/egress_probe)." % (where, size),
          file=sys.stderr)


def load_jobs(include_jd=True, cols=None):
    """All jobs. The web FEED passes include_jd=False to skip the large `jd` text column — the
    feed never shows it; the detail panel fetches one JD on demand via get_job_jd(). Measured at
    19,268 rows that is the difference between ~128 MB and ~11.6 MB. The scorer's FULL pass keeps
    the default (jd included) because it builds IDF over every description.

    `cols` narrows further for consumers that need only a few fields — see COLS_* above. It is
    best-effort: if that select fails (a column not migrated yet), it falls back to the normal
    include_jd=False path rather than raising, because reading more than necessary is a cost and
    failing here would take down a scrape. That fallback is CAPPED at the include_jd=False
    set whatever the caller passed: a request for six columns must never widen into a read
    of the description text.
    """
    if has_remote_db():
        if cols:
            try:
                return _fetch_all(TABLE, {"select": cols})
            except Exception:
                # NEVER ESCALATE A NARROW READ TO select=*. include_jd defaults to True, so
                # falling through with it untouched turned `load_jobs(cols='url,jd_fp')`
                # into a read of all 25 columns INCLUDING the 263 MB of description text --
                # the exact ~130 MB hazard _warn_full_jd_read exists to catch, reached by a
                # caller that had explicitly asked for six columns. The docstring above has
                # always said this path "falls back to the normal include_jd=False path";
                # the code did not, and it cost ~300 MB of a 5 GB monthly egress budget
                # twice in one afternoon before anyone read the two together.
                #
                # A caller that named columns cannot want every column. Widening to the
                # feed set is a fallback; widening to the whole table is a different bug.
                include_jd = False
        if include_jd:
            _warn_full_jd_read()
        # THE SPLITS ARE RE-JOINED HERE, so every caller keeps receiving one flat dict per
        # job whatever the storage looks like underneath. That is what has let ~120 call
        # sites go untouched across four phases of this: db.py's INTERFACE is the contract,
        # the table layout is not.
        #
        # Each half is gated on its own stamp, so the three states that actually occur --
        # neither moved, one moved, both moved -- are all reachable and all correct. pgrest
        # translates no joins, so the merge is here rather than in the query.
        want_jd = bool(include_jd) and jd_table_ready()
        want_facts = job_facts_ready()
        want_terms = job_terms_ready()
        if want_jd or want_facts or want_terms:
            # `jobs` alone: _FEED_COLS still NAMES the moved columns, and PostgREST answers
            # a select for a column that is not there with a 400 rather than a null. So the
            # moved names are dropped from the select and merged back on below.
            moved = set(JOB_FACTS_COLS) if want_facts else set()
            if want_terms:
                moved.add("jd_terms")
            sel_cols = [c for c in _FEED_COLS.split(",") if c not in moved]
            if include_jd and not want_jd:
                # THE DESCRIPTION IS STILL ON `jobs` AND _FEED_COLS DOES NOT NAME IT.
                # Reachable whenever job_facts or job_terms is stamped before
                # job_descriptions -- which the migrations explicitly permit, being
                # independent of each other. Without this line the merge branch fires,
                # selects _FEED_COLS, and returns rows with NO jd at all: the scorer
                # would then build its IDF over empty strings and analyse nothing,
                # while looking exactly like a corpus that had lost its descriptions.
                # Found by auditing the gate matrix, not by any test.
                sel_cols.append("jd")
            base = _fetch_all(TABLE, {"select": ",".join(sel_cols)})
            if want_facts:
                by_url = _facts_rows()
                for r in base:
                    got = by_url.get(r.get("url")) or {}
                    for c in JOB_FACTS_COLS:
                        r[c] = got.get(c)
            if want_jd:
                texts = _jd_rows()
                for r in base:
                    r["jd"] = texts.get(r.get("url"), "")
            if want_terms:
                packed = _terms_rows()
                for r in base:
                    r["jd_terms"] = packed.get(r.get("url"), "")
            return base
        sel = "*" if include_jd else _FEED_COLS
        try:
            return _fetch_all(TABLE, {"select": sel})
        except Exception:
            # An optional column isn't migrated yet -> retry with only the core set, so the
            # feed keeps working until the ALTERs are run. Drop them one at a time so a
            # partially-migrated database still gets everything it does have. (include_jd=True
            # uses "*", which never names a column, so only this path needs the fallback.)
            if include_jd:
                raise
            opt = list(_FEED_COLS_OPT)
            while opt:
                opt.pop()                      # newest/most-optional first
                sel = _FEED_COLS_CORE + ("," + ",".join(opt) if opt else "")
                try:
                    return _fetch_all(TABLE, {"select": sel})
                except Exception:
                    continue
            raise
    rows = _read_csv()
    actions = _load_actions()
    for r in rows:                       # fold like/hide/applied in for the app
        r["status"] = actions.get(r["url"], r.get("status", ""))
    return rows


# A stored JD never changes. The scheduled pass only fetches descriptions for rows that have
# none, and update_jds() below only writes newly-fetched text — the same immutability the CI
# jd_cache.json.gz relies on. So the one round trip /job makes can be answered from memory on
# a re-read, which is what happens whenever someone opens a job, goes back, and opens it again,
# or when app.js prefetches the page and the click then renders it for real.
#
# Bounded and LRU, sized in ROWS not bytes but with the bytes in mind: descriptions average
# ~5.6 KB, so 64 entries is a few hundred KB against a worker measured at 240 MB warm. The one
# writer (update_jds) evicts what it touches, so "immutable" needs no asterisk.
# Whether jobs.jd_fp exists. Optimistic: a fresh database has it (JOBS_DERIVED_SQL creates
# it), and the one deployment where it does not is the window between shipping this code and
# pasting the migration. Flipped at most once per process, by update_jds below.
_fp_col = {"ok": True}

# ================= the description, in its own table ================================
#
# See MIGRATION_job_descriptions.sql. Short version: Postgres already stores the text out of
# line, so this saves no disk -- what it buys is that `select=*` on `jobs` cannot return 263 MB
# by accident, which it has done twice in one afternoon.
# ================= what we DERIVED about a posting =================================
#
# See MIGRATION_job_facts.sql. The nine columns below are what reading a description told
# us, as opposed to what the employer published (title, company, location) or what we track
# (first_seen, is_active). db.py::FIELDS has grouped them with comments saying exactly this
# since long before there was a table to put them in.
JOB_FACTS_TABLE = "job_facts"

# THE ONE DEFINITION of which columns moved. Every writer, reader, backfill and verify below
# derives its column list from this tuple rather than repeating it -- because the failure
# mode of a column that is in one list and not another is not an error, it is
# _persist_derived diffing against a value it never read, deciding every row changed, and
# re-upserting the whole corpus on every run for ever. See COLS_SCORE's docstring.
JOB_FACTS_COLS = ("loc_state", "loc_metro", "remote",
                  "salary_min", "salary_max", "salary_period",
                  "exp_max_years", "sponsor_jd", "sponsor_reason",
                  "facts_fp")


# The packed keyword analysis, which is 55% of the corpus read on its own. Measured against
# the live box 2026-09-06: the feed's select is 69.8 MB / 45.1 s WITH jd_terms and
# 31.1 MB / 27.2 s without it, over 47,133 rows of which 99.5% carry terms. That 38.7 MB
# also sits RESIDENT in every Passenger worker, on an account capped at ~1.2 GB in total --
# which is the argument for moving it, more than the seconds are.
JOB_TERMS_TABLE = "job_terms"


def _stamp_ready(name, memo):
    """Has `name` been stamped complete in data_versions? Memoised for _JD_SRC_TTL seconds.

    ONE COPY OF THE GATE. There were three -- job_descriptions, job_facts, job_terms -- nine
    identical lines apiece differing only in the dataset name and which dict held the memo, and
    a fourth table would have made it four. The rule this file states everywhere else is that a
    thing has one definition; three copies of a cache-invalidation rule is exactly the shape
    that drifts, because a fix goes into the copy you were looking at.

    UNREADABLE COUNTS AS NOT READY, and that is safe in a way _derived_signature's version read
    is not: falling back means reading `jobs`, which is still the source of truth until the
    contract step drops it. Falling back is free; falling forward is not.
    """
    now = time.time()
    if memo["ready"] is not None and now - memo["at"] < _JD_SRC_TTL:
        return memo["ready"]
    try:
        ready = bool(get_data_version(name))
    except Exception:
        ready = False
    memo.update(ready=ready, at=now)
    return ready


def _side_rows(table, sel, urls, pick=None):
    """{url: value} from a side table -- the whole thing, or just these urls.

    ONE COPY OF THE BATCHED READ, for the same reason as the gate above. `pick` says what the
    value is: a column name for the scalar tables, or None to keep the whole row.
    """
    params = {"select": sel}
    if urls is None:
        rows = _fetch_all(table, params)
    else:
        rows = []
        for batch in _url_batches(list(urls)):
            rows.extend(_fetch_all(table, dict(params, url=_in_list(batch))))
    if pick is None:
        return {r["url"]: r for r in rows if r.get("url")}
    return {r["url"]: (r.get(pick) or "") for r in rows if r.get("url")}



def job_terms_ready():
    """True once backfill_job_terms.py has stamped completion. Same gate as the other two."""
    return _stamp_ready("job_terms", _terms_src)


def mirror_job_terms(rows, keys=None):
    """Copy jd_terms into job_terms as well. Never raises; same bargain as the other mirrors.

    n_terms is stored beside the text so that PRESENCE and THINNESS can be answered without
    reading 38.7 MB. Nothing uses that yet -- web._row_pending still unpacks the string -- and
    it is here rather than in a later migration because the writer is here: a column added
    later would be NULL on every existing row and need its own backfill to become useful.
    """
    if not _terms_tbl["ok"]:
        return
    payload = []
    for r in rows:
        if not r.get("url") or "jd_terms" not in r:
            continue
        packed = r.get("jd_terms") or ""
        row = {"url": r["url"], "jd_terms": packed or None, "n_terms": len(packed)}
        # Only carry the stamp when the caller actually wrote one. Naming it
        # unconditionally would clear a real provenance stamp on any write that
        # touches jd_terms without it -- dedupe_urls carries db.FIELDS on a URL move,
        # and facts_fp is not in FIELDS.
        if keys is None or "facts_fp" in set(keys):
            row["facts_fp"] = r.get("facts_fp")
        payload.append(row)
    if not payload:
        return
    cols = tuple(sorted({k for r in payload for k in r}))
    try:
        for i in range(0, len(payload), 60):      # the text is ~730 B/row; keep bodies small
            _upsert(payload[i:i + 60], keys=cols, table=JOB_TERMS_TABLE, pk="url")
    except Exception as ex:
        if not _table_missing(ex):
            print("  (job_terms mirror failed: %s)" % str(ex)[:120])
            return
        _terms_tbl["ok"] = False
        print("  (public.job_terms not migrated yet - the analysis is in jobs only. "
              "Run MIGRATION_job_terms.sql.)")


def _terms_rows(urls=None):
    """{url: jd_terms} from job_terms -- the whole table, or just these urls."""
    return _side_rows(JOB_TERMS_TABLE, "url,jd_terms", urls, "jd_terms")


def job_facts_ready():
    """True once backfill_job_facts.py has stamped completion. Same gate as jd_table_ready.

    A half-backfilled facts table is worse than none: a missing row reads as a posting with
    no experience floor and no pay, and _filter_rows KEEPS a row it has no number for -- so
    a partial copy would quietly widen every filter instead of narrowing it, which is the
    exact defect this whole revamp started from.
    """
    return _stamp_ready("job_facts", _facts_src)


def mirror_job_facts(rows, keys=None):
    """Copy the derived columns of these rows into job_facts as well. Never raises.

    Called after the authoritative write to `jobs`, for the same reason _mirror_jds is: a
    failure here costs the copy and never the reading. Rows are filtered to JOB_FACTS_COLS,
    so a caller may hand over whatever payload it already built.
    """
    if not _facts_tbl["ok"] or not rows:
        return
    # THE GROUP THE CALLER WROTE, NOT THE WHOLE COLUMN SET, and this is the difference
    # between a mirror and a corruption. _persist_derived writes its derived fields in TWO
    # payloads -- location/pay, then the JD signals -- and naming all ten columns on either
    # one sends the other five as explicit NULLs. Measured before this was fixed: a single
    # location/pay write erased exp_max_years, sponsor_jd, sponsor_reason and facts_fp, so
    # the two groups would have taken turns wiping each other on every scrape. That is the
    # exact failure this whole separation exists to prevent, introduced by the thing meant
    # to prevent it.
    #
    # `keys` is what update_job_fields was handed, so it names the group precisely. Without
    # it, fall back to the union actually present -- the same inference _upsert makes, and
    # for the same reason.
    if keys:
        group = [c for c in JOB_FACTS_COLS if c in set(keys)]
    else:
        present = {k for r in rows for k in r}
        group = [c for c in JOB_FACTS_COLS if c in present]
    if not group:
        return
    payload = [{k: r.get(k) for k in group + ["url"]} for r in rows if r.get("url")]
    if not payload:
        return
    # keys= still names the group, so a column that is None on every row of THIS batch is
    # still sent -- which is how a genuine clear reaches the table. The narrowing above is
    # about which columns the caller touched, not about which values are null.
    cols = tuple(group) + ("url",)
    try:
        for i in range(0, len(payload), 200):
            _upsert(payload[i:i + 200], keys=cols, table=JOB_FACTS_TABLE, pk="url")
    except Exception as ex:
        if not _table_missing(ex):
            print("  (job_facts mirror failed: %s)" % str(ex)[:120])
            return
        _facts_tbl["ok"] = False
        print("  (public.job_facts not migrated yet - derived fields are in jobs only. "
              "Run MIGRATION_job_facts.sql.)")


JD_TABLE = "job_descriptions"

# Is job_descriptions AUTHORITATIVE yet? Not "does the table exist" -- an existing but
# half-backfilled table is the worst possible source, because a missing row is indistinguishable
# from a job with no description and the scraper would queue a fetch for text we already hold.
#
# scripts/backfill_job_descriptions.py stamps data_versions['job_descriptions'] only when every
# row has landed, so this flips exactly once and never early. UNREADABLE COUNTS AS NOT READY,
# which is safe in a way the row-cache key's version read is not: falling back means reading
# jobs.jd, which is still the source of truth until the contract step drops it.
_jd_src = {"ready": None, "at": 0.0}
_JD_SRC_TTL = 300
_facts_src = {"ready": None, "at": 0.0}
_facts_tbl = {"ok": True}
_terms_src = {"ready": None, "at": 0.0}
_terms_tbl = {"ok": True}


def _facts_rows(urls=None):
    """{url: {derived columns}} from job_facts -- the whole table, or just these urls."""
    return _side_rows(JOB_FACTS_TABLE, "url," + ",".join(JOB_FACTS_COLS),
                      urls)


def jd_table_ready():
    """True once the backfill has stamped completion. Cached for _JD_SRC_TTL seconds."""
    return _stamp_ready("job_descriptions", _jd_src)


def _jd_rows(urls=None):
    """{url: jd} from job_descriptions -- the whole table, or just these urls."""
    return _side_rows(JD_TABLE, "url,jd", urls, "jd")



_JD_CACHE_MAX = 64
_jd_cache = collections.OrderedDict()


def get_job_jd(url):
    """The stored job-description text for ONE job, fetched on demand (the feed list omits
    it). Tiny single-row lookup on the url primary key. Returns '' if absent / on error.

    Memoized per url — see _jd_cache. A MISS is cached too: a row with no stored description is
    exactly the row the job page renders most often (the "description pending" state), and
    re-asking the database for a column that is still NULL is the most wasteful version of this
    call. Errors are NOT cached, so a blip does not pin an empty description for the process.
    """
    if not url:
        return ""
    if url in _jd_cache:
        _jd_cache.move_to_end(url)             # a read is a use
        return _jd_cache[url]
    if has_remote_db():
        try:
            # The description lives in its own table once the backfill has stamped it.
            # Until then jobs.jd is the source and that table is a shadow -- see
            # jd_table_ready() for why a half-backfilled table is worse than neither.
            src = JD_TABLE if jd_table_ready() else TABLE
            rows = _fetch_all(src, {"url": "eq.%s" % url, "select": "jd"})
            jd = (rows[0].get("jd") or "") if rows else ""
        except Exception:
            return ""                          # transient: do not remember it
    else:
        jd = ""
        for r in _read_csv():
            if r.get("url") == url:
                jd = r.get("jd", "") or ""
                break
    if len(_jd_cache) >= _JD_CACHE_MAX:
        _jd_cache.popitem(last=False)          # least-recently-USED, not oldest-inserted
    _jd_cache[url] = jd
    return jd


def load_jobs_by_urls(urls, include_jd=True):
    """The stored rows for a SPECIFIC set of job URLs.

    The digest and the scorer each need full rows for a handful of jobs — the ones a run just
    found, or the ones it is about to re-score. Both used to call load_jobs(), which walks the
    whole table: at ~20k rows with ~14.5k stored JDs that is ~70 MB over the wire to keep a few
    hundred rows, once per run, which is most of a free-tier egress budget. This asks for exactly
    the URLs wanted, batched by query-string length the same way delete_urls does (the limit is
    on operand length, not row count — see _DELETE_QS_BUDGET).

    Rows come back in no particular order, and a URL with no stored row is simply absent, so
    callers must keep their own fallback for jobs not in the table yet. A failed batch is skipped
    rather than raised: degrading to "no stored row" costs the caller a baseline score, whereas
    raising takes down a digest or a scoring run.
    """
    urls = [u for u in dict.fromkeys(urls) if u]      # de-dup, preserve order, drop blanks
    if not urls:
        return []
    if has_remote_db():
        # THE SAME THREE SPLITS load_jobs RE-JOINS, and they have to be re-joined the same
        # way here. These two functions are interchangeable from a caller's point of view --
        # notify.py's digest reads card fields through this one and the feed reads them
        # through the other -- so a version that consulted job_facts and one that read the
        # copies still sitting on `jobs` would quietly answer differently. Identical today,
        # because the mirrors keep both in step; not identical the moment Phase 5 drops a
        # column, at which point _FEED_COLS would name something that no longer exists and
        # this function would 400 on every batch.
        want_jd = bool(include_jd) and jd_table_ready()
        want_facts = job_facts_ready()
        want_terms = job_terms_ready()
        moved = set(JOB_FACTS_COLS) if want_facts else set()
        if want_terms:
            moved.add("jd_terms")
        if moved:
            sel = ",".join([c for c in _FEED_COLS.split(",") if c not in moved]
                           + ([] if want_jd or not include_jd else ["jd"]))
        else:
            sel = "*" if include_jd else _FEED_COLS
        rows = []
        for batch in _url_batches(urls):
            try:
                rows.extend(_fetch_all(TABLE, {"select": sel, "url": _in_list(batch)}))
            except Exception:
                # Same un-migrated-column case load_jobs() guards: a select naming an optional
                # column 400s until its ALTER has run ("*" never names one, so it can't hit
                # this). Retry the batch on the core set instead of losing those rows.
                if include_jd:
                    continue
                try:
                    rows.extend(_fetch_all(TABLE, {"select": _FEED_COLS_CORE,
                                                   "url": _in_list(batch)}))
                except Exception:
                    continue
        if want_facts:
            got = _facts_rows([r.get("url") for r in rows if r.get("url")])
            for r in rows:
                f = got.get(r.get("url")) or {}
                for c in JOB_FACTS_COLS:
                    r[c] = f.get(c)
        if want_terms:
            packed = _terms_rows([r.get("url") for r in rows if r.get("url")])
            for r in rows:
                r["jd_terms"] = packed.get(r.get("url"), "")
        if want_jd:
            texts = _jd_rows([r.get("url") for r in rows if r.get("url")])
            for r in rows:
                r["jd"] = texts.get(r.get("url"), "")
        return rows
    want = set(urls)
    rows = [r for r in _read_csv() if r.get("url") in want]
    actions = _load_actions()
    for r in rows:                       # fold like/hide/applied in, as load_jobs does
        r["status"] = actions.get(r["url"], r.get("status", ""))
    return rows


def sample_jobs(n=5, cols=None, **filters):
    """The first `n` rows matching `filters` — for a checker or a probe that wants a REAL row
    rather than the corpus.

    `next(j for j in load_jobs() if ...)` is the natural way to write "give me one job with a
    description", and it is a ~130 MB request that throws away 19,999 rows. Two places did it:
    scripts/test_prefs.py, which runs on a developer's machine several times an hour, and db.py's
    own `python db.py` connectivity check — the command you reach for when your credentials are
    NOT working, so it gets run repeatedly, in exactly the situation where
    nobody is thinking about bandwidth.

    Unlike _fetch_all this does NOT page: `limit` is honoured as written, one request, at most n
    rows. Filters are passed through in PostgREST spelling (`jd="not.is.null"`,
    `company="eq.Acme"`) and `order=url` makes the answer stable, so a test that samples a row
    gets the same row tomorrow.

    Raises on a failed request rather than returning [] — the connectivity check needs the
    exception text to tell you WHICH of url/key/table is wrong, and a caller that would rather
    have nothing can catch it.
    """
    n = max(1, int(n))
    if not has_remote_db():
        # No filter language off-line; "not.is.null" is read as "this column must be non-empty",
        # which is what every caller here means by it.
        want = [k for k, v in filters.items() if "null" in str(v)]
        rows = [r for r in _read_csv() if all((r.get(k) or "").strip() for k in want)]
        return rows[:n]
    p = {"select": cols or _FEED_COLS, "limit": n, "order": "url"}
    p.update({k: v for k, v in filters.items() if v})
    r = _http.get(_rest(TABLE), headers=_headers(), params=p, timeout=30)
    r.raise_for_status()
    rows = r.json()
    return rows if isinstance(rows, list) else []


def urls_missing_jd():
    """Set of job URLs that have NO stored JD — the complement of urls_with_jd(), and much
    cheaper when most rows already have one: measured 2,940 rows against 16,328, so 5.6x fewer
    to page. Prefer this whenever the caller wants the backlog rather than the coverage.

    Matches on NULL *or* empty string: a row can hold '' rather than NULL (the CSV backend and
    some early writes), and `if not jd` treated those as missing — so filtering on NULL alone
    would quietly drop them from the fetch queue forever. Empty set on error, which a caller
    must read as "no backlog known" and not as "everything has a JD".
    """
    if has_remote_db():
        try:
            if jd_table_ready():
                # AN ANTI-JOIN, and it is the one place this split costs something real. A
                # job with no description has no ROW in job_descriptions at all, so there is
                # nothing there to select for -- the backlog is every url in `jobs` minus
                # the ones that have text, and pgrest.py translates no joins.
                #
                # MEASURED 2026-09-06 against the live box: the single-table form answers in
                # 0.3 s because only 284 rows come back, while urls_with_jd() takes 30 s for
                # 46,903. Both halves here are url-only selects, so this is ~60 s and a few
                # MB rather than the 263 MB selecting the text to test it would cost -- but
                # it is a 100x regression on THIS call, paid once per scrape run against a
                # run that already takes 20-45 minutes. Accepted, not overlooked.
                #
                # If it ever bites: a `has_jd` boolean on `jobs`, written by update_jds in
                # the same statement as the text, restores the one-query form. It is a
                # pointer rather than a copy of the text, so it is cheap -- but it is still
                # a second column that can disagree with the first, which is the failure
                # this whole revamp exists to remove. Do not add it on suspicion; add it on
                # a measurement that says the minute matters.
                return {u for u in existing_urls() if u} - urls_with_jd()
            return {r["url"] for r in _fetch_all(TABLE, {"select": "url",
                                                         "or": "(jd.is.null,jd.eq.)"})
                    if r.get("url")}
        except Exception:
            return set()
    return {r["url"] for r in _read_csv() if not (r.get("jd") or "").strip()}


def urls_missing_jd_terms():
    """Set of job URLs with NO stored ANALYSIS — jd_terms NULL or empty.

    The sibling of urls_missing_jd(), and a different question. "Has a description" and "has an
    analysis of that description" came apart on 2026-08-30: 944 active rows held a full
    description (median 5,528 chars) with jd_terms empty, which the feed renders as "JD pending"
    identically to a row holding nothing, because _row_pending keys off THIS column and not off
    the text. Coverage has to be read here, not off the jd column.

    Urls only, same shape and cost as urls_missing_jd() — ~1,000 rows against the ~18 MB the
    jd_terms values themselves would move, which is why this is a filter and not a narrowed
    load_jobs(cols=...).

    NULL *or* empty for the same reason as urls_missing_jd: core.pack_analyzed returns "" rather
    than storing a term-less analysis, so a matched-but-unreadable row can hold '' and
    `if not packed` treats it as absent. Empty set on error, which a caller must read as "no
    backlog known" rather than "everything is analysed".
    """
    if has_remote_db():
        try:
            return {r["url"] for r in _fetch_all(TABLE, {"select": "url",
                                                         "or": "(jd_terms.is.null,jd_terms.eq.)"})
                    if r.get("url")}
        except Exception:
            return set()
    return {r["url"] for r in _read_csv() if not (r.get("jd_terms") or "").strip()}


def urls_with_jd():
    """Set of job URLs that have a stored JD — for 'has a description?' checks without
    pulling the JD text. Cheap (urls only). Empty set on error.

    For the inverse question prefer urls_missing_jd(), which pages ~5.6x fewer rows."""
    if has_remote_db():
        try:
            if jd_table_ready():
                # jd_chars > 0, NOT `jd is not null`. update_jds stores "" for a row whose
                # description was cleared -- close_dead_jds does exactly that -- and a row
                # holding an empty string HAS no description. The jobs.jd branch below says
                # the same thing with an or= group; PostgREST cannot put two filters on one
                # column in one query string, so the length column answers it in one hop
                # rather than selecting the text and testing it here, which is 263 MB.
                return {r["url"] for r in _fetch_all(
                    JD_TABLE, {"select": "url", "jd_chars": "gt.0"}) if r.get("url")}
            return {r["url"] for r in _fetch_all(TABLE, {"select": "url", "jd": "not.is.null"})
                    if r.get("url")}
        except Exception:
            return set()
    return {r["url"] for r in _read_csv() if (r.get("jd") or "").strip()}


def existing_urls():
    if has_remote_db():
        return {row["url"] for row in _fetch_all(TABLE, {"select": "url"}) if row.get("url")}
    return {r["url"] for r in _read_csv()}


def add_jobs(rows):
    """Insert NEW jobs (deduped by url). rows = list of dicts."""
    if not rows:
        return
    if has_remote_db():
        # first_seen is EXCLUDED on purpose, even though it's in FIELDS. _upsert normalizes each
        # chunk to the union of its rows' keys, so a single row carrying it would make every
        # other row in that chunk send an explicit null and merge-duplicates would blank dates
        # we already recorded. The column's DEFAULT + triggers own it; we never send it.
        _upsert([{k: r[k] for k in FIELDS if k != "first_seen" and k in r and r[k] != ""}
                 for r in rows])
        return
    existing = existing_urls()
    # A CSV has no column defaults, so stamp it here. Safe: this branch only appends URLs that
    # aren't already stored, so it can never move an existing job's first_seen.
    today = datetime.date.today().isoformat()
    new = [dict(r, first_seen=(r.get("first_seen") or today))
           for r in rows if r.get("url") not in existing]
    _write_csv(_read_csv() + new)


def update_job_fields(rows, keys=None):
    """Patch specific columns on existing jobs: rows = [{url, location?, found_date?}].
    Used by the extension's detail-fetch to fill in the real location / posting date for
    browser-imported jobs (the listing page often only had a code or nothing).

    `keys` names the column group being written — only needed by a caller that writes one group
    in SEVERAL calls; see _upsert. The CSV fallback below ignores it, because that branch copies
    truthy values only and so cannot clear a column with or without a key list."""
    rows = [r for r in rows if r.get("url")]
    if not rows:
        return
    if has_remote_db():
        _upsert(rows, keys=keys)           # merge-on-url updates only the given columns
        # MIRRORED HERE RATHER THAN AT EACH CALL SITE. Every derived write reaches the
        # database through this function -- _send_derived's two payloads, verify_dates,
        # the extension's location patch, requeue_analysis -- so hooking one place means
        # a future writer is covered without anyone remembering to add a line.
        # mirror_job_facts filters to JOB_FACTS_COLS, so a payload holding none of them
        # (a match_score clear, an is_active close) costs one comprehension and sends
        # nothing.
        mirror_job_facts(rows, keys)
        mirror_job_terms(rows, keys)
        return
    by_url = {r["url"]: r for r in rows}
    out = _read_csv()
    for r in out:
        patch = by_url.get(r.get("url"))
        if patch:
            for k, v in patch.items():
                if k != "url" and v:
                    r[k] = v
    _write_csv(out)


def update_scores(scores):
    """scores = {url: int match_score}."""
    if not scores:
        return
    if has_remote_db():
        _upsert([{"url": u, "match_score": int(s)} for u, s in scores.items()])
        return
    rows = _read_csv()
    for r in rows:
        if r.get("url") in scores:
            r["match_score"] = str(scores[r["url"]])
    _write_csv(rows)


def set_status(url, status):
    """status: 'liked' | 'hidden' | 'applied' | '' to clear."""
    if has_remote_db():
        r = _http.patch(
            _rest(TABLE), headers=_headers({"Prefer": "return=minimal"}),
            params={"url": "eq.%s" % url},
            data=json.dumps({"status": status or None}), timeout=30)
        r.raise_for_status()
        return
    a = _load_actions()
    if status:
        a[url] = status
    else:
        a.pop(url, None)
    _save_actions(a)


def get_statuses():
    """{url: status} for liked/hidden/applied jobs."""
    if has_remote_db():
        return {row["url"]: row["status"]
                for row in _fetch_all(TABLE, {"select": "url,status"}) if row.get("status")}
    return _load_actions()


def _in_list(values):
    """PostgREST `in.(...)` operand. Each value is double-quoted and its own double-quotes and
    backslashes escaped, so a URL containing a comma or a quote can't break out of the list."""
    return "in.(%s)" % ",".join(
        '"%s"' % str(v).replace("\\", "\\\\").replace('"', '\\"') for v in values)


# Max characters of `in.(...)` operand per DELETE. Batching by COUNT doesn't work here: the
# limit is on query-string length, not row count, and our URLs run 58-227 chars. Measured
# against the live project: a ~12KB operand (100 average URLs) succeeds, ~24KB returns 400.
# 8000 keeps a wide margin and still means ~70 rows per request.
_DELETE_QS_BUDGET = 8000


def _url_batches(urls, budget=_DELETE_QS_BUDGET):
    """Split urls into batches whose in.() operand stays under `budget` characters."""
    batch, size = [], 0
    for u in urls:
        cost = len(str(u)) + 8                       # quotes, comma, escaping headroom
        if batch and size + cost > budget:
            yield batch
            batch, size = [], 0
        batch.append(u)
        size += cost
    if batch:
        yield batch


def delete_urls(urls, progress=None, remote_only=False):
    """Remove jobs by url (used when tightening the filter). Works on both backends.

    Batched via PostgREST `url=in.(...)` rather than one request per URL: a 30-day purge
    deletes ~17k rows, and 17k sequential round-trips is both slow and a good way to
    rediscover the WinError 10054 that chunking fixed everywhere else. Rides the same
    retry/backoff from _make_http().

    remote_only=True refuses to fall through to the local CSV. Admin actions pass it: with
    Supabase briefly unreachable the fallback would rewrite an empty/absent jobs.csv, report
    "0 removed", and leave the real rows untouched — and reading that as "there was nothing
    to delete" is exactly how you delete the wrong thing on the retry. The scraper and
    dedupe script keep the old behaviour.
    """
    urls = [u for u in dict.fromkeys(urls) if u]     # de-dup, preserve order, drop blanks
    if not urls:
        return 0
    if remote_only and not has_remote_db():
        raise RuntimeError("delete_urls(remote_only=True) with no Supabase credentials. "
                           "refusing to touch the local-file fallback.")
    if has_remote_db():
        done = 0
        for batch in _url_batches(urls):
            resp = _http.delete(
                _rest(TABLE), headers=_headers({"Prefer": "return=minimal"}),
                params={"url": _in_list(batch)}, timeout=60)
            if resp.status_code >= 400:
                raise RuntimeError("Supabase delete %s (%d urls): %s"
                                   % (resp.status_code, len(batch), resp.text[:200]))
            done += len(batch)
            if progress:
                progress(done, len(urls))
        return done
    drop = set(urls)
    _write_csv([r for r in _read_csv() if r.get("url") not in drop])
    return len(drop)


def delete_all(confirm=""):
    """Wipe the ENTIRE jobs table. Deliberately awkward to call.

    This has no flagged-row protection, no batching, no undo, and — as of this writing — no
    callers anywhere in the repo. It exists for a one-off source-set switch from a shell.

    Three locks, because the cost of reaching it by accident is the whole corpus: a sentinel
    argument that can't be passed by mistake, an environment gate that is never set in
    production, and the rule that web.py never names this symbol at all. The admin panel's
    company delete goes through delete_urls() with an explicit list instead.
    """
    if confirm != "yes-wipe-the-jobs-table":
        raise RuntimeError("delete_all() requires confirm='yes-wipe-the-jobs-table'")
    if os.environ.get("ALLOW_DELETE_ALL") != "1":
        raise RuntimeError("delete_all() requires ALLOW_DELETE_ALL=1 in the environment")
    if has_remote_db():
        resp = _http.delete(_rest(TABLE), headers=_headers({"Prefer": "return=minimal"}),
                               params={"url": "neq.__none__"}, timeout=60)
        resp.raise_for_status()
    else:
        _write_csv([])


def all_flagged_urls():
    """Every job URL any user has liked / applied / hidden — so a prune never deletes a job
    someone is tracking. Empty set on error / local with no file."""
    if has_remote_db():
        try:
            return {r["url"] for r in _fetch_all(USERJOBS_TABLE, {"select": "url"}) if r.get("url")}
        except Exception:
            return set()
    out = set()
    for per_user in _load_json(USER_JOBS_FILE).values():
        out.update(u for u, st in (per_user or {}).items() if st)
    return out


def row_age_date(r):
    """The date a row should be JUDGED BY: the employer's verified date, else the date the
    board reported, else the day it entered the corpus. Same precedence web._row_date uses to
    filter and sort the feed, so 'older than 30 days' means the same thing in the purge as it
    does on screen. The first_seen leg is what makes undated boards (Meta, Workable, BambooHR,
    Rippling) ageable at all — prune_old_jobs used to read found_date alone and could never
    see them."""
    return ((r.get("posted_verified") or "")[:10] or (r.get("found_date") or "")[:10]
            or str(r.get("first_seen") or "")[:10])


# ------------------------------------------------------------------
# THE FRESHNESS POLICY'S SHARED HALF.
#
# Two gates enforce one policy: scraper.MAX_AGE_DAYS refuses stale postings on the way IN, and
# prune_old_jobs deletes stale rows already stored. scraper's own comment is emphatic that if the
# two numbers disagree the corpus drifts to whichever is looser, so the EXEMPTION has to live
# somewhere both can read. That is here, in the lower layer both import.
#
# Getting this one-sided is not a small bug, it is a silent no-op: intake would admit a 44-day-old
# Amazon row and the prune at the end of the very same run would delete it again, for ever, with
# nothing in either log looking wrong.
#
# WHAT EARNS A LONGER WINDOW. Both properties have to hold, or an old date cannot be interpreted:
#   * the source publishes a REAL posting date, not a derived guess, so the age is measurable at
#     all (see core.is_trusted_date for what a derived date looks like)
#   * the source only serves LIVE requisitions, so an old date means "open a long time" rather
#     than "nobody took the listing down"
# amazon.jobs/search.json satisfies both: it stamps an exact posted_date and its search only
# returns open reqs. Amazon also routinely leaves a req open for months, which is why 60% of its
# supply-chain family was being dropped on age alone (measured 2026-08-12: 70 of 116).
#
# Do NOT add a generic ATS host here. Greenhouse, Lever and the rest happily serve a board whose
# owner never closed a filled role, which is exactly what the 30-day default is for.
LONG_LIVED_HOSTS = frozenset({"www.amazon.jobs", "amazon.jobs"})
AGE_LONG_DAYS = int(os.environ.get("MAX_AGE_DAYS_LONG", "90") or 0)


def _url_host(u):
    try:
        from urllib.parse import urlparse
        return (urlparse(u or "").hostname or "").lower()
    except Exception:
        return ""


def is_long_lived(url):
    """Does this posting's source get the longer freshness window?"""
    return _url_host(url) in LONG_LIVED_HOSTS


def stale_urls(days=30, long_days=None):
    """URLs whose row_age_date is older than `days`. Rows with no date of any kind are left
    alone — we can't prove they're stale, so we don't guess.

    Rows from LONG_LIVED_HOSTS are judged against `long_days` instead (default AGE_LONG_DAYS).
    Pass long_days=0 to hold everything to the one window.
    """
    if long_days is None:
        long_days = AGE_LONG_DAYS
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat()
    long_cutoff = ((datetime.date.today() - datetime.timedelta(days=int(long_days))).isoformat()
                   if long_days else cutoff)
    if has_remote_db():
        rows = None
        # first_seen / posted_verified may not exist yet on an old database (see schema.sql);
        # drop the optional columns and retry rather than failing the whole purge.
        for sel in ("url,found_date,posted_verified,first_seen",
                    "url,found_date,posted_verified", "url,found_date"):
            try:
                rows = _fetch_all(TABLE, {"select": sel})
                break
            except Exception:
                continue
        rows = rows or []
    else:
        rows = _read_csv()
    out = []
    for r in rows:
        u, d = r.get("url"), row_age_date(r)
        if u and d and d < (long_cutoff if is_long_lived(u) else cutoff):
            out.append(u)
    return out, cutoff


def prune_old_jobs(days=60, dry_run=False, protect_flagged=True, progress=None, long_days=None):
    """Delete jobs older than `days` (by row_age_date), EXCEPT any a user has flagged — keeps
    the corpus fresh and the DB bounded as the wider net grows it. Returns how many were
    removed, or would be for dry_run. Defensive: never raises (a failed prune must not abort
    the scrape).

    `long_days` is the window for LONG_LIVED_HOSTS and must match the one the intake gate used;
    see the note above stale_urls for why a mismatch is a silent no-op rather than a small bug.
    """
    try:
        old, _cut = stale_urls(days, long_days=long_days)
        to_delete = list(old)
        if protect_flagged:
            flagged = all_flagged_urls()               # protect liked/applied/hidden
            to_delete = [u for u in to_delete if u not in flagged]
        if to_delete and not dry_run:
            delete_urls(to_delete, progress=progress)
        return len(to_delete)
    except Exception as e:
        print("prune_old_jobs skipped:", str(e)[:200])
        return 0


def import_from_files():
    """One-time migration: push local jobs.csv + user_jobs.json into Supabase."""
    if not has_remote_db():
        print("No database credentials found. Set them first (see docs/OPERATIONS.md).")
        return
    rows = _read_csv()
    actions = _load_actions()
    payload = []
    for r in rows:
        item = {k: r.get(k, "") for k in
                ("found_date", "title", "company", "location", "url", "sponsors_h1b")}
        if str(r.get("match_score", "")).isdigit():
            item["match_score"] = int(r["match_score"])
        st = actions.get(r["url"], r.get("status", ""))
        if st:
            item["status"] = st
        payload.append(item)
    for i in range(0, len(payload), 200):     # chunk so requests stay small
        _upsert(payload[i:i + 200])
    print("Imported %d jobs into Supabase table '%s'." % (len(payload), TABLE))


# ================= multi-user: accounts, per-user saved jobs, JD storage =========
USERS_TABLE = "users"
USERJOBS_TABLE = "user_jobs"
USERS_FILE = "users.json"               # local fallback
USER_JOBS_FILE = "user_jobs_local.json"  # local fallback (per-user statuses)
JDS_FILE = "jds.json"                    # local fallback (job description text)


def _now():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _load_json(path):
    if os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _dump_json(path, obj):
    json.dump(obj, open(path, "w", encoding="utf-8"))


# ---- accounts ----
def create_user(username, password_hash, resume=""):
    """Create an account. Returns (ok, message).

    A duplicate username is REPORTED, not raised: PostgREST answers 409/23505 and the admin
    UI needs to say "that name is taken" rather than 500. Note this is deliberately not an
    upsert — merging on conflict would silently overwrite an existing user's password, which
    is the one outcome a "create" must never have.
    """
    if has_remote_db():
        resp = _http.post(
            _rest(USERS_TABLE), headers=_headers({"Prefer": "return=minimal"}),
            data=json.dumps({"username": username, "password_hash": password_hash,
                             "resume": resume}), timeout=30)
        if resp.status_code < 400:
            return (True, "")
        body = resp.text or ""
        if resp.status_code == 409 or "23505" in body:
            return (False, "That username is already taken.")
        return (False, "create_user %s: %s" % (resp.status_code, body[:200]))
    users = _load_json(USERS_FILE)
    if username in users:
        return (False, "That username is already taken.")
    users[username] = {"password_hash": password_hash, "resume": resume, "created_at": _now()}
    _dump_json(USERS_FILE, users)
    return (True, "")


def get_user(username, cols=None):
    """Return {username, password_hash, resume, ...} or None.

    `cols` is a PostgREST select list for callers that need one or two fields. It matters
    because the default `*` drags along BOTH the full résumé text and the `brain_kb` jsonb
    (a user's whole self-training model), and this is called on nearly every request behind
    only a 60 s cache — so the wide select was re-downloading a résumé and a TF-IDF model to
    answer questions like "what is this user's token_epoch?". None keeps the old `*` so
    existing callers are unaffected; the local-file path ignores it and returns everything.
    """
    if has_remote_db():
        r = _http.get(_rest(USERS_TABLE), headers=_headers(),
                         params={"username": "eq.%s" % username,
                                 "select": cols or "*", "limit": 1},
                         timeout=30)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    users = _load_json(USERS_FILE)
    if username in users:
        u = dict(users[username]); u["username"] = username
        return u
    return None


# Widest-first select ladder, same idea as load_jobs' column fallback: ask for the admin
# columns, and drop back to the original pair on a database without the admin columns.
# Rows from the short select simply lack the keys, so every consumer must use .get().
_USER_COLS = ("username,created_at,disabled_at,token_epoch", "username,created_at")


def list_users():
    """Every account. Never includes password_hash — this feeds the admin table and the
    per-request account cache, neither of which has any business holding hashes."""
    if has_remote_db():
        last = None
        for sel in _USER_COLS:
            try:
                r = _http.get(_rest(USERS_TABLE), headers=_headers(),
                              params={"select": sel, "order": "created_at"}, timeout=30)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last = e
        raise last
    users = _load_json(USERS_FILE)
    return [{"username": k, "created_at": v.get("created_at", ""),
             "disabled_at": v.get("disabled_at"), "token_epoch": v.get("token_epoch", 0)}
            for k, v in users.items()]


def _patch_user(username, fields):
    if has_remote_db():
        resp = _http.patch(
            _rest(USERS_TABLE), headers=_headers({"Prefer": "return=minimal"}),
            params={"username": "eq.%s" % username}, data=json.dumps(fields), timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError("update user %s: %s" % (resp.status_code, resp.text[:200]))
        return
    users = _load_json(USERS_FILE)
    if username in users:
        users[username].update(fields)
        _dump_json(USERS_FILE, users)


def set_user_password(username, password_hash):
    _patch_user(username, {"password_hash": password_hash})


def set_user_resume(username, resume):
    _patch_user(username, {"resume": resume})


def set_user_disabled(username, disabled=True):
    """Disable (or re-enable) an account. Requires the admin columns — without the
    column PostgREST 400s and this raises, which the caller surfaces as "run the migration"."""
    _patch_user(username, {"disabled_at":
                           datetime.datetime.now(datetime.timezone.utc).isoformat()
                           if disabled else None})


def bump_token_epoch(username):
    """Invalidate every browser-extension token this user holds, by changing the value folded
    into their token's HMAC. Read-then-write: PostgREST has no atomic increment without an RPC,
    and a lost update BETWEEN TWO RACING BUMPS costs nothing worse than one extra click to
    revoke again.

    A failed READ is a different case and must not be swallowed. This used to default `cur` to 0
    and write 1, so a user sitting at epoch 5 was rolled back to 1 — which silently RE-VALIDATES
    every token signed under epochs 1-4, i.e. exactly the tokens the user just asked to revoke.
    A revoke that quietly un-revokes is worse than one that fails, so this raises instead. Both
    call sites in web.py already catch and surface it ("Couldn't revoke that token"), and
    get_user() raises on a transport error while returning None only when there is genuinely no
    such row — so the two cases stay distinguishable.
    """
    row = get_user(username, "token_epoch")
    if row is None:
        raise RuntimeError("no such user %r: refusing to reset token_epoch to 1" % username)
    cur = int(row.get("token_epoch") or 0)
    _patch_user(username, {"token_epoch": cur + 1})
    return cur + 1


def _user_child_tables():
    """Tables keyed by username, children first. Resolved at CALL time on purpose:
    PROFILES_TABLE, APPLICATIONS_TABLE, RESUMES_TABLE and LEARNED_TABLE are all defined
    further down this file, so binding them at module level here would NameError on import."""
    return (USERJOBS_TABLE, PROFILES_TABLE, APPLICATIONS_TABLE, RESUME_FILES_TABLE,
            RESUMES_TABLE, LEARNED_TABLE)


def delete_user(username, dry_run=False):
    """Delete an account and everything keyed to it. Returns {table: rows_removed}.

    Children are deleted FIRST and `users` LAST: if a child delete fails we stop before
    removing the users row, so a partial failure leaves a live account rather than exactly the
    orphans this function exists to stop creating.

    The child deletes are redundant once the admin migration has added the cascading
    foreign keys — kept anyway so this stays correct before the migration is run, on the
    local-file backend, and so dry_run can report per-table counts for the confirm screen.

    tailored_cache is skipped: put_tailored() writes username='' for anonymous entries, so it
    has no usable per-user filter and is pruned by age instead.
    """
    out = {}
    if has_remote_db():
        for table in _user_child_tables() + (USERS_TABLE,):
            if dry_run:
                out[table] = table_count(table, {"username": "eq.%s" % username}) or 0
                continue
            resp = _http.delete(_rest(table), headers=_headers({"Prefer": "return=representation"}),
                                params={"username": "eq.%s" % username}, timeout=30)
            if resp.status_code >= 400:
                raise RuntimeError("delete_user %s (%s): %s"
                                   % (table, resp.status_code, resp.text[:200]))
            try:
                out[table] = len(resp.json() or [])
            except Exception:
                out[table] = 0
        return out
    users = _load_json(USERS_FILE)
    uj = _load_json(USER_JOBS_FILE)
    out = {USERS_TABLE: 1 if username in users else 0,
           USERJOBS_TABLE: len(uj.get(username) or {})}
    if not dry_run:
        users.pop(username, None); _dump_json(USERS_FILE, users)
        uj.pop(username, None); _dump_json(USER_JOBS_FILE, uj)
    return out


# ---- company blocklist ----
# Deleting a company's jobs does NOT stick on its own: the scrape runs twice a weekday and
# puts them straight back. Both ingestion paths (scraper.main and the extension's bulk import)
# consult this table, so a delete paired with a block is the only combination that holds.
#
# Every function here swallows its errors and returns an empty result. A blocklist read runs
# inside the scraper's hot path and must never be the reason a scrape aborts — worst case it
# reads as "nothing blocked", which is the behaviour before this feature existed.
BLOCKED_TABLE = "blocked_companies"
BLOCKED_FILE = "blocked_companies_local.json"


def block_key(name):
    """Blocklist key for a company name. Reuses normalize_label (defined further down this
    file, so it is resolved at call time): lowercased, punctuation collapsed.

    Suffixes are deliberately NOT stripped. Reducing "Apple Inc" to "apple" would also match
    "Apple Hospitality", a different employer — a blocklist that over-matches silently deletes
    jobs the operator never chose to block.
    """
    return normalize_label(name)


# Legal suffixes, for blocklist matching ONLY -- block_key itself deliberately keeps them.
# Bare 'co' is excluded on purpose: it would reduce 'Home Co' to 'home', and the spelled-out
# 'company' covers the form federal data actually uses.
_BLOCK_SUFFIX_RE = re.compile(
    r"\s+(?:inc|llc|l\s?l\s?c|corp|corporation|ltd|limited|llp|plc|pllc|lp|company)\.?$")


def _block_core(key):
    """A block_key with any trailing legal suffixes removed. 'ulta inc' -> 'ulta'."""
    prev = None
    while key and key != prev:
        prev = key
        key = _BLOCK_SUFFIX_RE.sub("", key).strip()
    return key


def is_blocked(name, blocked):
    """True when `name` is on the blocklist, allowing a legal suffix on EITHER side.

    block_key does not strip suffixes, for a good documented reason: reducing 'Apple Inc' to
    'apple' would also match 'Apple Hospitality'. But taking that literally let the blocklist be
    bypassed by spelling. Measured 2026-08-31: 'ulta' and 'autozone' had BOTH been blocked, with
    the measured reasons still attached -- and a sponsor sweep adopted both anyway, because the
    USCIS spelling is 'ULTA INC' and 'AUTOZONE INC', whose keys are 'ulta inc' and 'autozone
    inc'. Two boards worth ~20,000 postings a rotation walked straight past a blocklist that
    already named them.

    The fix keeps the anti-over-match property: cores are compared EXACTLY, on both sides, never
    as a prefix. So blocked 'apple' matches 'Apple Inc' (core 'apple') and still does NOT match
    'Apple Hospitality' (core 'apple hospitality'), and blocked 'mercy' does not reach 'Mercy
    Corps'. Widening is exactly one legal suffix, which is what the spelling gap is made of.
    """
    if not name or not blocked:
        return False
    key = block_key(name)
    if not key:
        return False
    if key in blocked:
        return True
    core = _block_core(key)
    if not core:
        return False
    return core in blocked or core in {_block_core(k) for k in blocked if k}


def blocked_company_keys():
    """set() of normalized names the ingestion paths must refuse. Empty on any failure."""
    try:
        return {(r.get("name_key") or "") for r in list_blocked() if r.get("name_key")}
    except Exception:
        return set()


def list_blocked():
    """[{name_key, name, reason, added_by, created_at}], newest first. [] if unavailable."""
    if has_remote_db():
        try:
            r = _http.get(_rest(BLOCKED_TABLE), headers=_headers(),
                          params={"select": "*", "order": "created_at.desc"}, timeout=20)
            return r.json() if r.status_code < 400 else []
        except Exception:
            return []
    try:
        return list((_load_json(BLOCKED_FILE) or {}).values())
    except Exception:
        return []


def add_blocked(name, reason="", added_by=""):
    """Block a company. Upserts on name_key, so re-blocking just refreshes the reason."""
    key = block_key(name)
    if not key:
        return False
    rec = {"name_key": key, "name": (name or "").strip()[:200],
           "reason": (reason or "")[:300], "added_by": (added_by or "")[:80]}
    if has_remote_db():
        try:
            resp = _http.post(
                _rest(BLOCKED_TABLE),
                headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                params={"on_conflict": "name_key"}, data=json.dumps(rec), timeout=20)
            return resp.status_code < 400
        except Exception:
            return False
    blob = _load_json(BLOCKED_FILE) or {}
    rec["created_at"] = _now()
    blob[key] = rec
    _dump_json(BLOCKED_FILE, blob)
    return True


def remove_blocked(name_key):
    if has_remote_db():
        try:
            resp = _http.delete(_rest(BLOCKED_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                                params={"name_key": "eq.%s" % name_key}, timeout=20)
            return resp.status_code < 400
        except Exception:
            return False
    blob = _load_json(BLOCKED_FILE) or {}
    blob.pop(name_key, None)
    _dump_json(BLOCKED_FILE, blob)
    return True


# ---- admin audit trail ----
AUDIT_TABLE = "admin_audit"
AUDIT_FILE = "admin_audit_local.json"


def audit_log(actor, action, target="", count=0, detail=None):
    """Record an admin action and return its id (or "" if it couldn't be written).

    Called BEFORE a destructive action begins so the intent survives a process that dies
    mid-batch, then updated with the real count via audit_update. `detail` must stay small —
    a sample, never the full URL list.
    """
    import uuid
    rec = {"id": uuid.uuid4().hex, "actor": (actor or "")[:80], "action": (action or "")[:60],
           "target": (target or "")[:200], "count": int(count or 0),
           "detail": detail if isinstance(detail, dict) else {}}
    if has_remote_db():
        try:
            resp = _http.post(_rest(AUDIT_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                              data=json.dumps(rec), timeout=20)
            return rec["id"] if resp.status_code < 400 else ""
        except Exception:
            return ""
    blob = _load_json(AUDIT_FILE) or {}
    rec["at"] = _now()
    blob[rec["id"]] = rec
    _dump_json(AUDIT_FILE, blob)
    return rec["id"]


def audit_update(audit_id, count, detail=None):
    """Fill in the outcome of an action logged by audit_log. Best-effort."""
    if not audit_id:
        return
    fields = {"count": int(count or 0)}
    if isinstance(detail, dict):
        fields["detail"] = detail
    if has_remote_db():
        try:
            _http.patch(_rest(AUDIT_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                        params={"id": "eq.%s" % audit_id}, data=json.dumps(fields), timeout=20)
        except Exception:
            pass
        return
    blob = _load_json(AUDIT_FILE) or {}
    if audit_id in blob:
        blob[audit_id].update(fields)
        _dump_json(AUDIT_FILE, blob)


def list_audit(limit=25):
    """Most recent admin actions. [] if the table doesn't exist yet."""
    if has_remote_db():
        try:
            r = _http.get(_rest(AUDIT_TABLE), headers=_headers(),
                          params={"select": "*", "order": "at.desc", "limit": limit}, timeout=20)
            return r.json() if r.status_code < 400 else []
        except Exception:
            return []
    try:
        rows = sorted((_load_json(AUDIT_FILE) or {}).values(),
                      key=lambda r: r.get("at") or "", reverse=True)
        return rows[:limit]
    except Exception:
        return []


# ---- per-user liked / hidden / applied ----
def get_user_statuses(username):
    """{url: status} for THIS user's liked/hidden/applied jobs."""
    if has_remote_db():
        return {row["url"]: row["status"]
                for row in _fetch_all(USERJOBS_TABLE,
                                      {"username": "eq.%s" % username, "select": "url,status"})
                if row.get("status")}
    return _load_json(USER_JOBS_FILE).get(username, {})


# The only values this column may hold. A CLOSED SET, and enforced HERE rather than in the two
# routes that call it, so /api/action, /action and the extension all inherit it and a caller
# added later cannot forget. web.py already whitelists `via` for the weaker of the two reasons —
# "a browser writing a value that ends up in an aggregate" — while `status`, which decides
# whether a posting is hidden from your feed forever, was passed through untouched.
USER_STATUSES = ("liked", "hidden", "applied")

# ---------------- the stored per-(user, job) match score ----------------
# See MIGRATION_user_scores.sql for the whole argument. The short version: the score used to
# live only in score_cache/*.json.gz, which is per process and on disk, so a restarted app or a
# fresh worker showed "Not scored" for jobs the database could already answer for.
USER_SCORES_TABLE = "user_scores"
USER_SCORES_FILE = "user_scores_local.json"      # local fallback, same shape as USER_JOBS_FILE
# Every score is stamped with the md5 of the profile it was computed against, and every read
# filters on it. A row scored against an older resume does not come back, so the caller treats it
# as missing and recomputes -- this table can be out of date, but it cannot silently serve a
# number computed against a document the user has since replaced.
# "table not allowed" is dbproxy's own refusal and belongs here with the rest: it means the
# DEPLOYED proxy predates this table, which is the same "not available yet" state as an unrun
# migration and wants the same handling. Reads happen on every feed build for every user, so a
# state that persists until the next deploy must not print on each one.
_MISSING_TABLE = ("does not exist", "PGRST205", "PGRST202", "42P01", "undefined_table",
                  "table not allowed")


def resume_fp(text):
    """The identity of a profile, for scoring purposes. md5 of the exact text scored, which is
    what web.user_scores has always keyed its own caches on -- so the stored rows and the
    in-process ones agree by construction rather than by anyone remembering to."""
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


# The cap update_jds applies before storing a description. Hoisted out of that function
# because jd_fingerprint below MUST hash the same bytes the column actually holds: the
# scoring pass analyses the text it fetched, which can be longer (measured max 8,879 chars),
# and hashing the uncapped copy at one end and the capped one at the other would mark every
# freshly fetched row as stale for ever.
JD_MAX_CHARS = 8000


def jd_fingerprint(text):
    """The identity of a stored DESCRIPTION. None when there is no description at all.

    The same bargain as resume_fp above, pointed at the other half of the problem. A derived
    column -- exp_max_years, jd_terms, the sponsorship verdict -- is a claim ABOUT A SPECIFIC
    TEXT, and a plain column has no way to notice when that text is replaced underneath it.
    On 2026-09-04 a sweep rewrote 1,783 descriptions on rows the table already held; the
    readings stayed, and for two days the experience filter both admitted ten-year jobs to a
    two-year search and hid genuinely entry-level ones from it. Storing the fingerprint of
    the text a reading came from turns that from an audit into a WHERE clause:

        select url from public.jobs where jd_fp is distinct from facts_fp;

    NONE RATHER THAN A HASH OF THE EMPTY STRING, and that is why this is a function rather
    than a one-liner at each call site. A row we hold no text for has no reading to be stale.
    If empty hashed to a value, every never-fetched row would sit permanently on the wrong
    side of that comparison -- a real hash on one side, NULL on the other -- and the query
    that is meant to mean "these rows are lying" would return the fetch backlog instead.
    Absence is not a mismatch.
    """
    if not (text or "").strip():
        return None
    return hashlib.md5((text or "")[:JD_MAX_CHARS].encode("utf-8")).hexdigest()


# A missing COLUMN, which is not the same question as a missing table even though Postgres
# spells both "does not exist". _MISSING_TABLE above matches that phrase, so reusing it here
# would read a missing table as a missing column and drop a field instead of falling back.
_MISSING_COL = ('42703', 'undefined_column', 'does not exist')


def _column_missing(exc, col):
    """Did this write fail because `col` has not been migrated yet, rather than for real?

    Deliberately requires the column NAME in the message as well as the shape, because the
    only safe response to this is to write less data, and doing that on a misread error
    would silently drop a field for ever.
    """
    s = repr(exc)
    return col in s and any(m in s for m in _MISSING_COL)


def _table_missing(exc):
    """Has the migration simply not been run yet? Distinguished from a real failure because the
    two want opposite handling: a missing table means fall back and carry on quietly, anything
    else is worth surfacing."""
    s = repr(exc)
    return any(m in s for m in _MISSING_TABLE)


def get_user_scores(username, fp):
    """{url: score} for this user, computed against the profile whose hash is `fp`.

    Returns {} rather than raising when the table is absent, so the app runs unchanged before
    MIGRATION_user_scores.sql has been applied -- the caller's own fallback (compute it) is
    exactly the behaviour that existed before this table.
    """
    if not username or not fp:
        return {}
    if has_remote_db():
        try:
            rows = _fetch_all(USER_SCORES_TABLE, {
                "select": "url,score",
                "username": "eq.%s" % username,
                "resume_fp": "eq.%s" % fp})
        except Exception as e:
            if not _table_missing(e):
                print("get_user_scores: %r" % (e,))
            return {}
        return {r["url"]: int(r["score"] or 0) for r in rows if r.get("url")}
    store = _load_json(USER_SCORES_FILE).get(username) or {}
    return {u: int(s) for u, s in (store.get(fp) or {}).items()}


def save_user_scores(username, fp, scores, chunk=500, progress=None):
    """Upsert {url: score} for one user. Returns the number of rows written.

    Chunked for the same reason _upsert is: one statement carrying 47,845 rows is a single
    enormous write, and on a shared box the failure mode is a reset connection rather than an
    error you can read.
    """
    if not username or not fp or not scores:
        return 0
    items = [(u, int(s)) for u, s in scores.items() if u]
    if not has_remote_db():
        store = _load_json(USER_SCORES_FILE)
        store.setdefault(username, {})[fp] = {u: s for u, s in items}
        _dump_json(USER_SCORES_FILE, store)
        return len(items)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    done = 0
    for i in range(0, len(items), chunk):
        payload = json.dumps([{"username": username, "url": u, "score": s,
                               "resume_fp": fp, "updated_at": now}
                              for u, s in items[i:i + chunk]])
        last, dropped = "", False
        for attempt in range(3):
            try:
                resp = _http.post(
                    _rest(USER_SCORES_TABLE),
                    headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                    params={"on_conflict": "username,url"}, data=payload, timeout=60)
                if resp.status_code < 400:
                    break
                last = "%s: %s" % (resp.status_code, (resp.text or "")[:200])
                if any(m in last for m in _MISSING_TABLE):
                    raise RuntimeError("user_scores table is missing -- run "
                                       "MIGRATION_user_scores.sql")
                # A JOB THAT VANISHED IS NOT AN ERROR, it is the race this table's foreign key
                # exists to describe: the corpus was read, a prune deleted a posting, and the
                # score for it arrived afterwards. ON DELETE CASCADE means the row could not
                # have survived anyway. PostgREST fails the whole BATCH on one bad row and has
                # no per-row mode, so the batch is dropped rather than retried -- those jobs are
                # gone, and any that are not reappear as gaps on the next run.
                if "23503" in last or "foreign key" in last.lower():
                    print("  (skipped %d score(s) for jobs deleted mid-run)"
                          % len(items[i:i + chunk]))
                    dropped = True
                    break
            except RuntimeError:
                raise
            except Exception as e:
                last = repr(e)[:200]
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError("save_user_scores failed after retries: %s" % last)
        if dropped:
            continue                         # on purpose, and NOT counted as written
        done += len(items[i:i + chunk])
        if progress:
            progress(done, len(items))
    return done


def clear_scores_for_urls(urls, progress=None):
    """Drop every user's score for these jobs -- what a re-analysis means.

    A stored score is derived from the job's jd_terms, so when the scoring pass rewrites that
    column the number underneath it is about an analysis that no longer exists. Deleting is
    right rather than recomputing here: this runs inside the scraper, which has no business
    loading every user's profile, and the reader already treats a missing row as "compute it".
    """
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        return 0
    if not has_remote_db():
        store = _load_json(USER_SCORES_FILE)
        gone = 0
        for _user, by_fp in store.items():
            for _fp, m in by_fp.items():
                for u in urls:
                    gone += 1 if m.pop(u, None) is not None else 0
        _dump_json(USER_SCORES_FILE, store)
        return gone
    done = 0
    for batch in _url_batches(urls):
        try:
            resp = _http.delete(_rest(USER_SCORES_TABLE),
                                headers=_headers({"Prefer": "return=minimal"}),
                                params={"url": _in_list(batch)}, timeout=60)
        except Exception as e:
            if not _table_missing(e):
                print("clear_scores_for_urls: %r" % (e,))
            return done
        if resp.status_code >= 400:
            # COUNTED ONLY WHEN IT HAPPENED. Returning the batch size on a refused DELETE told
            # the caller stale scores had been cleared when every one of them was still there,
            # and the caller's next move is to stop worrying about them.
            if not any(m in (resp.text or "") for m in _MISSING_TABLE):
                print("clear_scores_for_urls %s: %s" % (resp.status_code, resp.text[:200]))
            return done
        done += len(batch)
        if progress:
            progress(done, len(urls))
    return done


def user_scores_count():
    """How many scores are stored, or 0 when the table is not there yet. For an operator
    checking whether the backfill has run, and for the admin panel to report."""
    try:
        # `or 0`: table_count answers None on a failed request rather than raising, so without
        # this the caller gets None where it asked for a count and the except below never runs.
        return table_count(USER_SCORES_TABLE) or 0
    except Exception as e:
        if not _table_missing(e):
            print("user_scores_count: %r" % (e,))
        return 0


def set_user_status(username, url, status):
    """status: 'liked' | 'hidden' | 'applied' | '' to clear — scoped to one user.

    Raises ValueError on anything else. The docstring has always said this; nothing enforced it,
    so any account could insert unbounded rows into the shared user_jobs table carrying arbitrary
    strings, and every reader of that column had to hope.
    """
    status = (status or "").strip()
    if status and status not in USER_STATUSES:
        raise ValueError("status must be one of %s or '' to clear, not %r"
                         % (", ".join(USER_STATUSES), status[:40]))
    if has_remote_db():
        if status:
            resp = _http.post(
                _rest(USERJOBS_TABLE),
                headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                params={"on_conflict": "username,url"},
                # updated_at is written EXPLICITLY, not left to the column default. This is an
                # upsert with merge-duplicates, and a `default now()` only fires on insert — so
                # re-liking an existing row would keep the original timestamp forever. Harmless
                # if the column doesn't exist yet: PostgREST ignores unknown keys? It does NOT,
                # it 400s — so the migration-free path is covered by the retry just below.
                data=json.dumps({"username": username, "url": url, "status": status,
                                 "updated_at": datetime.datetime.now(
                                     datetime.timezone.utc).isoformat()}), timeout=30)
            if resp.status_code >= 400 and "updated_at" in (resp.text or ""):
                # the events table predates that column: drop it and retry, the
                # same shape as load_jobs' column fallback. Losing the timestamp costs recency
                # reporting, not the like itself.
                resp = _http.post(
                    _rest(USERJOBS_TABLE),
                    headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                    params={"on_conflict": "username,url"},
                    data=json.dumps({"username": username, "url": url, "status": status}),
                    timeout=30)
        else:
            resp = _http.delete(
                _rest(USERJOBS_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                params={"username": "eq.%s" % username, "url": "eq.%s" % url}, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError("set_user_status %s: %s" % (resp.status_code, resp.text[:200]))
        return
    uj = _load_json(USER_JOBS_FILE)
    d = uj.setdefault(username, {})
    if status:
        d[url] = status
    else:
        d.pop(url, None)
    _dump_json(USER_JOBS_FILE, uj)


# ---- job description text (shared; lets us score any resume against any job) ----
def update_jds(jds):
    """{url: jd_text} -> persist each job's description (used for per-user scoring).
    JD text is large, so cap each one and write in small CHUNKS — a single bulk POST
    of all of them is multiple MB and gets the connection reset."""
    # THE FINGERPRINT RIDES WITH THE TEXT, in the same row of the same statement, so there
    # is no window in which the column and its stamp disagree. Every writer that can replace
    # a description goes through here -- the sweep's listing JDs, refetch_thin_jds,
    # close_dead_jds, the extension import -- so this is the only place that has to know.
    rows = [{"url": u, "jd": (jd or "")[:JD_MAX_CHARS],
             "jd_fp": jd_fingerprint(jd)} for u, jd in jds.items() if u]
    if not rows:
        return
    for r in rows:
        _jd_cache.pop(r["url"], None)          # the only thing that can falsify _jd_cache
    if has_remote_db():
        # DEGRADES IF THE MIGRATION HAS NOT BEEN RUN. This function is on the scrape's
        # critical path -- it is how every fetched description reaches the table -- and
        # jd_fp is a new column. Deploying the code before pasting
        # MIGRATION_jd_fingerprints.sql would otherwise fail EVERY description write, which
        # is a far worse outcome than not having the provenance stamp for a day. So: try
        # with it, and on a missing-column error drop the stamp, say so once, and carry on.
        # The flag is process-wide, so the cost is one failed chunk per worker, not one per
        # batch. Same self-serve shape as JOBS_DERIVED_SQL, which exists for this exact
        # ordering problem.
        for i in range(0, len(rows), 30):     # ~30 JDs/request keeps the body small
            chunk = rows[i:i + 30]
            if not _fp_col["ok"]:
                chunk = [{k: v for k, v in r.items() if k != "jd_fp"} for r in chunk]
            try:
                _upsert(chunk)
            except Exception as e:
                if not (_fp_col["ok"] and _column_missing(e, "jd_fp")):
                    raise
                _fp_col["ok"] = False
                print("  (jobs.jd_fp not migrated yet — storing descriptions without the "
                      "provenance stamp. Run MIGRATION_jd_fingerprints.sql.)")
                _upsert([{k: v for k, v in r.items() if k != "jd_fp"} for r in chunk])
        _mirror_jds(rows)
        return
    _dump_json(JDS_FILE, jds)


# Whether public.job_descriptions exists at all. Same optimistic shape as _fp_col: a fresh
# database has it, and the one state where it does not is the window between deploying this
# code and pasting MIGRATION_job_descriptions.sql.
_jd_tbl = {"ok": True}


def _mirror_jds(rows):
    """Write the descriptions to job_descriptions as well. Never raises.

    A SHADOW COPY UNTIL THE BACKFILL STAMPS COMPLETION, and the ordering is the safety
    property: jobs.jd is written first and is authoritative, so a failure here loses the
    mirror and never the text. After the flip both are still written, which is what keeps
    them in step until the contract step drops the column.
    """
    if not _jd_tbl["ok"]:
        return
    payload = [{"url": r["url"], "jd": r["jd"], "jd_chars": len(r["jd"] or ""),
                "updated_at": _now()} for r in rows]
    try:
        for i in range(0, len(payload), 30):
            _upsert(payload[i:i + 30], table=JD_TABLE, pk="url")
    except Exception as e:
        if not _table_missing(e):
            # A real failure is worth surfacing, but not worth failing the scrape over: the
            # text is already safely in jobs.jd and the backfill re-syncs whatever drifted.
            print("  (job_descriptions mirror failed: %s)" % str(e)[:120])
            return
        _jd_tbl["ok"] = False
        print("  (public.job_descriptions not migrated yet — descriptions are in jobs.jd "
              "only. Run MIGRATION_job_descriptions.sql.)")


def requeue_analysis(urls):
    """Mark these rows' stored ANALYSIS as no longer trustworthy, by clearing match_score.

    A description is immutable in the ordinary path -- score_jobs fetches one only when the
    column is empty -- so the derived columns (jd_terms, exp_max_years, sponsor_jd) are normally
    a true reading of the text the row holds. The repair paths break that on purpose:
    refetch_thin_jds replaces a loading shell with the real posting, close_dead_jds writes back
    a recovered one or blanks a junk one, and the extension fills in a description a browser
    import arrived without. After any of those the row still carries the OLD text's analysis and
    nothing notices -- score_jobs queues a fetch on "jd is empty", and this row's is not.

    MEASURED CONSEQUENCE, 2026-09-06: nine postings in the owner's "0 to 2 Years" feed asked for
    3 to 10 years in their own descriptions. exp_max_years was NULL on all nine because it was
    derived from a shorter earlier copy that stated no number, and _filter_rows keeps a row it
    holds no number for. The job page read the CURRENT text and printed the requirement under a
    card the filter had just admitted.

    match_score is the re-queue signal because score_jobs already reads NULL there as "no run
    has ever scored this row" (_new_only_targets' `unscored` set), so the next scrape re-analyses
    it and _persist_derived rewrites every JD-derived column from the text now stored. Nothing
    else is cleared: blanking jd_terms would make the feed say "JD pending" for a row holding a
    perfectly good description, whereas a NULL match_score is invisible to the feed -- it ranks
    on the per-user numbers in user_scores, not on this column.

    The CSV fallback cannot express a clear at all (update_job_fields' local branch copies
    truthy values only, as its own docstring says), so this is a no-op off a real database.
    """
    rows = [{"url": u} for u in dict.fromkeys(urls) if u]
    if not rows or not has_remote_db():
        return 0
    # keys= is REQUIRED here, not tidiness. _upsert drops None values when it merges duplicate
    # urls and then infers the key union from what survives, so a column that is None on every
    # row of a batch is not sent at all and the stale value lives on. Naming the group forces
    # match_score onto every row -- the same trap the comment in _upsert describes.
    update_job_fields(rows, keys=("url", "match_score"))
    return len(rows)


# ================= employers, and the version stamp for the caches that read them ====
#
# See MIGRATION_companies.sql for why this is a table. Short version: sponsor_counts.json and
# visa_tags.json cost 23 MB of RAM per worker to answer questions about 3,395 employers, 98.5% of
# their rows are never asked about, and /reload cannot clear the module globals that hold them.
COMPANIES_TABLE = "companies"
COMPANIES_FILE = "companies_table_local.json"     # local fallback, same shape
VERSIONS_TABLE = "data_versions"
VERSIONS_FILE = "data_versions_local.json"
_versions = {"map": None, "at": 0.0}
_VERSIONS_TTL = 300          # seconds; bounds how long a worker misses a rebuild


def load_companies():
    """{name_key: row} for every employer we hold facts about, or {} if not migrated yet.

    READ WHOLE, deliberately. This is a few thousand narrow rows -- measured 3,395 employers
    across the live corpus -- against the 129,660-key file it replaces, so paging it by the
    companies actually on screen would cost more round trips than it saves bytes.

    {} ON ANY FAILURE IS SAFE HERE AND IS NOT SAFE IN _derived_signature -- see get_data_version
    below for the difference. A caller that gets {} falls back to the JSON files and renders the
    same cards; a KEY that gets {} silently agrees with a key computed from real data, and two
    workers then fight over one cache file.
    """
    if has_remote_db():
        try:
            # ORDER EXPLICITLY. _fetch_all pages with `order=url` by default, and this
            # table is keyed on name_key -- so the default asks for a column that does
            # not exist here. Postgres answers `column "url" does not exist`, which
            # contains the substring _table_missing() looks for, so the except below
            # would read a REAL error as "not migrated yet" and return {} for ever.
            # The whole employer-facts win would silently never switch on, and the
            # symptom would be "the migration ran and nothing changed".
            rows = _fetch_all(COMPANIES_TABLE, {"select": "*", "order": "name_key"})
        except Exception as e:
            if not _table_missing(e):
                raise
            return {}
        return {r["name_key"]: r for r in rows if r.get("name_key")}
    return _load_json(COMPANIES_FILE) or {}


def save_companies(rows):
    """Upsert employer rows. rows = [{name_key, display_name, ...}]."""
    rows = [r for r in rows if r.get("name_key")]
    if not rows:
        return 0
    if has_remote_db():
        # keys= names the group so a column that is None on every row of a chunk is still sent.
        # Without it a batch in which nobody has a logo would leave stale logos in place -- the
        # same trap db._upsert's own comment describes, and the one requeue_analysis was built
        # around.
        cols = sorted({k for r in rows for k in r})
        for i in range(0, len(rows), 200):
            _upsert(rows[i:i + 200], keys=cols, table=COMPANIES_TABLE, pk="name_key")
        return len(rows)
    _dump_json(COMPANIES_FILE, {r["name_key"]: r for r in rows})
    return len(rows)


def _versions_map():
    """{name: version} for every generated dataset. Memoised; RAISES on a real read failure.

    ONE QUERY SERVES EVERY CONSUMER, and that is not premature tidiness. Each reader that wants a
    version -- web.companies_table, db.jd_table_ready, and whatever Phase 3 adds -- would
    otherwise probe separately, and get_job_jd is on the /job page's critical path against a box
    measured at 271 ms round trip. One extra hop per consumer per worker is a real page.

    The table is four rows. Reading it whole costs the same as reading one.
    """
    now = time.time()
    if _versions["map"] is not None and now - _versions["at"] < _VERSIONS_TTL:
        return _versions["map"]
    try:
        # Ordered on this table's own key, for the reason load_companies spells out:
        # the default `order=url` would 400 here, and that 400 says "does not exist",
        # which _table_missing cannot tell from an un-run migration. Every gate would
        # then read not-ready permanently -- job_descriptions, job_facts, job_terms.
        rows = _fetch_all(VERSIONS_TABLE, {"select": "name,version", "order": "name"})
    except Exception as e:
        if not _table_missing(e):
            raise                      # see get_data_version: a caller may be keying a cache
        rows = []                      # un-migrated is a stable answer, not a failure
    m = {r.get("name"): (r.get("version") or "") for r in rows if r.get("name")}
    _versions.update(map=m, at=now)
    return m


def get_data_version(name):
    """The current version stamp for a generated dataset. RAISES if it cannot be read.

    THE RAISE IS THE POINT, and it is the opposite of load_companies' behaviour. This value goes
    into web._derived_signature, which is half the row_cache key, and that function's docstring
    states the rule: nothing that can fail silently may be in the key. It has already cost one
    production incident -- two KV maps whose readers swallowed a failure into {}, so workers
    computed different keys, each rebuilt 7 s over the other's file, and production showed 63 ms
    and 8,401 ms in the same second.

    An ABSENT row is a different thing from a failed read and returns "" quite happily: it means
    no builder has ever stamped this dataset, which is a real and stable answer.
    """
    if not has_remote_db():
        return (_load_json(VERSIONS_FILE) or {}).get(name, "")
    return _versions_map().get(name, "")


def set_data_version(name, version):
    """Stamp a dataset. Builders call this after writing; readers key caches on it."""
    if has_remote_db():
        _upsert([{"name": name, "version": version, "updated_at": _now()}],
                keys=("name", "version", "updated_at"), table=VERSIONS_TABLE, pk="name")
        # The writer is usually a build script rather than the app, but a stale memo in the
        # process that just bumped the version is the one case guaranteed to be wrong.
        _versions.update(map=None, at=0.0)
        return
    d = _load_json(VERSIONS_FILE) or {}
    d[name] = version
    _dump_json(VERSIONS_FILE, d)



# ================= custom job boards (added through the app's "Add board" view) ====
BOARDS_TABLE = "boards"
BOARDS_FILE = "boards.json"          # local fallback


def list_boards():
    """[{url, ats_type, company, added_by, created_at}] of user-added boards.
    Defensive: a missing table / failed request returns [] so the scrape never breaks."""
    if has_remote_db():
        try:
            r = _http.get(_rest(BOARDS_TABLE), headers=_headers(),
                             params={"select": "*", "order": "created_at"}, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception:
            return []
    rows = _load_json(BOARDS_FILE)
    return rows if isinstance(rows, list) else []


def add_board(url, ats_type, company, added_by=""):
    """Insert/replace a custom board (PK = url). Returns (ok, error_message)."""
    rec = {"url": url, "ats_type": ats_type, "company": company,
           "added_by": added_by, "created_at": _now()}
    if has_remote_db():
        resp = _http.post(
            _rest(BOARDS_TABLE),
            headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            params={"on_conflict": "url"}, data=json.dumps(rec), timeout=30)
        if resp.status_code >= 400:
            return False, "Supabase add_board %s: %s" % (resp.status_code, resp.text[:300])
        return True, ""
    rows = [b for b in list_boards() if b.get("url") != url] + [rec]
    _dump_json(BOARDS_FILE, rows)
    return True, ""


def delete_board(url):
    if has_remote_db():
        resp = _http.delete(_rest(BOARDS_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                               params={"url": "eq.%s" % url}, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError("delete_board %s: %s" % (resp.status_code, resp.text[:200]))
        return
    rows = [b for b in list_boards() if b.get("url") != url]
    _dump_json(BOARDS_FILE, rows)


# ================= JobSpy / aggregator findings (SIDECAR — never the feed) =================
# A DISCOVERY LEDGER, not a job source. Rows here are postings the daily sweep saw on
# LinkedIn/Indeed/jobright. They exist to answer "which employers are hiring that we do not
# scrape yet", and to keep the evidence behind a board being adopted.
#
# NOTHING IN THE FEED PATH READS THIS TABLE, and the writer touches no other table. That is the
# whole point of it being separate. CLAUDE.md's "don't reach for a job aggregator" stands:
# Adzuna was 6% of the feed and 38% of every job with no usable description, and writing these
# rows into `jobs` would repeat exactly that. Nothing here is scored, deduped against the corpus,
# or rendered. `jobs`, `boards` and the company tables are never written by this module.
#
# PK is the posting url, so re-running a day upserts instead of piling up duplicates.
FINDINGS_TABLE = "jobspy_findings"
FINDINGS_FILE = "jobspy_findings_local.json"     # local fallback, same pattern as BOARDS_FILE
FINDINGS_FIELDS = ("url", "title", "company", "posted_date", "location", "source",
                   "career_page", "board_url", "ats_type", "seniority", "salary",
                   "h1b_filings", "visa_routes", "company_is_new", "run_date", "created_at")

# Surfaced the same self-serve way as APPLICATIONS_SQL: all `if not exists`, safe to re-run.
FINDINGS_SQL = """-- Sidecar ledger for the daily aggregator sweep. NOT read by the feed.
create table if not exists public.jobspy_findings (
  url text primary key,
  title text, company text,
  posted_date date,
  location text, source text,
  career_page text, board_url text, ats_type text,
  seniority text, salary text,
  h1b_filings integer, visa_routes text,
  company_is_new boolean,
  run_date date,
  created_at timestamptz default now());
create index if not exists jobspy_findings_company_idx on public.jobspy_findings (company);
create index if not exists jobspy_findings_run_idx on public.jobspy_findings (run_date);
create index if not exists jobspy_findings_new_idx on public.jobspy_findings (company_is_new);
"""


def list_findings(limit=0):
    """Rows from the findings ledger, newest run first. Defensive: never raises."""
    if has_remote_db():
        try:
            params = {"select": "*", "order": "run_date.desc"}
            if limit:
                params["limit"] = str(int(limit))
            r = _http.get(_rest(FINDINGS_TABLE), headers=_headers(), params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception:
            return []
    rows = _load_json(FINDINGS_FILE)
    rows = rows if isinstance(rows, list) else []
    return rows[:limit] if limit else rows


def add_findings(rows):
    """Upsert findings on url. Returns (written, error_message).

    Batched: a sweep produces hundreds of rows and one request each would spend the run's whole
    time budget on round trips. Returns the first error rather than raising, so a failed write
    still leaves the sweep free to emit its report artifact -- the report is the deliverable the
    user actually reads, and losing it to a database hiccup would be the worse outcome.
    """
    recs = []
    for r in rows or []:
        if not (r or {}).get("url"):
            continue
        rec = {k: r.get(k) for k in FINDINGS_FIELDS}
        rec["created_at"] = rec.get("created_at") or _now()
        recs.append(rec)
    if not recs:
        return 0, ""
    if has_remote_db():
        written = 0
        for i in range(0, len(recs), 500):
            chunk = recs[i:i + 500]
            resp = _http.post(
                _rest(FINDINGS_TABLE),
                headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                params={"on_conflict": "url"}, data=json.dumps(chunk), timeout=60)
            if resp.status_code >= 400:
                return written, "add_findings %s: %s" % (resp.status_code, resp.text[:300])
            written += len(chunk)
        return written, ""
    have = {r.get("url"): r for r in list_findings()}
    for rec in recs:
        have[rec["url"]] = rec
    _dump_json(FINDINGS_FILE, list(have.values()))
    return len(recs), ""


# ================= per-user application tracker =================
# Each user's applications are scoped by `username` (private to them), exactly like
# user_jobs. Tracks apps from THIS app AND ones the user made on any other platform.
APPLICATIONS_TABLE = "applications"
APPLICATIONS_FILE = "applications_local.json"   # local fallback {username: [recs]}
APP_FIELDS = ("id", "username", "company", "title", "url", "status", "applied_date",
              "resume_name", "resume_used", "notes", "created_at")
APPLICATIONS_SQL = (
    "create table if not exists public.applications (\n"
    "  id text primary key,\n"
    "  username text not null,\n"
    "  company text, title text, url text,\n"
    "  status text, applied_date date,\n"
    "  resume_name text, resume_used text, notes text,\n"
    "  created_at timestamptz default now());\n"
    "alter table public.applications add column if not exists resume_name text;\n"
    "create index if not exists applications_user_idx on public.applications (username);\n\n"
    "create table if not exists public.resumes (\n"
    "  id text primary key, username text not null,\n"
    "  name text, content text, created_at timestamptz default now());\n"
    "create index if not exists resumes_user_idx on public.resumes (username);\n\n"
    "create table if not exists public.profiles (\n"
    "  username text primary key,\n"
    "  name text, email text, phone text, location text, linkedin text,\n"
    "  work_authorized text, needs_sponsorship text, default_resume text, notes text,\n"
    "  updated_at timestamptz default now());\n"
    "alter table public.profiles add column if not exists default_resume text;\n"
    # --- application-autofill profile columns ---
    "alter table public.profiles add column if not exists first_name text;\n"
    "alter table public.profiles add column if not exists last_name text;\n"
    "alter table public.profiles add column if not exists pronouns text;\n"
    "alter table public.profiles add column if not exists address_line1 text;\n"
    "alter table public.profiles add column if not exists address_line2 text;\n"
    "alter table public.profiles add column if not exists city text;\n"
    "alter table public.profiles add column if not exists state text;\n"
    "alter table public.profiles add column if not exists postal_code text;\n"
    "alter table public.profiles add column if not exists country text;\n"
    "alter table public.profiles add column if not exists github text;\n"
    "alter table public.profiles add column if not exists portfolio text;\n"
    "alter table public.profiles add column if not exists website text;\n"
    "alter table public.profiles add column if not exists work_auth_status text;\n"
    "alter table public.profiles add column if not exists requires_sponsorship_now text;\n"
    "alter table public.profiles add column if not exists requires_sponsorship_future text;\n"
    "alter table public.profiles add column if not exists gender text;\n"
    "alter table public.profiles add column if not exists race_ethnicity text;\n"
    "alter table public.profiles add column if not exists hispanic_latino text;\n"
    "alter table public.profiles add column if not exists veteran_status text;\n"
    "alter table public.profiles add column if not exists disability_status text;\n"
    "alter table public.profiles add column if not exists desired_salary text;\n"
    "alter table public.profiles add column if not exists salary_currency text;\n"
    "alter table public.profiles add column if not exists available_start_date text;\n"
    "alter table public.profiles add column if not exists willing_to_relocate text;\n"
    "alter table public.profiles add column if not exists how_did_you_hear text;\n"
    # --- work-authorization timeline (core.visa_timeline) ---
    "alter table public.profiles add column if not exists program_end_date text;\n"
    "alter table public.profiles add column if not exists opt_type text;\n"
    "alter table public.profiles add column if not exists opt_start_date text;\n"
    "alter table public.profiles add column if not exists opt_end_date text;\n"
    "alter table public.profiles add column if not exists stem_eligible text;\n"
    "alter table public.profiles add column if not exists unemployment_days_used text;\n"
    "-- saved feed filters + email-digest opt-in (core.normalize_prefs)\n"
    "alter table public.profiles add column if not exists search_prefs jsonb default '{}'::jsonb;\n"
    "alter table public.profiles add column if not exists extra jsonb default '{}'::jsonb;\n"
    "alter table public.profiles add column if not exists application_defaults jsonb default '{}'::jsonb;\n\n"
    # --- tailored-résumé cache ---
    "create table if not exists public.tailored_cache (\n"
    "  id text primary key, username text, data jsonb, created_at timestamptz default now());\n"
    "create index if not exists tailored_cache_user_idx on public.tailored_cache (username);\n\n"
    # --- learned answers (the extension's 'training' bank: how the user answers each question) ---
    "create table if not exists public.learned_answers (\n"
    "  username text not null, key text not null,\n"
    "  label text, value text, type text, options jsonb, company text,\n"
    "  count int default 1, updated_at timestamptz default now(),\n"
    "  primary key (username, key));\n"
    "create index if not exists learned_answers_user_idx on public.learned_answers (username);")


def list_applications(username):
    """This user's applications, newest first. Defensive: missing table / error -> []."""
    if has_remote_db():
        try:
            r = _http.get(_rest(APPLICATIONS_TABLE), headers=_headers(),
                             params={"username": "eq.%s" % username, "select": "*",
                                     "order": "created_at.desc"}, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception:
            return []
    data = _load_json(APPLICATIONS_FILE)
    recs = data.get(username, []) if isinstance(data, dict) else []
    return sorted(recs, key=lambda x: x.get("created_at", ""), reverse=True)


def save_application(username, rec):
    """Insert or update one application (PK=id; id/created_at auto-filled). Columns not
    provided are left unchanged on update. Returns (ok, id_or_error)."""
    import uuid
    rec = dict(rec)
    rec["username"] = username
    if not rec.get("id"):
        rec["id"] = uuid.uuid4().hex
    if not rec.get("created_at"):
        rec["created_at"] = _now()
    payload = {k: rec.get(k) for k in APP_FIELDS if k in rec}
    if not payload.get("applied_date"):
        payload["applied_date"] = None              # empty string isn't a valid SQL date
    if has_remote_db():
        resp = _http.post(
            _rest(APPLICATIONS_TABLE),
            headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            params={"on_conflict": "id"}, data=json.dumps(payload), timeout=30)
        if resp.status_code >= 400:
            return False, "Supabase save_application %s: %s" % (resp.status_code, resp.text[:300])
        return True, rec["id"]
    data = _load_json(APPLICATIONS_FILE)
    if not isinstance(data, dict):
        data = {}
    lst = data.setdefault(username, [])
    for i, a in enumerate(lst):
        if a.get("id") == rec["id"]:
            lst[i] = {**a, **payload}
            break
    else:
        lst.append(payload)
    _dump_json(APPLICATIONS_FILE, data)
    return True, rec["id"]


def find_application_by_url(username, url):
    """This user's application for a given apply-url, or None (de-dupes feed auto-log)."""
    if not url:
        return None
    return next((a for a in list_applications(username) if a.get("url") == url), None)


def delete_application(username, app_id):
    if has_remote_db():
        resp = _http.delete(_rest(APPLICATIONS_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                               params={"id": "eq.%s" % app_id, "username": "eq.%s" % username}, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError("delete_application %s: %s" % (resp.status_code, resp.text[:200]))
        return
    data = _load_json(APPLICATIONS_FILE)
    if isinstance(data, dict) and username in data:
        data[username] = [a for a in data[username] if a.get("id") != app_id]
        _dump_json(APPLICATIONS_FILE, data)


# ---- saved résumé versions (so you can record WHICH résumé you used per application) ----
RESUMES_TABLE = "resumes"
RESUMES_FILE = "resumes_local.json"     # local fallback {username: [recs]}
# `active` marks the ONE row that users.resume mirrors. It has to be listed here: save_resume
# filters every payload to these keys and drops the rest without a word.
RESUME_FIELDS = ("id", "username", "name", "content", "created_at", "active")


def list_resumes(username):
    """This user's saved résumé versions. Defensive: missing table / error -> []."""
    if has_remote_db():
        try:
            r = _http.get(_rest(RESUMES_TABLE), headers=_headers(),
                             params={"username": "eq.%s" % username, "select": "*",
                                     "order": "created_at"}, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception:
            return []
    data = _load_json(RESUMES_FILE)
    return data.get(username, []) if isinstance(data, dict) else []


def save_resume(username, rec):
    """Insert/update a named résumé version (PK=id). Returns (ok, id_or_error)."""
    import uuid
    rec = dict(rec)
    rec["username"] = username
    if not rec.get("id"):
        rec["id"] = uuid.uuid4().hex
    if not rec.get("created_at"):
        rec["created_at"] = _now()
    payload = {k: rec.get(k) for k in RESUME_FIELDS if k in rec}
    if has_remote_db():
        resp = _http.post(
            _rest(RESUMES_TABLE),
            headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            params={"on_conflict": "id"}, data=json.dumps(payload), timeout=30)
        if resp.status_code >= 400:
            return False, "Supabase save_resume %s: %s" % (resp.status_code, resp.text[:300])
        return True, rec["id"]
    data = _load_json(RESUMES_FILE)
    if not isinstance(data, dict):
        data = {}
    lst = data.setdefault(username, [])
    for i, a in enumerate(lst):
        if a.get("id") == rec["id"]:
            lst[i] = {**a, **payload}
            break
    else:
        lst.append(payload)
    _dump_json(RESUMES_FILE, data)
    return True, rec["id"]


def delete_resume(username, rid):
    if has_remote_db():
        resp = _http.delete(_rest(RESUMES_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                               params={"id": "eq.%s" % rid, "username": "eq.%s" % username}, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError("delete_resume %s: %s" % (resp.status_code, resp.text[:200]))
        return
    data = _load_json(RESUMES_FILE)
    if isinstance(data, dict) and username in data:
        data[username] = [a for a in data[username] if a.get("id") != rid]
        _dump_json(RESUMES_FILE, data)


# ---- the active resume ----
# The library row is the truth; users.resume is a CACHE of whichever row is active, kept only
# because current_resume(), the feed's match %, core.score_against and db.profile_text all read
# it. Writing both here is what stops the two stores drifting apart again.


def get_active_resume(username):
    """The user's live resume row, or None. Falls back to the newest row when nothing is flagged,
    so a library that predates the `active` column still answers instead of returning nothing."""
    rows = list_resumes(username) or []
    if not rows:
        return None
    return next((r for r in rows if r.get("active")), rows[-1])


def set_active_resume(username, rid):
    """Flag one row active and mirror its text into users.resume. Returns the row, or None.

    Clear-then-set across two writes rather than one statement, and deliberately NOT guarded by a
    unique constraint: the window where a user momentarily has zero active rows is harmless (the
    reader falls back to newest), whereas a constraint would make the clear half fail outright.
    """
    rows = list_resumes(username) or []
    target = next((r for r in rows if r.get("id") == rid), None)
    if target is None:
        return None

    def _flag(row, on):
        # created_at is threaded back deliberately. save_resume stamps it whenever it is absent,
        # and this is a PARTIAL update, so omitting it would quietly reset the résumé's creation
        # date every time the user switched which one was active — and created_at is what
        # get_active_resume's newest-row fallback and the library ordering both sort on.
        save_resume(username, {"id": row["id"], "active": on,
                               "created_at": row.get("created_at")})

    for r in rows:
        if r.get("active") and r.get("id") != rid:
            _flag(r, False)
    _flag(target, True)
    target = dict(target, active=True)
    # The mirror. If this half fails the library is still correct, so it must not take the
    # activation down with it -- the next save repairs it.
    try:
        set_user_resume(username, target.get("content") or "")
    except Exception:
        pass
    return target


# ---- uploaded resume FILES (pdf / docx / tex) ----
# A sibling table on purpose. list_resumes() selects *, brain.get_resume() re-lists every row to
# fetch one, and profile_text() walks all rows behind a 60s cache -- a blob column on `resumes`
# would ride along on all of it. Here the bytes are only read when something asks by id.
#
# `b64` is base64 in a TEXT column, never bytea: pgrest.jsonify() runs
# bytes(v).decode("utf-8", "replace") over every value it returns, so a bytea column comes back
# from the direct-Postgres transport full of U+FFFD with no error raised. Same encoding the
# existing blob store already uses for tailored PDFs.
RESUME_FILES_TABLE = "resume_files"
RESUME_FILES_FILE = "resume_files_local.json"   # local fallback {username: [recs]}
RESUME_FILE_FIELDS = ("id", "resume_id", "username", "kind", "filename", "mime",
                      "b64", "size", "created_at")
# Everything except the payload. Every listing uses this; only get_resume_file() asks for b64.
RESUME_FILE_META = tuple(f for f in RESUME_FILE_FIELDS if f != "b64")
# Mirrors core.RESUME_UPLOAD_EXTS. If a format can be uploaded it can be stored, or the
# Original File tab is silently empty for it.
RESUME_FILE_KINDS = ("pdf", "docx", "tex", "txt", "md")
RESUME_FILES_TTL_DAYS = 365

RESUME_FILES_SQL = """-- Resume Brain review panel: active-resume flag, the two Brain objects that
-- were never created, and durable storage for uploaded resume files.
-- Safe to re-run. Full version with the reasoning: MIGRATION_resume_files.sql
alter table public.resumes add column if not exists active boolean default false;
create index if not exists resumes_active_idx on public.resumes using btree (username) where active;

alter table public.users add column if not exists brain_kb jsonb;
create table if not exists public.brain_companies (
  domain text primary key, data jsonb,
  fetched_at timestamp with time zone default now());

create table if not exists public.resume_files (
  id text not null, resume_id text, username text not null,
  kind text not null, filename text, mime text, b64 text, size integer,
  created_at timestamp with time zone default now(),
  constraint resume_files_pkey PRIMARY KEY (id));
create index if not exists resume_files_resume_idx on public.resume_files using btree (resume_id);
create index if not exists resume_files_created_idx on public.resume_files using btree (created_at);

do $$ begin if not exists (select 1 from pg_constraint where conname = 'resume_files_username_fkey') then alter table public.resume_files add constraint resume_files_username_fkey FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE; end if; end $$;
do $$ begin if not exists (select 1 from pg_constraint where conname = 'resume_files_resume_fkey') then alter table public.resume_files add constraint resume_files_resume_fkey FOREIGN KEY (resume_id) REFERENCES resumes(id) ON DELETE CASCADE; end if; end $$;

update public.resumes t
set active = true
where t.active is not true
  and not exists (select 1 from public.resumes a
                  where a.username = t.username and a.active)
  and t.id = (select r.id
              from public.resumes r
              left join public.users u on u.username = r.username
              where r.username = t.username
              order by (r.content = u.resume) desc nulls last, r.created_at desc
              limit 1);

update public.users u
set resume = r.content
from public.resumes r
where r.username = u.username and r.active
  and coalesce(u.resume, '') = '' and coalesce(r.content, '') <> '';

notify pgrst, 'reload schema';"""


def list_resume_files(username, resume_id=None):
    """File METADATA for this user (or one resume). Never returns b64 -- see the note above."""
    if has_remote_db():
        try:
            params = {"username": "eq.%s" % username,
                      "select": ",".join(RESUME_FILE_META), "order": "created_at.desc"}
            if resume_id:
                params["resume_id"] = "eq.%s" % resume_id
            r = _http.get(_rest(RESUME_FILES_TABLE), headers=_headers(), params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception:
            return []
    data = _load_json(RESUME_FILES_FILE)
    rows = data.get(username, []) if isinstance(data, dict) else []
    if resume_id:
        rows = [f for f in rows if f.get("resume_id") == resume_id]
    return [{k: f.get(k) for k in RESUME_FILE_META} for f in rows]


def get_resume_file(username, fid):
    """One file WITH its bytes. The only read that pulls a payload."""
    if has_remote_db():
        try:
            r = _http.get(_rest(RESUME_FILES_TABLE), headers=_headers(),
                          params={"id": "eq.%s" % fid, "username": "eq.%s" % username,
                                  "select": "*", "limit": "1"}, timeout=30)
            r.raise_for_status()
            rows = r.json()
            return rows[0] if rows else None
        except Exception:
            return None
    data = _load_json(RESUME_FILES_FILE)
    rows = data.get(username, []) if isinstance(data, dict) else []
    return next((f for f in rows if f.get("id") == fid), None)


def save_resume_file(username, rec):
    """Store one uploaded artifact. Returns (ok, id_or_error).

    One row per (resume_id, kind): re-uploading a PDF for the same resume REPLACES it rather than
    growing the table, which is the half the existing blob store never had.
    """
    import uuid
    rec = dict(rec)
    rec["username"] = username
    if rec.get("kind") not in RESUME_FILE_KINDS:
        return False, "unsupported kind %r" % (rec.get("kind"),)
    if not rec.get("id"):
        rec["id"] = uuid.uuid4().hex
    if not rec.get("created_at"):
        rec["created_at"] = _now()
    if rec.get("size") is None:
        rec["size"] = len(rec.get("b64") or "")
    payload = {k: rec.get(k) for k in RESUME_FILE_FIELDS if k in rec}
    for old in list_resume_files(username, rec.get("resume_id")):
        if old.get("kind") == rec["kind"] and old.get("id") != rec["id"]:
            try:
                delete_resume_file(username, old["id"])
            except Exception:
                pass
    if has_remote_db():
        resp = _http.post(
            _rest(RESUME_FILES_TABLE),
            headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            params={"on_conflict": "id"}, data=json.dumps(payload), timeout=60)
        if resp.status_code >= 400:
            return False, "save_resume_file %s: %s" % (resp.status_code, resp.text[:300])
        return True, rec["id"]
    data = _load_json(RESUME_FILES_FILE)
    if not isinstance(data, dict):
        data = {}
    data.setdefault(username, []).append(payload)
    _dump_json(RESUME_FILES_FILE, data)
    return True, rec["id"]


def delete_resume_file(username, fid):
    if has_remote_db():
        resp = _http.delete(_rest(RESUME_FILES_TABLE),
                            headers=_headers({"Prefer": "return=minimal"}),
                            params={"id": "eq.%s" % fid, "username": "eq.%s" % username},
                            timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError("delete_resume_file %s: %s" % (resp.status_code, resp.text[:200]))
        return
    data = _load_json(RESUME_FILES_FILE)
    if isinstance(data, dict) and username in data:
        data[username] = [f for f in data[username] if f.get("id") != fid]
        _dump_json(RESUME_FILES_FILE, data)


def prune_resume_files(days=RESUME_FILES_TTL_DAYS):
    """Age out stored artifacts. Returns rows removed (best effort).

    This exists because the app already ships a health warning that the tailored-résumé cache "has
    no expiry anywhere in the codebase". A blob store without a prune is a slow leak, so this one
    gets its prune in the same change that creates it rather than a year later.
    """
    cutoff = (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(days=int(days))).isoformat()
    if has_remote_db():
        try:
            resp = _http.delete(_rest(RESUME_FILES_TABLE),
                                headers=_headers({"Prefer": "return=representation"}),
                                params={"created_at": "lt.%s" % cutoff, "select": "id"},
                                timeout=60)
            if resp.status_code >= 400:
                return 0
            return len(resp.json() or [])
        except Exception:
            return 0
    data = _load_json(RESUME_FILES_FILE)
    if not isinstance(data, dict):
        return 0
    n = 0
    for user, rows in list(data.items()):
        keep = [f for f in rows if (f.get("created_at") or "") >= cutoff]
        n += len(rows) - len(keep)
        data[user] = keep
    if n:
        _dump_json(RESUME_FILES_FILE, data)
    return n


# ---- Resume Brain knowledge base ----
# Per-user stories + lessons + self-training model live in a `brain_kb` JSON column on the
# users row (one ALTER). Company research is SHARED across users (public facts) in its own
# `brain_companies` table. Both fall back to local JSON so the app runs with zero DB setup;
# if the Supabase column/table doesn't exist yet, reads/writes degrade to the local files.
BRAIN_KB_FILE = "brain_kb_local.json"            # {username: {stories, lessons, model}}
BRAIN_COMPANIES_TABLE = "brain_companies"
BRAIN_COMPANIES_FILE = "brain_companies_local.json"   # {domain: {...research...}}


def _brain_kb_default():
    return {"stories": [], "lessons": [], "model": {"assoc": {}, "df": {}, "n": 0}}


def get_brain_kb(username):
    """Per-user Resume Brain data: {stories:[], lessons:[], model:{}}. Reads users.brain_kb
    (Supabase) and falls back to a local file when the column is absent. Never raises."""
    kb = None
    if username:
        try:
            if has_remote_db():
                kb = (get_user(username, "brain_kb") or {}).get("brain_kb")
                if kb is None:                              # column missing/empty -> local backup
                    kb = _load_json(BRAIN_KB_FILE).get(username)
            else:
                kb = _load_json(BRAIN_KB_FILE).get(username)
        except Exception:
            try:
                kb = _load_json(BRAIN_KB_FILE).get(username)
            except Exception:
                kb = None
    if isinstance(kb, str):
        try:
            kb = json.loads(kb)
        except Exception:
            kb = None
    if not isinstance(kb, dict):
        kb = _brain_kb_default()
    kb.setdefault("stories", [])
    kb.setdefault("lessons", [])
    kb.setdefault("model", {})
    kb["model"].setdefault("assoc", {})
    kb["model"].setdefault("df", {})
    kb["model"].setdefault("n", 0)
    return kb


def _save_brain_kb_local(username, kb):
    data = _load_json(BRAIN_KB_FILE)
    if not isinstance(data, dict):
        data = {}
    data[username] = kb
    _dump_json(BRAIN_KB_FILE, data)


def save_brain_kb(username, kb):
    """Persist a user's KB. Tries Supabase (users.brain_kb jsonb); on any failure (e.g. the
    column hasn't been added yet) falls back to a local file so data is never lost."""
    if not username:
        return
    if has_remote_db():
        try:
            _patch_user(username, {"brain_kb": kb})
            return
        except Exception:
            pass
    _save_brain_kb_local(username, kb)


def get_brain_company(domain):
    """Shared company-research record by domain (any user's crawl benefits everyone)."""
    if not domain:
        return None
    try:
        if has_remote_db():
            r = _http.get(_rest(BRAIN_COMPANIES_TABLE), headers=_headers(),
                          params={"domain": "eq.%s" % domain, "select": "*", "limit": 1}, timeout=20)
            r.raise_for_status()
            rows = r.json()
            if rows:
                d = rows[0].get("data")
                return json.loads(d) if isinstance(d, str) else d
            return _load_json(BRAIN_COMPANIES_FILE).get(domain)   # local backup
    except Exception:
        pass
    data = _load_json(BRAIN_COMPANIES_FILE)
    return data.get(domain) if isinstance(data, dict) else None


def put_brain_company(domain, rec):
    """Upsert shared company research (keyed on domain). Local fallback on DB failure."""
    if not domain:
        return
    rec = dict(rec)
    rec["domain"] = domain
    rec["fetched_at"] = _now()
    if has_remote_db():
        try:
            payload = {"domain": domain, "data": rec, "fetched_at": rec["fetched_at"]}
            resp = _http.post(
                _rest(BRAIN_COMPANIES_TABLE),
                headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                params={"on_conflict": "domain"}, data=json.dumps(payload), timeout=30)
            if resp.status_code < 400:
                return
        except Exception:
            pass
    data = _load_json(BRAIN_COMPANIES_FILE)
    if not isinstance(data, dict):
        data = {}
    data[domain] = rec
    _dump_json(BRAIN_COMPANIES_FILE, data)


def list_brain_companies():
    """{domain: record} for all researched companies (shared)."""
    try:
        if has_remote_db():
            out = {}
            for row in _fetch_all(BRAIN_COMPANIES_TABLE, {"select": "*"}):
                d = row.get("data")
                if isinstance(d, str):
                    try:
                        d = json.loads(d)
                    except Exception:
                        d = None
                if isinstance(d, dict):
                    out[row.get("domain")] = d
            if out:
                return out
    except Exception:
        pass
    data = _load_json(BRAIN_COMPANIES_FILE)
    return data if isinstance(data, dict) else {}


def profile_text(username):
    """The matching profile: the LIVE résumé plus every story. This is what the feed scores
    jobs against.

    It used to concatenate every résumé in the library, which made the match % answer a question
    nobody asked. Someone keeping a PM résumé and an SWE résumé was scored against the union of
    both, so every job matched something and switching which résumé was live changed nothing at
    all -- the live flag was real in the database and invisible in the feed. One résumé at a
    time is what makes "how well do I fit this job" a question with an answer, and what makes
    choosing a different one mean something.

    STORIES STAY. They are not a competing résumé -- they are extra evidence for the same person,
    they do not contradict whichever résumé is live, and folding them in is the whole point of
    "teach your brain". Only the OTHER résumés are excluded.

    Narrowing this lowers every score, so whether it needed a core.MIN_SCALE bump was measured
    rather than assumed: about 14% off the middle of the distribution, which is a change of
    degree and not of meaning. The arithmetic and the decision are recorded at MIN_SCALE. Bump it
    if this ever narrows further.
    """
    row = get_active_resume(username) or {}
    parts = [row.get("content", "") or ""]
    for s in get_brain_kb(username).get("stories", []):
        parts.append((s.get("title", "") or "") + " " + (s.get("text", "") or "")
                     + " " + " ".join(s.get("skills", []) or []))
    return "\n".join(p for p in parts if p.strip())


# ---- user profile (name/email/phone/work-auth) — for the Chrome extension autofill ----
PROFILES_TABLE = "profiles"
PROFILES_FILE = "profiles_local.json"
# NOTE: extra columns added below are also added to public.profiles via APPLICATIONS_SQL.
# get_profile selects *, so adding a column here + the ALTER is all that's needed.
PROFILE_FIELDS = (
    "username", "name", "email", "phone", "location", "linkedin",
    "work_authorized", "needs_sponsorship", "default_resume", "notes",
    # identity
    "first_name", "last_name", "pronouns",
    # address
    "address_line1", "address_line2", "city", "state", "postal_code", "country",
    # links
    "github", "portfolio", "website",
    # work authorization
    "work_auth_status", "requires_sponsorship_now", "requires_sponsorship_future",
    # work-authorization timeline (dates the user enters; read by core.visa_timeline).
    # Also listed in PROFILE_OPT_FIELDS below — save_profile drops them and retries if the
    # migration hasn't been run, so an un-migrated database can still save everything else.
    "program_end_date", "opt_type", "opt_start_date", "opt_end_date",
    "stem_eligible", "unemployment_days_used",
    # saved feed filters + digest opt-in
    "search_prefs",
    # EEO / voluntary self-identification
    "gender", "race_ethnicity", "hispanic_latino", "veteran_status", "disability_status",
    # compensation & logistics
    "desired_salary", "salary_currency", "available_start_date",
    "willing_to_relocate", "how_did_you_hear",
    # free-form JSON: misc answers + recurring custom-question answers (keyed by question hash)
    "extra", "application_defaults",
    "updated_at",
)
_PROFILE_JSON_FIELDS = ("extra", "application_defaults", "search_prefs")
# Profile columns that only exist after the latest ALTERs in APPLICATIONS_SQL have been run.
# save_profile retries without these on a 400 so an un-migrated database still saves the rest.
PROFILE_OPT_FIELDS = ("program_end_date", "opt_type", "opt_start_date", "opt_end_date",
                      "stem_eligible", "unemployment_days_used", "search_prefs")


def _decode_profile(rec):
    """Coerce the jsonb columns to dicts (Supabase may hand them back as strings)."""
    if not isinstance(rec, dict):
        return {}
    for k in _PROFILE_JSON_FIELDS:
        v = rec.get(k)
        if isinstance(v, str):
            try:
                rec[k] = json.loads(v or "{}")
            except Exception:
                rec[k] = {}
        elif v is None:
            rec[k] = {}
    return rec


def get_profile(username):
    """This user's profile dict (or {} if none / missing table)."""
    if has_remote_db():
        try:
            r = _http.get(_rest(PROFILES_TABLE), headers=_headers(),
                             params={"username": "eq.%s" % username, "select": "*", "limit": 1}, timeout=30)
            r.raise_for_status()
            rows = r.json()
            return _decode_profile(rows[0]) if rows else {}
        except Exception:
            return {}
    data = _load_json(PROFILES_FILE)
    return _decode_profile(data.get(username) or {}) if isinstance(data, dict) else {}


def save_profile(username, fields):
    """Upsert the user's profile (PK=username). Returns (ok, message)."""
    rec = {k: fields.get(k) for k in PROFILE_FIELDS if k in fields}
    for k in _PROFILE_JSON_FIELDS:                       # accept dicts or JSON strings
        if isinstance(rec.get(k), str):
            try:
                rec[k] = json.loads(rec[k] or "{}")
            except Exception:
                rec[k] = {}
    rec["username"] = username
    rec["updated_at"] = _now()
    if has_remote_db():
        def _post(payload):
            return _http.post(
                _rest(PROFILES_TABLE),
                headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                params={"on_conflict": "username"}, data=json.dumps(payload), timeout=30)

        resp = _post(rec)
        # A profile column added by a later migration makes PostgREST reject the WHOLE upsert,
        # which would break saving name/address/EEO too — not just the new field. So drop the
        # migration-dependent keys and retry once, and report that they weren't saved.
        if resp.status_code >= 400 and any(k in rec for k in PROFILE_OPT_FIELDS):
            trimmed = {k: v for k, v in rec.items() if k not in PROFILE_OPT_FIELDS}
            retry = _post(trimmed)
            if retry.status_code < 400:
                return True, ("saved, but the work-authorization dates need a one-time "
                              "migration first (see the setup SQL on the Applications page)")
            resp = retry
        if resp.status_code >= 400:
            return False, "Supabase save_profile %s: %s" % (resp.status_code, resp.text[:300])
        return True, ""
    data = _load_json(PROFILES_FILE)
    if not isinstance(data, dict):
        data = {}
    data[username] = {**(data.get(username) or {}), **rec}
    _dump_json(PROFILES_FILE, data)
    return True, ""


# ---- tailored-résumé cache (avoid re-paying Gemini + LaTeX compile on retriggers) ----
# Keyed by a caller-built hash of (résumé, job, format). Same local-or-Supabase + graceful
# degrade pattern as brain_companies; never raises (a cache miss must never break tailoring).
TAILORED_CACHE_TABLE = "tailored_cache"
TAILORED_CACHE_FILE = "tailored_cache_local.json"        # {cache_key: payload}


def get_tailored(cache_key):
    """Return a cached tailor payload for cache_key, or None."""
    if not cache_key:
        return None
    try:
        if has_remote_db():
            r = _http.get(_rest(TAILORED_CACHE_TABLE), headers=_headers(),
                          params={"id": "eq.%s" % cache_key, "select": "data", "limit": 1}, timeout=20)
            r.raise_for_status()
            rows = r.json()
            if rows:
                d = rows[0].get("data")
                return json.loads(d) if isinstance(d, str) else d
            # fall through to local backup
    except Exception:
        pass
    data = _load_json(TAILORED_CACHE_FILE)
    return data.get(cache_key) if isinstance(data, dict) else None


def put_tailored(cache_key, payload, username=""):
    """Upsert a tailor payload (keyed on cache_key). Local fallback on DB failure."""
    if not cache_key:
        return
    if has_remote_db():
        try:
            body = {"id": cache_key, "username": username, "data": payload, "created_at": _now()}
            resp = _http.post(
                _rest(TAILORED_CACHE_TABLE),
                headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                params={"on_conflict": "id"}, data=json.dumps(body), timeout=30)
            if resp.status_code < 400:
                return
        except Exception:
            pass
    data = _load_json(TAILORED_CACHE_FILE)
    if not isinstance(data, dict):
        data = {}
    data[cache_key] = payload
    _dump_json(TAILORED_CACHE_FILE, data)


# ---- learned answers ("training" the auto-apply: how this user answers each question) ----
# The extension captures {label, value, type, options} from forms the user fills (or auto-fills and
# the user keeps), normalizes the label to a key, and upserts here. The fill flow then prefers the
# user's OWN past answer over an AI guess. Same local-or-Supabase + graceful-degrade pattern.
LEARNED_TABLE = "learned_answers"
LEARNED_FILE = "learned_answers_local.json"              # {username: {key: {value,type,options,company,count,label}}}


@functools.lru_cache(maxsize=8192)
def _normalize_label(s):
    """The three substitutions below, memoized. See normalize_label for why."""
    s = s.lower()
    s = re.sub(r"\((?:[^()]*\b(required|optional)\b[^()]*)\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200]


def normalize_label(label):
    """Stable key for matching the same question across forms/ATS: lowercased, asterisks/parens
    stripped, non-alphanumerics collapsed to spaces. 'Are you authorized to work in the US?*' and
    'Are you authorized to work in the US' map to the same key.

    Memoized, because block_key() routes COMPANY NAMES through here and those callers are
    per-row: the job page scans the whole corpus for other roles at this employer, and so does
    every research poll behind it. That was three re.sub per row to re-derive a few thousand
    distinct answers. Bounded (lru_cache, not a plain dict) because the other caller is ATS
    form labels, which are unbounded in shape and come from pages we do not control.

    `label or ""` stays OUT of the memoized function so it only ever sees a str: None and ""
    must not become two entries, and an unhashable argument must never reach lru_cache.
    """
    return _normalize_label(label or "")


def get_learned(username):
    """Map of {key: {value,type,options,company,count,label}} for the user. {} if none/unavailable."""
    if not username:
        return {}
    if has_remote_db():
        try:
            r = _http.get(_rest(LEARNED_TABLE), headers=_headers(),
                          params={"username": "eq.%s" % username,
                                  "select": "key,label,value,type,options,company,count", "limit": 5000}, timeout=20)
            if r.status_code < 400:
                out = {}
                for row in r.json():
                    out[row.get("key")] = {"value": row.get("value", ""), "type": row.get("type"),
                                           "options": row.get("options"), "company": row.get("company"),
                                           "count": row.get("count", 0), "label": row.get("label")}
                return out                                # authoritative even when empty
        except Exception:
            pass
    data = _load_json(LEARNED_FILE)
    return (data.get(username) or {}) if isinstance(data, dict) else {}


def save_learned(username, items):
    """Upsert captured answers. items: [{label,type,value,options?,company?}]. Latest value wins;
    count increments. Returns the number of answers stored. Best-effort (never raises)."""
    if not username or not items:
        return 0
    existing = get_learned(username)
    rows, seen = [], set()
    for it in items:
        label = (it.get("label") or "").strip()
        value = it.get("value")
        if not label or value in (None, ""):
            continue
        key = normalize_label(label)
        if not key or key in seen:
            continue
        seen.add(key)
        prev = existing.get(key) or {}
        rows.append({"username": username, "key": key, "label": label[:300],
                     "value": str(value)[:600], "type": (it.get("type") or "text")[:30],
                     "options": it.get("options") or None, "company": (it.get("company") or "")[:120],
                     "count": int(prev.get("count", 0) or 0) + 1, "updated_at": _now()})
    if not rows:
        return 0
    if has_remote_db():
        try:
            resp = _http.post(_rest(LEARNED_TABLE),
                              headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                              params={"on_conflict": "username,key"}, data=json.dumps(rows), timeout=30)
            if resp.status_code < 400:
                return len(rows)
        except Exception:
            pass
    data = _load_json(LEARNED_FILE)
    if not isinstance(data, dict):
        data = {}
    u = data.get(username) or {}
    for r in rows:
        u[r["key"]] = {"value": r["value"], "type": r["type"], "options": r["options"],
                       "company": r["company"], "count": r["count"], "label": r["label"]}
    data[username] = u
    _dump_json(LEARNED_FILE, data)
    return len(rows)


def delete_learned(username, key):
    """Remove one learned answer (by normalized key) from the user's bank. Best-effort; returns True
    on success. Used by the 'manage learned answers' UI in the extension."""
    if not username or not key:
        return False
    if has_remote_db():
        try:
            resp = _http.delete(_rest(LEARNED_TABLE), headers=_headers(),
                                params={"username": "eq.%s" % username, "key": "eq.%s" % key}, timeout=20)
            if resp.status_code < 400:
                return True
        except Exception:
            pass
    data = _load_json(LEARNED_FILE)
    if isinstance(data, dict) and isinstance(data.get(username), dict) and key in data[username]:
        del data[username][key]
        _dump_json(LEARNED_FILE, data)
        return True
    return False


# ---- shared key/value blobs ----
# `scrape_status` is (id text primary key, data jsonb, updated_at timestamptz) and was created
# for the one row the "Update jobs" bar polls. The shape is a generic kv store and the id column
# takes any key, so the admin panel's database-size history lives here too rather than earning a
# migration of its own. Works without the table (local-file fallback); never raises — a status
# write must not be able to break a scrape.
SCRAPE_STATUS_TABLE = "scrape_status"
SCRAPE_STATUS_FILE = "scrape_status_local.json"
_SCRAPE_STATUS_KEY = "current"


def put_kv(key, obj):
    """Persist a JSON blob under `key`. Stamps updated_at (UTC, ISO) into the blob itself as
    well as the column, because the browser reads it out of the JSON. Best-effort."""
    try:
        rec = dict(obj or {})
        # UTC with an offset so the browser parses it correctly — a scrape may run on GitHub's
        # UTC runners while the viewer is in any timezone, and a naive local time skews elapsed.
        rec["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if has_remote_db():
            try:
                payload = {"id": key, "data": rec, "updated_at": rec["updated_at"]}
                resp = _http.post(
                    _rest(SCRAPE_STATUS_TABLE),
                    headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                    params={"on_conflict": "id"}, data=json.dumps(payload), timeout=15)
                if resp.status_code < 400:
                    return
            except Exception:
                pass
        # Local fallback is a dict OF blobs keyed by id. The file used to hold the bare status
        # dict, so a pre-existing one is migrated on first write rather than clobbered.
        blob = _load_json(SCRAPE_STATUS_FILE)
        if not isinstance(blob, dict) or "phase" in blob:
            blob = {_SCRAPE_STATUS_KEY: blob} if isinstance(blob, dict) and blob else {}
        blob[key] = rec
        _dump_json(SCRAPE_STATUS_FILE, blob)
    except Exception:
        pass


def get_kv(key, default=None):
    """The JSON blob stored under `key`, or `default` ({} unless given). Never raises."""
    fallback = {} if default is None else default
    try:
        if has_remote_db():
            try:
                r = _http.get(_rest(SCRAPE_STATUS_TABLE), headers=_headers(),
                              params={"id": "eq.%s" % key, "select": "data", "limit": 1},
                              timeout=15)
                if r.status_code < 400:
                    rows = r.json()
                    if rows:
                        d = rows[0].get("data")
                        if isinstance(d, str):
                            d = json.loads(d)
                        if isinstance(d, dict):
                            return d
            except Exception:
                pass
        blob = _load_json(SCRAPE_STATUS_FILE)
        if isinstance(blob, dict):
            # Old shape: the file WAS the status dict. Only "current" can be served from it.
            if "phase" in blob:
                return blob if key == _SCRAPE_STATUS_KEY else fallback
            d = blob.get(key)
            if isinstance(d, dict):
                return d
        return fallback
    except Exception:
        return fallback


def set_scrape_status(d):
    """Persist the current scrape progress dict (phase/done/total/found/started_at/...)."""
    put_kv(_SCRAPE_STATUS_KEY, d)


def get_scrape_status():
    """The latest scrape progress dict, or {} if none."""
    return get_kv(_SCRAPE_STATUS_KEY)


# ---- database size (admin panel) ----
# PostgREST cannot run arbitrary SQL, so Postgres' own size functions are only reachable through
# a stored function exposed at /rpc/. This text is kept here — beside APPLICATIONS_SQL and
# JOBS_DERIVED_SQL — so the admin page can render it as a paste-this block when the function
# doesn't exist yet, which is the same self-serve pattern the boards and applications tables use.
DB_STATS_SQL = """-- JobMatch — database size for the admin panel.
-- Paste into Supabase -> SQL Editor -> Run. Safe to re-run.

create or replace function public.db_stats()
returns json
language sql
security definer
set search_path = public, pg_catalog
as $$
  select json_build_object(
    'db_bytes',    pg_database_size(current_database()),
    'db_pretty',   pg_size_pretty(pg_database_size(current_database())),
    'measured_at', now(),
    'tables', (
      -- NOTE the ordering lives INSIDE json_agg. Sorting the rendered JSON instead would
      -- compare total_bytes as text and put 9 MB above 400 MB.
      select coalesce(json_agg(json_build_object(
               'table',       x.relname,
               'est_rows',    x.reltuples::bigint,
               'total_bytes', x.total_bytes,
               'table_bytes', x.table_bytes,
               'index_bytes', x.index_bytes,
               'toast_bytes', x.toast_bytes,
               'pretty',      pg_size_pretty(x.total_bytes)
             ) order by x.total_bytes desc), '[]'::json)
      from (
        select c.relname, c.reltuples,
               pg_total_relation_size(c.oid)                        as total_bytes,
               pg_table_size(c.oid)                                 as table_bytes,
               pg_indexes_size(c.oid)                               as index_bytes,
               coalesce(pg_total_relation_size(c.reltoastrelid), 0) as toast_bytes
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relkind = 'r'
      ) x)
  );
$$;

revoke all on function public.db_stats() from public;
grant execute on function public.db_stats() to anon, authenticated, service_role;

-- PostgREST caches the schema; without this the new function 404s until it reloads.
notify pgrst, 'reload schema';
"""


# ---- product analytics events ----
# The admin page shows this path when an events write fails because the table is not there yet,
# so it has to name a file that EXISTS. It used to be SUPABASE_EVENTS_MIGRATION.sql, which was
# deleted with the Supabase transport on 2026-09-01; schema.sql is the live DDL now, regenerated
# by scripts/dump_schema.py from the database itself rather than hand-maintained.
EVENTS_TABLE = "events"
EVENTS_DAILY_TABLE = "events_daily"
EVENTS_SQL_FILE = "schema.sql"


def insert_events(rows):
    """Bulk-insert analytics events. Returns True on success. NEVER raises.

    A plain insert, not an upsert: `id` is a bigserial and there is no conflict target.
    _upsert() can't be reused here — it is hard-wired to the jobs table — so this follows the
    same inline-POST convention every other non-jobs table in this file uses.

    Analytics must never break a request or a scrape, so every failure is swallowed. The caller
    (analytics.py) counts consecutive failures and stops trying rather than retrying forever.
    """
    if not rows or not has_remote_db():
        return False
    # PostgREST requires a uniform key set across a bulk insert; a missing key in one row of
    # the batch makes it reject the whole batch rather than defaulting that column.
    keys = sorted({k for r in rows for k in r})
    payload = [{k: r.get(k) for k in keys} for r in rows]
    body = json.dumps(payload)
    ok, why = _post_events(body)
    if ok:
        return True
    # SELF-HEAL, ONCE. The migration to cPanel copied 13,293 rows with their ids and never
    # advanced events_id_seq, so every insert since collided with events_pkey and this function
    # -- which swallows failures so analytics can never break a request -- hid a three-day
    # outage. A duplicate key here means the sequence is behind the data, and that is repairable
    # without a human: advance it and retry the same batch.
    if _seq_repair_once(why):
        ok, why = _post_events(body)
        if ok:
            return True
    _note_event_failure(why)
    return False


_events_failures = 0
_events_last_error = ""
_events_seq_repaired = False


def _post_events(body):
    """(ok, reason). Split out so the retry below posts byte-identical bytes."""
    try:
        resp = _http.post(_rest(EVENTS_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                          data=body, timeout=15)
        if resp.status_code < 400:
            return True, ""
        return False, (resp.text or "")[:300]
    except Exception as e:
        return False, str(e)[:300]


def _seq_repair_once(why):
    """True if a sequence repair was just performed and the caller should retry.

    Gated on the error text AND on the direct-Postgres transport: pgrest.Session is the only
    backend with a repair_sequences(), and a process reaching the database through the HTTP
    proxy has no business rewriting sequence state on the far side.
    """
    global _events_seq_repaired
    if _events_seq_repaired or not PG_DSN:
        return False
    low = (why or "").lower()
    if "duplicate key" not in low and "unique constraint" not in low:
        return False
    _events_seq_repaired = True
    try:
        fixed = _http.repair_sequences()
    except Exception as e:
        print("events: sequence repair failed: %s" % str(e)[:200], file=sys.stderr)
        return False
    print("events: id sequence was behind the data; repaired %s"
          % ", ".join("%s.%s->%s" % f for f in fixed), file=sys.stderr)
    return True


def _note_event_failure(why):
    """Count it, and say it ONCE. Silence is what made this cost three days."""
    global _events_failures, _events_last_error
    _events_failures += 1
    _events_last_error = why or "unknown"
    if _events_failures == 1:
        print("events: insert failed, analytics will be incomplete: %s" % _events_last_error,
              file=sys.stderr)


def newest_event_ts():
    """The most recent event's timestamp, or "".

    A one-row ordered select, NOT _fetch_all — that helper overwrites `limit` with its 1000-row
    page size and would walk the whole events table to answer a question about one row.
    """
    if not has_remote_db():
        return ""
    try:
        r = _http.get(_rest(EVENTS_TABLE), headers=_headers(),
                      params={"select": "ts", "order": "ts.desc", "limit": 1}, timeout=15)
        if r.status_code >= 400:
            return ""
        rows = r.json() or []
        return (rows[0].get("ts") or "") if rows else ""
    except Exception:
        return ""


def events_health():
    """{failures, last_error, seq_repaired} for /admin/health.json."""
    return {"failures": _events_failures, "last_error": _events_last_error,
            "seq_repaired": _events_seq_repaired}


def ev_usage(days=7):
    """Aggregated event stats via the public.ev_usage() RPC. {} if it isn't installed.

    Deliberately an RPC rather than pulling rows: _fetch_all pages at 1000 a request (and
    defaults to `order=url`, a column this table doesn't have), so reading 60k events over the
    wire from shared cPanel is 60 sequential round trips for numbers Postgres can produce in one.
    """
    if not has_remote_db():
        return {}
    try:
        r = _http.post(_rest("rpc/ev_usage"), headers=_headers(),
                       data=json.dumps({"days": int(days)}), timeout=25)
        if r.status_code >= 400:
            return {}
        d = r.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def events_daily(since_day):
    """Pre-aggregated daily counts from `since_day` (ISO date). [] if unavailable."""
    if not has_remote_db():
        return []
    try:
        r = _http.get(_rest(EVENTS_DAILY_TABLE), headers=_headers(),
                      params={"select": "*", "day": "gte.%s" % since_day,
                              "order": "day", "limit": 20000}, timeout=25)
        return r.json() if r.status_code < 400 else []
    except Exception:
        return []


def prune_events(before_day):
    """Delete raw events older than `before_day` (ISO date). Returns True if it ran.

    Deleting does not hand space back to the OS — autovacuum reclaims it for reuse — so the
    table PLATEAUS rather than shrinking. That is the intended outcome; the dashboard number
    settling instead of dropping is not a bug.
    """
    if not has_remote_db():
        return False
    try:
        r = _http.delete(_rest(EVENTS_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                         params={"ts": "lt.%s" % before_day}, timeout=60)
        return r.status_code < 400
    except Exception:
        return False


def db_stats():
    """Database and per-table sizes via the public.db_stats() RPC.

    {} when the function hasn't been created yet (PostgREST answers 404/PGRST202) or on any
    error — the admin page then renders DB_STATS_SQL as a paste-this block instead of a number.

    Two caveats the caller must surface rather than hide: `est_rows` is autovacuum's estimate
    (-1 on a never-analyzed table) and must never be shown as a count — table_count() is the
    real one; and pg_database_size is a FLOOR on what Supabase bills, which counts the whole
    instance including the auth/storage schemas and WAL.
    """
    if not has_remote_db():
        return {}
    try:
        r = _http.post(_rest("rpc/db_stats"), headers=_headers(), data="{}", timeout=20)
        if r.status_code >= 400:
            return {}
        d = r.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


if __name__ == "__main__":
    import sys
    if not has_remote_db():
        print("Storage backend: local files")
        print("Jobs available:", len(load_jobs()))
    else:
        try:
            # One row proves the url, the key and the table; the count then rides a HEAD, which
            # returns no body at all. This used to be len(load_jobs()) — every row WITH its
            # description, ~130 MB, to print one integer, from the command the setup guide hands
            # you when your credentials are broken and you are about to run it several times.
            sample_jobs(1, cols="url")
            n = table_count(TABLE)
            print("Storage backend: %s (connected OK)" % backend_name())
            print("Jobs in table:", n if n is not None else "?")
        except Exception as e:
            print("Storage backend: %s configured, but a request FAILED:"
                  % backend_name())
            print("   ", repr(e))
            print("Check: did you run the CREATE TABLE sql, and are the URL + key correct?")
            sys.exit(1)
