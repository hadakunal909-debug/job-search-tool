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


class _LazyHTTP:
    """Defers building the requests.Session (and the ~1 s `import requests`) until the
    first actual DB call, so importing db.py stays cheap on a cold Passenger start. All
    `_http.get/post/...` call sites keep working unchanged."""
    def __getattr__(self, name):
        global _http_session
        if _http_session is None:
            _http_session = _make_http()
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
          "posted_verified", "posted_confidence"]

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
    url, key = _creds()
    return bool(url and key)


def _rest(path=""):
    url, _ = _creds()
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


def _upsert(rows):
    """Insert/merge rows on the `url` primary key (PostgREST upsert).
    PostgREST needs every object in a bulk write to share the SAME keys, so we
    normalize to the union of keys (missing -> None)."""
    if not rows:
        return
    keys = sorted({k for r in rows for k in r})
    body = [{k: r.get(k) for k in keys} for r in rows]
    resp = _http.post(
        _rest(TABLE),
        headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
        params={"on_conflict": "url"}, data=json.dumps(body), timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError("Supabase upsert %s: %s" % (resp.status_code, resp.text[:300]))


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
# posted_verified = real posting date recovered by scraper.verify_dates (preferred on the
# card over found_date). Listed explicitly so the feed select pulls it without the JD.
_FEED_COLS = "url,found_date,posted_verified,title,company,location,sponsors_h1b,match_score,status"


def load_jobs(include_jd=True):
    """All jobs. The web FEED passes include_jd=False to skip the large `jd` text column
    (~12 KB × ~2,600 rows) — the feed never shows it; the detail panel fetches one JD on
    demand via get_job_jd(). That drops the feed fetch from ~20 MB to ~1 MB. The scraper,
    scorer, and notifier keep the default (jd included) since they need the description."""
    if using_supabase():
        sel = "*" if include_jd else _FEED_COLS
        try:
            return _fetch_all(TABLE, {"select": sel})
        except Exception:
            # posted_verified not migrated yet -> retry without it so the feed keeps working
            # until `alter table jobs add column posted_verified` is run. (include_jd=True uses
            # "*", which never names the column, so only the explicit-column path needs this.)
            if not include_jd and "posted_verified" in _FEED_COLS:
                return _fetch_all(TABLE, {"select": _FEED_COLS.replace(",posted_verified", "")})
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


def urls_with_jd():
    """Set of job URLs that have a stored JD — for 'has a description?' checks without
    pulling the JD text. Cheap (urls only). Empty set on error."""
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
        _upsert([{k: r[k] for k in FIELDS if k in r and r[k] != ""} for r in rows])
        return
    existing = existing_urls()
    new = [r for r in rows if r.get("url") not in existing]
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


def delete_urls(urls):
    """Remove jobs by url (used when tightening the filter). Works on both backends."""
    urls = list(urls)
    if not urls:
        return
    if using_supabase():
        for u in urls:
            resp = _http.delete(
                _rest(TABLE), headers=_headers({"Prefer": "return=minimal"}),
                params={"url": "eq.%s" % u}, timeout=30)
            if resp.status_code >= 400:
                raise RuntimeError("Supabase delete %s: %s" % (resp.status_code, resp.text[:200]))
        return
    drop = set(urls)
    _write_csv([r for r in _read_csv() if r.get("url") not in drop])


def delete_all():
    """Wipe the jobs table (used when switching the whole source set)."""
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


def prune_old_jobs(days=60):
    """Delete jobs first seen more than `days` ago, EXCEPT any a user has flagged — keeps the
    corpus fresh and the DB bounded as the wider net grows it. Returns how many were removed.
    Defensive: never raises (a failed prune must not abort the scrape)."""
    try:
        cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).isoformat()
        if using_supabase():
            old = {r["url"] for r in _fetch_all(TABLE, {"select": "url", "found_date": "lt.%s" % cutoff})
                   if r.get("url")}
        else:
            old = {r["url"] for r in _read_csv()
                   if r.get("url") and (r.get("found_date") or "")[:10] and (r["found_date"][:10] < cutoff)}
        to_delete = list(old - all_flagged_urls())     # protect liked/applied/hidden
        if to_delete:
            delete_urls(to_delete)
        return len(to_delete)
    except Exception as e:
        print("prune_old_jobs skipped:", str(e)[:200])
        return 0


def import_from_files():
    """One-time migration: push local jobs.csv + user_jobs.json into Supabase."""
    if not using_supabase():
        print("No Supabase credentials found — set them first (see SUPABASE_SETUP.md).")
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
    if using_supabase():
        resp = _http.post(
            _rest(USERS_TABLE), headers=_headers({"Prefer": "return=minimal"}),
            data=json.dumps({"username": username, "password_hash": password_hash,
                             "resume": resume}), timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError("create_user %s: %s" % (resp.status_code, resp.text[:200]))
        return
    users = _load_json(USERS_FILE)
    users[username] = {"password_hash": password_hash, "resume": resume, "created_at": _now()}
    _dump_json(USERS_FILE, users)


def get_user(username):
    """Return {username, password_hash, resume, ...} or None."""
    if using_supabase():
        r = _http.get(_rest(USERS_TABLE), headers=_headers(),
                         params={"username": "eq.%s" % username, "select": "*", "limit": 1},
                         timeout=30)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None
    users = _load_json(USERS_FILE)
    if username in users:
        u = dict(users[username]); u["username"] = username
        return u
    return None


def list_users():
    if using_supabase():
        r = _http.get(_rest(USERS_TABLE), headers=_headers(),
                         params={"select": "username,created_at", "order": "created_at"}, timeout=30)
        r.raise_for_status()
        return r.json()
    users = _load_json(USERS_FILE)
    return [{"username": k, "created_at": v.get("created_at", "")} for k, v in users.items()]


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


def delete_user(username):
    if using_supabase():
        for table in (USERJOBS_TABLE, USERS_TABLE):
            resp = _http.delete(_rest(table), headers=_headers({"Prefer": "return=minimal"}),
                                   params={"username": "eq.%s" % username}, timeout=30)
            if resp.status_code >= 400:
                raise RuntimeError("delete_user %s: %s" % (resp.status_code, resp.text[:200]))
        return
    users = _load_json(USERS_FILE); users.pop(username, None); _dump_json(USERS_FILE, users)
    uj = _load_json(USER_JOBS_FILE); uj.pop(username, None); _dump_json(USER_JOBS_FILE, uj)


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
                data=json.dumps({"username": username, "url": url, "status": status}), timeout=30)
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
    "alter table public.profiles add column if not exists extra jsonb default '{}'::jsonb;\n"
    "alter table public.profiles add column if not exists application_defaults jsonb default '{}'::jsonb;\n\n"
    # --- tailored-résumé cache ---
    "create table if not exists public.tailored_cache (\n"
    "  id text primary key, username text, data jsonb, created_at timestamptz default now());\n"
    "create index if not exists tailored_cache_user_idx on public.tailored_cache (username);")


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
                kb = (get_user(username) or {}).get("brain_kb")
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
    # EEO / voluntary self-identification
    "gender", "race_ethnicity", "hispanic_latino", "veteran_status", "disability_status",
    # compensation & logistics
    "desired_salary", "salary_currency", "available_start_date",
    "willing_to_relocate", "how_did_you_hear",
    # free-form JSON: misc answers + recurring custom-question answers (keyed by question hash)
    "extra", "application_defaults",
    "updated_at",
)
_PROFILE_JSON_FIELDS = ("extra", "application_defaults")


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
        resp = _http.post(
            _rest(PROFILES_TABLE),
            headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
            params={"on_conflict": "username"}, data=json.dumps(rec), timeout=30)
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


# ---- live scrape progress (for the in-page "Update jobs" progress bar) ----
# One shared row the scraper updates as it runs; the feed page polls it. Supabase table:
#   create table scrape_status (id text primary key, data jsonb, updated_at timestamptz);
# Works without the table (local-file fallback); never raises (a status write must never
# break a scrape).
SCRAPE_STATUS_TABLE = "scrape_status"
SCRAPE_STATUS_FILE = "scrape_status_local.json"
_SCRAPE_STATUS_KEY = "current"


def set_scrape_status(d):
    """Persist the current scrape progress dict (phase/done/total/found/started_at/...).
    Best-effort: returns silently on any failure so it can't abort a scrape."""
    try:
        rec = dict(d or {})
        # UTC with 'Z' so the browser parses it correctly (the scrape may run on GitHub's
        # UTC runners while the viewer is in any timezone — naive local times would skew elapsed).
        rec["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if using_supabase():
            try:
                payload = {"id": _SCRAPE_STATUS_KEY, "data": rec, "updated_at": rec["updated_at"]}
                resp = _http.post(
                    _rest(SCRAPE_STATUS_TABLE),
                    headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                    params={"on_conflict": "id"}, data=json.dumps(payload), timeout=15)
                if resp.status_code < 400:
                    return
            except Exception:
                pass
        _dump_json(SCRAPE_STATUS_FILE, rec)
    except Exception:
        pass


def get_scrape_status():
    """The latest scrape progress dict, or {} if none. Never raises."""
    try:
        if using_supabase():
            try:
                r = _http.get(_rest(SCRAPE_STATUS_TABLE), headers=_headers(),
                              params={"id": "eq.%s" % _SCRAPE_STATUS_KEY, "select": "data", "limit": 1},
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
        d = _load_json(SCRAPE_STATUS_FILE)
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
            n = len(load_jobs())
            print("Storage backend: Supabase (connected OK)")
            print("Jobs in table:", n)
        except Exception as e:
            print("Storage backend: Supabase configured, but a request FAILED:")
            print("   ", repr(e))
            print("Check: did you run the CREATE TABLE sql, and are the URL + key correct?")
            sys.exit(1)
