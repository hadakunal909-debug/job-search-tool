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

import requests


def _make_http():
    """Session for all Supabase REST calls: keep-alive pooling + automatic retry with
    backoff on transient failures. Shared-network blips (the recurring WinError 10054
    'connection forcibly closed' during chunked JD upserts) used to abort a whole
    score run; now each request retries itself. Retrying writes is safe here because
    every write is idempotent — upserts keyed on url, patches/deletes on eq filters."""
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


_http = _make_http()


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
          "sponsors_h1b", "match_score", "status"]

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
def load_jobs():
    if using_supabase():
        return _fetch_all(TABLE, {"select": "*"})
    rows = _read_csv()
    actions = _load_actions()
    for r in rows:                       # fold like/hide/applied in for the app
        r["status"] = actions.get(r["url"], r.get("status", ""))
    return rows


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
    rows = [{"url": u, "jd": (jd or "")[:12000]} for u, jd in jds.items() if u]
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
    "alter table public.profiles add column if not exists default_resume text;")


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
PROFILE_FIELDS = ("username", "name", "email", "phone", "location", "linkedin",
                  "work_authorized", "needs_sponsorship", "default_resume", "notes", "updated_at")


def get_profile(username):
    """This user's profile dict (or {} if none / missing table)."""
    if using_supabase():
        try:
            r = _http.get(_rest(PROFILES_TABLE), headers=_headers(),
                             params={"username": "eq.%s" % username, "select": "*", "limit": 1}, timeout=30)
            r.raise_for_status()
            rows = r.json()
            return rows[0] if rows else {}
        except Exception:
            return {}
    data = _load_json(PROFILES_FILE)
    return (data.get(username) or {}) if isinstance(data, dict) else {}


def save_profile(username, fields):
    """Upsert the user's profile (PK=username). Returns (ok, message)."""
    rec = {k: fields.get(k) for k in PROFILE_FIELDS if k in fields}
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
