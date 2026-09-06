"""dbproxy.py — the scraper's transport, when the database is not reachable from GitHub.

THE PROBLEM. The database now lives on cPanel and listens on 127.0.0.1, which is the entire
security argument for putting it there. But `scrape.yml` runs on GitHub's runners twice a
weekday and writes to that database through five steps. Their IPs are dynamic and span enormous
ranges, so the alternatives are: expose Postgres to the internet behind an allowlist of
"everything", on a database holding password hashes, resumes and application history — or route
the writes through something that is already exposed and already authenticated. This is that.

    scraper (GitHub Actions)
        db.py  ->  dbproxy.Session  --HTTPS-->  POST /api/db  (Flask, on cPanel)
                                                     `-> db._http (pgrest.Session) -> Postgres

It is the THIRD implementation of the same five verbs, after requests->PostgREST and
pgrest->psycopg, which is why nothing above db.py changes for this either.

WHY A GENERAL PROXY RATHER THAN A NARROW "INGEST JOBS" ENDPOINT. Measured, not assumed: the
scraper calls 29 distinct db functions, including db._rest / db._headers / db._fetch_all
directly. It reads the corpus, patches scores and JDs, deletes stale rows, reads and writes
boards, reads profiles and users. A narrow endpoint would mean rewriting all of that; this
needs no scraper changes at all.

WHAT STOPS IT BEING A REMOTE SQL CONSOLE. That is the fair objection to a proxy, and the
answer is that the envelope carries no SQL and cannot be made to:

  * The wire format is (method, table, params, prefer, body). There is no field a statement
    could travel in. The server rebuilds the SQL itself through pgrest.build, which already
    refuses unknown operators, rejects identifiers that are not identifiers, and refuses an
    unfiltered DELETE outright.
  * TABLES ARE ALLOWLISTED to this app's own fourteen. Not pg_catalog, not another tenant's.
  * EVERY REQUEST IS HMAC-SIGNED over its exact bytes plus a timestamp. A bearer token in a
    header would be replayable forever by anything that saw one request; a signature over the
    body means a captured request cannot be edited, and the timestamp window means it cannot be
    replayed tomorrow. The secret never travels — only the signature does.
  * Signature comparison is constant-time, and an invalid signature is answered before the
    body is parsed, so a wrong secret cannot reach any of the logic below it.

Configuration, all by environment:
    server   DB_PROXY_SECRET   enables POST /api/db
    scraper  DB_PROXY_URL      https://stemjobs1.astrochakra.co/api/db
             DB_PROXY_SECRET   the same value, from GitHub Actions secrets
"""
import hashlib
import hmac
import json
import os
import time

import pgrest

# The app's own tables. Anything else is refused before a statement is built — a proxy whose
# table name is whatever the caller sent is a proxy that can read pg_shadow.
ALLOWED_TABLES = {
    "jobs", "users", "profiles", "user_jobs", "applications", "resumes", "boards",
    "blocked_companies", "brain_companies", "learned_answers", "scrape_status",
    "admin_audit", "events", "events_daily", "tailored_cache",
    # The stored per-(user, job) match score. Dense -- users x jobs -- so it is the one table
    # here a caller might legitimately read 47,845 rows out of in a single request.
    "user_scores",
    # Listed for completeness so an admin probe can COUNT it. Nothing should ever route a
    # base64 PDF through this transport: /api/db is an ordinary Flask route, so the real cap
    # is MAX_CONTENT_LENGTH (6 MB), not MAX_BODY below, and it is untested above a few KB.
    "resume_files",
    # Employer-level facts, and the version stamp the row cache keys on. See
    # MIGRATION_companies.sql. Listing them here is not optional and the failure is
    # asymmetric: the cPanel app talks to Postgres directly and works without it, so a run
    # from a laptop or from Actions is the ONLY place a missing entry shows up -- as a 403
    # that looks like an auth problem rather than an allowlist one.
    "companies", "data_versions",
}
# Stored procedures the admin panels call. Named individually for the same reason as the tables.
ALLOWED_RPC = {"db_stats", "ev_usage"}

MAX_SKEW = 300          # seconds a signed request stays valid; bounds replay
MAX_BODY = 16 * 1024 * 1024


def sign(secret, ts, raw):
    """Hex HMAC-SHA256 over the timestamp and the exact request bytes.

    The timestamp is inside the signed message, not merely alongside it — signing only the body
    would let anyone who captured a request move its clock forward and replay it indefinitely.
    """
    msg = ("%s." % ts).encode("utf-8") + (raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def verify(secret, ts, raw, sig, now=None):
    """(ok, reason). Signature first, then freshness: a valid-looking timestamp on an unsigned
    request should never get as far as being interesting."""
    if not (secret and sig and ts):
        return False, "missing signature"
    if not hmac.compare_digest(sign(secret, ts, raw), str(sig)):
        return False, "bad signature"
    try:
        skew = abs((now if now is not None else time.time()) - float(ts))
    except (TypeError, ValueError):
        return False, "bad timestamp"
    if skew > MAX_SKEW:
        return False, "stale request (%.0fs skew)" % skew
    return True, ""


def _check_target(table):
    """The table or rpc this envelope names, or a reason to refuse it."""
    if table.startswith("rpc/"):
        return (None if table[4:] in ALLOWED_RPC else "rpc not allowed: %s" % table[4:])
    return None if table in ALLOWED_TABLES else "table not allowed: %s" % table


def handle(raw, ts, sig, secret, backend, now=None, local_ok=True):
    """Server side. Returns (http_status, response_dict).

    `backend` is anything with the five verbs — on the cPanel app that is db._http, which is a
    pgrest.Session, so the request lands on the local Postgres by exactly the same path a
    request from the web app itself would take. Kept free of Flask so it can be tested without
    one.
    """
    if not secret:
        return 503, {"error": "proxy not configured"}
    if raw is None or len(raw) > MAX_BODY:
        return 413, {"error": "body too large"}
    ok, why = verify(secret, ts, raw, sig, now)
    if not ok:
        return 401, {"error": why}
    # AFTER the signature, deliberately. `local_ok` is false when this app has no database of
    # its own (no PG_DSN), and proxying would then mean a caller asking us to call Supabase for
    # them — slower than the direct call they already have, and a write path nobody intended.
    # Checking it BEFORE the signature, as the first version did, told every anonymous caller
    # what this server is configured with. Configuration state is for authenticated callers.
    if not local_ok:
        return 503, {"error": "proxy has no local database (PG_DSN unset)"}
    try:
        env = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return 400, {"error": "malformed envelope"}

    method = str(env.get("method") or "").upper()
    table = str(env.get("table") or "")
    if method not in ("GET", "POST", "PATCH", "DELETE", "HEAD"):
        return 400, {"error": "bad method"}
    why = _check_target(table)
    if why:
        return 403, {"error": why}

    params = env.get("params") or {}
    prefer = str(env.get("prefer") or "")
    body = env.get("body")
    if not isinstance(params, dict):
        return 400, {"error": "params must be an object"}

    # VALIDATE HERE, not by hoping the backend does it. Every refusal described at the top of
    # this file -- unknown operators, identifiers that are not identifiers, an unfiltered DELETE
    # -- lives in pgrest.build, and until this check existed they only happened because the
    # backend on this machine happens to BE a pgrest.Session. That made the endpoint's safety a
    # property of the wiring rather than of the endpoint, and a test with a plain backend proved
    # it by getting an unfiltered DELETE and `select=url; drop table jobs` straight through.
    # Building it twice costs a few microseconds of string work and is worth it.
    if not table.startswith("rpc/"):
        try:
            pgrest.build(method, table, params, body, prefer)
        except pgrest.PgRestError as e:
            return 400, {"error": "refused: %s" % str(e)[:300]}

    url = "proxy://local/rest/v1/%s" % table
    kw = {"headers": {"Prefer": prefer}, "params": params, "timeout": 120}
    if body is not None:
        kw["data"] = json.dumps(body).encode("utf-8")
    try:
        r = getattr(backend, method.lower())(url, **kw)
    except pgrest.PgRestError as e:
        # The translator refused it. That is a rejection, not a server fault, and the caller
        # should see which one — these are the guards described at the top of this file.
        return 400, {"error": "refused: %s" % str(e)[:300]}
    except Exception as e:
        return 502, {"error": "backend failed: %s" % str(e)[:300]}

    rows = None
    try:
        rows = r.json()
    except Exception:
        rows = None
    return 200, {"status": r.status_code, "rows": rows,
                 "headers": {"Content-Range": (r.headers or {}).get("Content-Range", "")}}


def _repeatable(method, prefer):
    """May this request be sent again when we never learned whether the first one landed?

    Retrying is only worth having if it cannot double-write, so this is decided from the
    request itself rather than assumed:

      * GET and HEAD change nothing.
      * PATCH and DELETE always carry their filter in `params` -- pgrest.build refuses an
        unfiltered DELETE outright -- so applying one twice writes the same values to the same
        rows and deletes an already-deleted row.
      * POST is safe only as an UPSERT. db.py sends `resolution=merge-duplicates` on every
        insert that can collide, so a replay merges. The one bare POST in the codebase is the
        admin_audit append, where a replay would genuinely be a second audit row -- so a bare
        POST is sent exactly once and a blip during it fails loudly, which is the right
        outcome for an audit trail.
    """
    if method in ("GET", "HEAD", "PATCH", "DELETE"):
        return True
    return method == "POST" and "merge-duplicates" in (prefer or "")


class Session:
    """Client side: the five verbs, over HTTPS, for db._http to use."""

    def __init__(self, url, secret):
        self.url = url
        self.secret = secret
        self._http = None

    def _session(self):
        """A session that retries CONNECTIONS but never STATUSES.

        Deliberately not db._make_http(), whose policy retries 429/500/502/503/504 — right for
        Supabase, wrong here. This endpoint answers 4xx and 5xx to say things: 401 bad
        signature, 403 table not allowed, 503 no local database. Retrying those three times and
        then raising RetryError replaces the server's explanation with "too many 502 error
        responses", which is exactly how the first live test wasted a debugging round: the
        server was reporting the real problem the whole time and the client was eating it.
        """
        if self._http is None:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
            s = requests.Session()
            s.mount("https://", HTTPAdapter(max_retries=Retry(
                total=2, connect=2, read=2, status=0, backoff_factor=0.5)))
            s.mount("http://", HTTPAdapter(max_retries=Retry(
                total=2, connect=2, read=2, status=0, backoff_factor=0.5)))
            self._http = s
        return self._http

    # WHAT IS WORTH A SECOND ATTEMPT -- and what deliberately still is not.
    #
    # _session() above refuses to retry statuses, and that reasoning holds for the proxy's OWN
    # vocabulary: 401 bad signature, 403 table not allowed, 400 malformed. Retrying an
    # explanation three times replaces it with a timeout, which is how the first live test
    # wasted a debugging round.
    #
    # It does NOT hold for the hop in front of the proxy. /api/db is a Flask route on shared
    # cPanel hosting behind LiteSpeed, and when that front end hiccups the request never
    # reaches the application at all: it comes back 502/503/504, or -- the case that actually
    # took the 2026-09-03 scrape down -- 200 carrying an HTML error page. r.json() was called
    # on whatever arrived, so a few seconds of web-server trouble raised JSONDecodeError out
    # of db.load_jobs() and killed the run on its FIRST query, twelve seconds after the
    # preflight step had proved the database was reachable. Three steps died that way inside
    # one minute and the day's scrape banked nothing.
    #
    # A genuine "503 no local database" from the proxy itself gets retried too. That is the
    # accepted cost: three attempts, ~3s, and the caller still ends up with the server's own
    # message. Losing a day's scrape to a blip is not a trade worth making to save it.
    _RETRY_STATUSES = frozenset((502, 503, 504))
    _ATTEMPTS = 3
    _BACKOFF = 1.0

    def _run(self, method, url, headers=None, params=None, data=None, timeout=None):
        table = str(url).rsplit("/rest/v1/", 1)[-1].split("?")[0]
        body = None
        if data is not None:
            body = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        prefer = (headers or {}).get("Prefer") or ""
        raw = json.dumps({"method": method, "table": table,
                          "params": params or {},
                          "prefer": prefer,
                          "body": body}, separators=(",", ":")).encode("utf-8")
        attempts = self._ATTEMPTS if _repeatable(method, prefer) else 1
        for i in range(attempts):
            if i:
                time.sleep(self._BACKOFF * (2 ** (i - 1)))
            # ts and the signature are recomputed per attempt on purpose: the server rejects a
            # stale X-DB-Ts, so reusing the first attempt's clock would turn a backoff into a
            # 401 -- a retry that reports the wrong cause is worse than no retry.
            ts = "%d" % int(time.time())
            try:
                r = self._session().post(
                    self.url, data=raw, timeout=timeout or 120,
                    headers={"Content-Type": "application/json",
                             "X-DB-Ts": ts, "X-DB-Sig": sign(self.secret, ts, raw)})
            except Exception:
                # urllib3 already retried the CONNECTION twice inside that call, so arriving
                # here means the host is down or the read timed out. Re-raise on the last
                # attempt rather than inventing a Response: callers have always seen the
                # transport exception for this case and several catch it by type.
                if i == attempts - 1:
                    raise
                continue
            if r.status_code >= 400:
                # Surface the proxy's own reason; db.py's callers already read .text on failure.
                resp = pgrest.Response(r.status_code, {"message": (r.text or "")[:300]})
                if r.status_code in self._RETRY_STATUSES and i < attempts - 1:
                    continue
                return resp
            try:
                payload = r.json()
            except ValueError:
                # 200 with a body that is not JSON. Answer in the same shape the >=400 branch
                # above uses so raise_for_status() reports "HTTP 502: <the HTML>" instead of a
                # JSONDecodeError thrown from inside requests, which named neither the table
                # nor the transport and read like a bug in our own parsing.
                if i < attempts - 1:
                    continue
                return pgrest.Response(502, {"message": "proxy returned non-JSON: %s"
                                                        % (r.text or "")[:300]})
            if not isinstance(payload, dict):
                # Valid JSON, wrong envelope -- an interstitial that answers `[]` or `"ok"`.
                # .get() on it would AttributeError two frames from here.
                if i < attempts - 1:
                    continue
                return pgrest.Response(502, {"message": "proxy returned a %s, not an envelope"
                                                        % type(payload).__name__})
            return pgrest.Response(payload.get("status", 200), payload.get("rows"),
                                   payload.get("headers") or {})

    def get(self, url, **kw):
        return self._run("GET", url, **kw)

    def post(self, url, **kw):
        return self._run("POST", url, **kw)

    def patch(self, url, **kw):
        return self._run("PATCH", url, **kw)

    def delete(self, url, **kw):
        return self._run("DELETE", url, **kw)

    def head(self, url, **kw):
        return self._run("HEAD", url, **kw)


def client_from_env():
    """A Session if both DB_PROXY_* are set, else None."""
    url = os.environ.get("DB_PROXY_URL") or ""
    secret = os.environ.get("DB_PROXY_SECRET") or ""
    return Session(url, secret) if (url and secret) else None
