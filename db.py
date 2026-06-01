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
