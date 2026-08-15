"""
db.py — storage layer for the job tool.

Talks to Supabase through its PostgREST REST API using `requests` (no extra SDK, so
it installs cleanly everywhere — including Python 3.14). Falls back to local files
(jobs.csv + user_jobs.json) when no credentials are set, so the tool keeps working
locally with zero setup. The same code runs both ways.

Credentials (checked in order):
  1. env vars  SUPABASE_URL / SUPABASE_KEY        (GitHub Actions)
  2. Streamlit secrets  [supabase] url / key       (local + deployed app)

See SUPABASE_SETUP.md for the one-time table + keys setup.
"""
import os
import csv
import json
import re
import sys
import time
import datetime


def _make_http():
    """Session for all Supabase REST calls: keep-alive pooling + automatic retry with
    backoff on transient failures. Shared-network blips (the recurring WinError 10054
    'connection forcibly closed' during chunked JD upserts) used to abort a whole
    score run; now each request retries itself. Retrying writes is safe here because
    every write is idempotent — upserts keyed on url, patches/deletes on eq filters."""
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    retry = Retry(total=3, connect=3, read=2, backoff_factor=0.5,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset({"GET", "POST", "PATCH", "DELETE", "HEAD"}),
                  respect_retry_after_header=True)
    s = requests.Session()
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


_http_session = None


# Set PG_DSN and this module talks to a Postgres server DIRECTLY instead of to Supabase's
# PostgREST — for a self-hosted database (cPanel, a VPS) or any managed Postgres that isn't
# Supabase. pgrest.py implements the five HTTP verbs over psycopg and translates the query
# language, so none of the 75 functions below change and neither does anything that calls them.
# Unset it and you are back on Supabase, which is what makes it safe to try.
#
#   PG_DSN="postgresql://user:pw@localhost:5432/dbname"
PG_DSN = os.environ.get("PG_DSN") or ""


class _LazyHTTP:
    """Defers building the requests.Session (and the ~1 s `import requests`) until the
    first actual DB call, so importing db.py stays cheap on a cold Passenger start. All
    `_http.get/post/...` call sites keep working unchanged."""
    def __getattr__(self, name):
        global _http_session
        if _http_session is None:
            # Three transports, one interface, checked most-specific first. PG_DSN wins because
            # a process that can reach the database directly should never route through HTTP to
            # reach itself — the cPanel app sets PG_DSN, the scraper sets DB_PROXY_*, and
            # neither should ever have both.
            if PG_DSN:
                import pgrest
                _http_session = pgrest.Session(PG_DSN)
            else:
                import dbproxy
                _http_session = dbproxy.client_from_env() or _make_http()
        return getattr(_http_session, name)


_http = _LazyHTTP()


def _load_env_file(path=".env"):
    """Load KEY=VALUE pairs from a .env file into os.environ (set-if-absent), so every
    module reads ONE config source: db's SUPABASE_*, web's GH_TOKEN, the scraper's
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
    "-- LAST. Without this PostgREST answers from its cached schema and every column added above\n"
    "-- reads as missing until it happens to reload.\n"
    "notify pgrst, 'reload schema';\n")

_creds_cache = None


def _creds():
    global _creds_cache
    if _creds_cache is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        if not (url and key):                       # .env file (any Python version; cPanel + cron)
            try:
                with open(".env", encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if ln and not ln.startswith("#") and "=" in ln:
                            k, v = ln.split("=", 1)
                            v = v.strip().strip('"').strip("'")
                            if k.strip() == "SUPABASE_URL":
                                url = url or v
                            elif k.strip() == "SUPABASE_KEY":
                                key = key or v
            except Exception:
                pass
        if not (url and key):                       # local secrets file (Streamlit, py3.11+)
            try:
                import tomllib
                with open(os.path.join(".streamlit", "secrets.toml"), "rb") as f:
                    sec = tomllib.load(f).get("supabase", {})
                url = url or sec.get("url")
                key = key or sec.get("key")
            except Exception:
                pass
        if not (url and key):                       # Streamlit Cloud secrets
            try:
                import streamlit as st
                sec = st.secrets.get("supabase", {})
                url = url or sec.get("url")
                key = key or sec.get("key")
            except Exception:
                pass
        _creds_cache = (url.rstrip("/") if url else None, key)
    return _creds_cache


def using_supabase():
    """True when there is a REMOTE database to talk to, of either kind.

    The name is historical and now reads as "not the local CSV fallback" — every caller uses it
    to choose between the network path and jobs.csv, and a direct-Postgres backend belongs on
    the network side of that question. Renaming it would touch ~40 call sites for no behavioural
    change, so the docstring carries the meaning instead."""
    if PG_DSN or (os.environ.get("DB_PROXY_URL") and os.environ.get("DB_PROXY_SECRET")):
        return True
    url, key = _creds()
    return bool(url and key)


def _rest(path=""):
    url, _ = _creds()
    if (PG_DSN or os.environ.get("DB_PROXY_URL")) and not url:
        # pgrest.Session only reads the part after /rest/v1/ to find the table, so the host is
        # a placeholder. Keeping the same shape means _rest()'s callers stay identical.
        return "pg://local/rest/v1/%s" % path
    return "%s/rest/v1/%s" % (url, path)


def _headers(extra=None):
    _, key = _creds()
    h = {"apikey": key, "Authorization": "Bearer %s" % key,
         "Content-Type": "application/json"}
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

    HEAD is already allowlisted by the retry adapter in _make_http, so this needs no plumbing.
    `params` takes the usual PostgREST filters, e.g. {"company": "eq.Tesla"}.
    """
    if not using_supabase():
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


def jobs_fingerprint():
    """(row_count, newest_first_seen) — a near-free "has the corpus changed?" probe.

    Neither half returns rows: the count rides the HEAD in table_count(), and the date is a
    one-row ordered select. Together they let a caller revalidate a cache instead of re-reading
    it — the difference between ~0 bytes and the ~10.7 MB a full feed read costs at 19k rows.

    `first_seen` (the date a row entered THIS database), not `last_seen`: last_seen is NULL on
    every row in the live table — measured 0 of 19,268 non-null — so it fingerprints nothing.
    first_seen is populated on all of them and advances whenever a job is inserted, while the
    count moves on inserts and on the 30-day prune. A prune and an insert of the same size
    therefore still register, because the new rows carry a newer first_seen.

    Returns (None, "") when unavailable. Callers MUST read that as "don't know" and refetch,
    never as "unchanged" — a probe that fails while the DB is briefly unreachable would
    otherwise pin a stale feed in place indefinitely.
    """
    if not using_supabase():
        return (None, "")
    n = table_count(TABLE)
    if n is None:
        return (None, "")
    try:
        r = _http.get(_rest(TABLE), headers=_headers(),
                      params={"select": "first_seen", "order": "first_seen.desc.nullslast",
                              "limit": 1}, timeout=15)
        if r.status_code >= 400:
            return (None, "")
        rows = r.json() or []
        return (n, (rows[0].get("first_seen") or "") if rows else "")
    except Exception:
        return (None, "")


def _upsert(rows, chunk=200):
    """Insert/merge rows on the `url` primary key (PostgREST upsert), in chunks with a soft retry.
    A single huge merge-upsert (a full re-score, or a big backlog of new jobs after the scheduled
    scrape has been down) is one giant statement; splitting it keeps each write small. On top of
    that, free-tier Supabase occasionally goes through a minute or two of HTTP 500s under load, so
    each chunk gets a couple of extra spaced-out attempts before we give up (and then we surface
    the real response body, not a bare RetryError). PostgREST needs every object in a bulk write
    to share the SAME keys, so we normalize to the union of keys (missing -> None)."""
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
        u = r.get("url")
        if u is None:
            passthrough.append(r)
            continue
        if u not in merged:
            merged[u] = {}
            order.append(u)
        merged[u].update({k: v for k, v in r.items() if v is not None})
    rows = [merged[u] for u in order] + passthrough
    keys = sorted({k for r in rows for k in r})
    for i in range(0, len(rows), chunk):
        payload = json.dumps([{k: r.get(k) for k in keys} for r in rows[i:i + chunk]])
        last = ""
        for attempt in range(4):           # ~0 + 3 + 6 + 12s of backoff rides out a transient 500 window
            try:
                resp = _http.post(
                    _rest(TABLE),
                    headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                    params={"on_conflict": "url"}, data=payload, timeout=60)
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

COLS_SCORE = ("url,found_date,location,first_seen,"
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
    failing here would take down a scrape.
    """
    if using_supabase():
        if cols:
            try:
                return _fetch_all(TABLE, {"select": cols})
            except Exception:
                pass                     # fall through to the wider, always-supported select
        if include_jd:
            _warn_full_jd_read()
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


def get_job_jd(url):
    """The stored job-description text for ONE job, fetched on demand (the feed list omits
    it). Tiny single-row lookup on the url primary key. Returns '' if absent / on error."""
    if not url:
        return ""
    if using_supabase():
        try:
            rows = _fetch_all(TABLE, {"url": "eq.%s" % url, "select": "jd"})
            return (rows[0].get("jd") or "") if rows else ""
        except Exception:
            return ""
    for r in _read_csv():
        if r.get("url") == url:
            return r.get("jd", "") or ""
    return ""


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
    if using_supabase():
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
    own `python db.py` connectivity check — the command SUPABASE_SETUP.md tells you to run when
    your credentials are NOT working, so it gets run repeatedly, in exactly the situation where
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
    if not using_supabase():
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
    if using_supabase():
        try:
            return {r["url"] for r in _fetch_all(TABLE, {"select": "url",
                                                         "or": "(jd.is.null,jd.eq.)"})
                    if r.get("url")}
        except Exception:
            return set()
    return {r["url"] for r in _read_csv() if not (r.get("jd") or "").strip()}


def urls_with_jd():
    """Set of job URLs that have a stored JD — for 'has a description?' checks without
    pulling the JD text. Cheap (urls only). Empty set on error.

    For the inverse question prefer urls_missing_jd(), which pages ~5.6x fewer rows."""
    if using_supabase():
        try:
            return {r["url"] for r in _fetch_all(TABLE, {"select": "url", "jd": "not.is.null"})
                    if r.get("url")}
        except Exception:
            return set()
    return {r["url"] for r in _read_csv() if (r.get("jd") or "").strip()}


def existing_urls():
    if using_supabase():
        return {row["url"] for row in _fetch_all(TABLE, {"select": "url"}) if row.get("url")}
    return {r["url"] for r in _read_csv()}


def add_jobs(rows):
    """Insert NEW jobs (deduped by url). rows = list of dicts."""
    if not rows:
        return
    if using_supabase():
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


def update_job_fields(rows):
    """Patch specific columns on existing jobs: rows = [{url, location?, found_date?}].
    Used by the extension's detail-fetch to fill in the real location / posting date for
    browser-imported jobs (the listing page often only had a code or nothing)."""
    rows = [r for r in rows if r.get("url")]
    if not rows:
        return
    if using_supabase():
        _upsert(rows)                      # merge-on-url updates only the given columns
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
    if using_supabase():
        _upsert([{"url": u, "match_score": int(s)} for u, s in scores.items()])
        return
    rows = _read_csv()
    for r in rows:
        if r.get("url") in scores:
            r["match_score"] = str(scores[r["url"]])
    _write_csv(rows)


def set_status(url, status):
    """status: 'liked' | 'hidden' | 'applied' | '' to clear."""
    if using_supabase():
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
    if using_supabase():
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
    if remote_only and not using_supabase():
        raise RuntimeError("delete_urls(remote_only=True) with no Supabase credentials. "
                           "refusing to touch the local-file fallback.")
    if using_supabase():
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
    if using_supabase():
        resp = _http.delete(_rest(TABLE), headers=_headers({"Prefer": "return=minimal"}),
                               params={"url": "neq.__none__"}, timeout=60)
        resp.raise_for_status()
    else:
        _write_csv([])


def all_flagged_urls():
    """Every job URL any user has liked / applied / hidden — so a prune never deletes a job
    someone is tracking. Empty set on error / local with no file."""
    if using_supabase():
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
    if using_supabase():
        rows = None
        # first_seen / posted_verified may not exist yet (see SUPABASE_PENDING_MIGRATION.sql);
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
    if not using_supabase():
        print("No Supabase credentials found. Set them first (see SUPABASE_SETUP.md).")
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
    if using_supabase():
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
    if using_supabase():
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
# columns, and drop back to the original pair if SUPABASE_ADMIN_MIGRATION.sql hasn't been run.
# Rows from the short select simply lack the keys, so every consumer must use .get().
_USER_COLS = ("username,created_at,disabled_at,token_epoch", "username,created_at")


def list_users():
    """Every account. Never includes password_hash — this feeds the admin table and the
    per-request account cache, neither of which has any business holding hashes."""
    if using_supabase():
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
    if using_supabase():
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
    """Disable (or re-enable) an account. Requires SUPABASE_ADMIN_MIGRATION.sql — without the
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
    return (USERJOBS_TABLE, PROFILES_TABLE, APPLICATIONS_TABLE, RESUMES_TABLE, LEARNED_TABLE)


def delete_user(username, dry_run=False):
    """Delete an account and everything keyed to it. Returns {table: rows_removed}.

    Children are deleted FIRST and `users` LAST: if a child delete fails we stop before
    removing the users row, so a partial failure leaves a live account rather than exactly the
    orphans this function exists to stop creating.

    The child deletes are redundant once SUPABASE_ADMIN_MIGRATION.sql has added the cascading
    foreign keys — kept anyway so this stays correct before the migration is run, on the
    local-file backend, and so dry_run can report per-table counts for the confirm screen.

    tailored_cache is skipped: put_tailored() writes username='' for anonymous entries, so it
    has no usable per-user filter and is pruned by age instead.
    """
    out = {}
    if using_supabase():
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


def blocked_company_keys():
    """set() of normalized names the ingestion paths must refuse. Empty on any failure."""
    try:
        return {(r.get("name_key") or "") for r in list_blocked() if r.get("name_key")}
    except Exception:
        return set()


def list_blocked():
    """[{name_key, name, reason, added_by, created_at}], newest first. [] if unavailable."""
    if using_supabase():
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
    if using_supabase():
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
    if using_supabase():
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
    if using_supabase():
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
    if using_supabase():
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
    if using_supabase():
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
    if using_supabase():
        return {row["url"]: row["status"]
                for row in _fetch_all(USERJOBS_TABLE,
                                      {"username": "eq.%s" % username, "select": "url,status"})
                if row.get("status")}
    return _load_json(USER_JOBS_FILE).get(username, {})


def set_user_status(username, url, status):
    """status: 'liked' | 'hidden' | 'applied' | '' to clear — scoped to one user."""
    if using_supabase():
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
                # SUPABASE_EVENTS_MIGRATION.sql hasn't been run: drop the column and retry, the
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
    rows = [{"url": u, "jd": (jd or "")[:8000]} for u, jd in jds.items() if u]   # cap: bound DB size
    if not rows:
        return
    if using_supabase():
        for i in range(0, len(rows), 30):     # ~30 JDs/request keeps the body small
            _upsert(rows[i:i + 30])
        return
    _dump_json(JDS_FILE, jds)


# ================= custom job boards (added through the app's "Add board" view) ====
BOARDS_TABLE = "boards"
BOARDS_FILE = "boards.json"          # local fallback


def list_boards():
    """[{url, ats_type, company, added_by, created_at}] of user-added boards.
    Defensive: a missing table / failed request returns [] so the scrape never breaks."""
    if using_supabase():
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
    if using_supabase():
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
    if using_supabase():
        resp = _http.delete(_rest(BOARDS_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                               params={"url": "eq.%s" % url}, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError("delete_board %s: %s" % (resp.status_code, resp.text[:200]))
        return
    rows = [b for b in list_boards() if b.get("url") != url]
    _dump_json(BOARDS_FILE, rows)


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
    if using_supabase():
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
    if using_supabase():
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
    if using_supabase():
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
RESUME_FIELDS = ("id", "username", "name", "content", "created_at")


def list_resumes(username):
    """This user's saved résumé versions. Defensive: missing table / error -> []."""
    if using_supabase():
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
    if using_supabase():
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
    if using_supabase():
        resp = _http.delete(_rest(RESUMES_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                               params={"id": "eq.%s" % rid, "username": "eq.%s" % username}, timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError("delete_resume %s: %s" % (resp.status_code, resp.text[:200]))
        return
    data = _load_json(RESUMES_FILE)
    if isinstance(data, dict) and username in data:
        data[username] = [a for a in data[username] if a.get("id") != rid]
        _dump_json(RESUMES_FILE, data)


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
            if using_supabase():
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
    if using_supabase():
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
        if using_supabase():
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
    if using_supabase():
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
        if using_supabase():
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
    """The COMPLETE matching profile: every résumé + every story combined. This is what the
    feed scores jobs against (per the 'match against the whole Resume Brain' design)."""
    parts = [r.get("content", "") or "" for r in list_resumes(username)]
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
    if using_supabase():
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
    if using_supabase():
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
        if using_supabase():
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
    if using_supabase():
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


def normalize_label(label):
    """Stable key for matching the same question across forms/ATS: lowercased, asterisks/parens
    stripped, non-alphanumerics collapsed to spaces. 'Are you authorized to work in the US?*' and
    'Are you authorized to work in the US' map to the same key."""
    s = (label or "").lower()
    s = re.sub(r"\((?:[^()]*\b(required|optional)\b[^()]*)\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:200]


def get_learned(username):
    """Map of {key: {value,type,options,company,count,label}} for the user. {} if none/unavailable."""
    if not username:
        return {}
    if using_supabase():
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
    if using_supabase():
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
    if using_supabase():
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
        if using_supabase():
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
        if using_supabase():
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
# Schema lives in SUPABASE_EVENTS_MIGRATION.sql; this constant is only the path so the admin
# page can point at it when a write fails because the table isn't there yet.
EVENTS_TABLE = "events"
EVENTS_DAILY_TABLE = "events_daily"
EVENTS_SQL_FILE = "SUPABASE_EVENTS_MIGRATION.sql"


def insert_events(rows):
    """Bulk-insert analytics events. Returns True on success. NEVER raises.

    A plain insert, not an upsert: `id` is a bigserial and there is no conflict target.
    _upsert() can't be reused here — it is hard-wired to the jobs table — so this follows the
    same inline-POST convention every other non-jobs table in this file uses.

    Analytics must never break a request or a scrape, so every failure is swallowed. The caller
    (analytics.py) counts consecutive failures and stops trying rather than retrying forever.
    """
    if not rows or not using_supabase():
        return False
    # PostgREST requires a uniform key set across a bulk insert; a missing key in one row of
    # the batch makes it reject the whole batch rather than defaulting that column.
    keys = sorted({k for r in rows for k in r})
    payload = [{k: r.get(k) for k in keys} for r in rows]
    try:
        resp = _http.post(_rest(EVENTS_TABLE), headers=_headers({"Prefer": "return=minimal"}),
                          data=json.dumps(payload), timeout=15)
        return resp.status_code < 400
    except Exception:
        return False


def ev_usage(days=7):
    """Aggregated event stats via the public.ev_usage() RPC. {} if it isn't installed.

    Deliberately an RPC rather than pulling rows: _fetch_all pages at 1000 a request (and
    defaults to `order=url`, a column this table doesn't have), so reading 60k events over the
    wire from shared cPanel is 60 sequential round trips for numbers Postgres can produce in one.
    """
    if not using_supabase():
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
    if not using_supabase():
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
    if not using_supabase():
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
    if not using_supabase():
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
    if not using_supabase():
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
            print("Storage backend: Supabase (connected OK)")
            print("Jobs in table:", n if n is not None else "?")
        except Exception as e:
            print("Storage backend: Supabase configured, but a request FAILED:")
            print("   ", repr(e))
            print("Check: did you run the CREATE TABLE sql, and are the URL + key correct?")
            sys.exit(1)
