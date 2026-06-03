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
        if not (url and key):                       # local secrets file (plain runs)
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


def _upsert(rows):
    """Insert/merge rows on the `url` primary key (PostgREST upsert).
    PostgREST needs every object in a bulk write to share the SAME keys, so we
    normalize to the union of keys (missing -> None)."""
    if not rows:
        return
    keys = sorted({k for r in rows for k in r})
    body = [{k: r.get(k) for k in keys} for r in rows]
    resp = requests.post(
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
        r = requests.get(_rest(TABLE), headers=_headers(),
                         params={"select": "*"}, timeout=30)
        r.raise_for_status()
        return r.json()
    rows = _read_csv()
    actions = _load_actions()
    for r in rows:                       # fold like/hide/applied in for the app
        r["status"] = actions.get(r["url"], r.get("status", ""))
    return rows


def existing_urls():
    if using_supabase():
        r = requests.get(_rest(TABLE), headers=_headers(),
                         params={"select": "url"}, timeout=30)
        r.raise_for_status()
        return {row["url"] for row in r.json() if row.get("url")}
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
        r = requests.patch(
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
        r = requests.get(_rest(TABLE), headers=_headers(),
                         params={"select": "url,status"}, timeout=30)
        r.raise_for_status()
        return {row["url"]: row["status"] for row in r.json() if row.get("status")}
    return _load_actions()


def delete_urls(urls):
    """Remove jobs by url (used when tightening the filter). Works on both backends."""
    urls = list(urls)
    if not urls:
        return
    if using_supabase():
        for u in urls:
            resp = requests.delete(
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
        resp = requests.delete(_rest(TABLE), headers=_headers({"Prefer": "return=minimal"}),
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
        resp = requests.post(
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
        r = requests.get(_rest(USERS_TABLE), headers=_headers(),
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
        r = requests.get(_rest(USERS_TABLE), headers=_headers(),
                         params={"select": "username,created_at", "order": "created_at"}, timeout=30)
        r.raise_for_status()
        return r.json()
    users = _load_json(USERS_FILE)
    return [{"username": k, "created_at": v.get("created_at", "")} for k, v in users.items()]


def _patch_user(username, fields):
    if using_supabase():
        resp = requests.patch(
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
            resp = requests.delete(_rest(table), headers=_headers({"Prefer": "return=minimal"}),
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
        r = requests.get(_rest(USERJOBS_TABLE), headers=_headers(),
                         params={"username": "eq.%s" % username, "select": "url,status"}, timeout=30)
        r.raise_for_status()
        return {row["url"]: row["status"] for row in r.json() if row.get("status")}
    return _load_json(USER_JOBS_FILE).get(username, {})


def set_user_status(username, url, status):
    """status: 'liked' | 'hidden' | 'applied' | '' to clear — scoped to one user."""
    if using_supabase():
        if status:
            resp = requests.post(
                _rest(USERJOBS_TABLE),
                headers=_headers({"Prefer": "resolution=merge-duplicates,return=minimal"}),
                params={"on_conflict": "username,url"},
                data=json.dumps({"username": username, "url": url, "status": status}), timeout=30)
        else:
            resp = requests.delete(
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
    """{url: jd_text} -> persist each job's description (used for per-user scoring)."""
    rows = [{"url": u, "jd": jd} for u, jd in jds.items() if u]
    if not rows:
        return
    if using_supabase():
        _upsert(rows)            # merges on the `url` primary key
        return
    _dump_json(JDS_FILE, jds)


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
