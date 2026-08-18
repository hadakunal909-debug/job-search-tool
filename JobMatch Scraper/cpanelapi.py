"""cpanelapi.py — the read-only half of cPanel's UAPI, for anything that needs to know what
this account is actually using.

WHY THIS EXISTS. The admin panel spent its whole life reporting the database against Supabase's
500 MB free-tier cap. That number stopped meaning anything on 2026-08-15 when the database moved
to Postgres on cPanel, and it was actively misleading: measured through this module on
2026-08-18, the account's disk quota is UNLIMITED (`megabyte_limit: "0.00"`, which is how cPanel
spells "no limit") while `astrocha_jobmatch` was 123.59 MB. A panel warning about 25% of a cap
that does not exist is worse than a panel with no number at all.

THE REAL CEILING IS INODES. The same call reports 44,305 of a 200,000 inode limit — 22% — and
nothing in this app had ever looked at it. That is the number that can actually stop this
account working, and unlike disk it is not something a prune of old job rows will help with,
because it counts FILES, not bytes.

CONFIGURATION, all optional. With none of it set every function here returns None and the panel
degrades to the database size it can already measure through the db_stats RPC:

    CPANEL_HOST        stemjobs1.astrochakra.co
    CPANEL_USER        astrocha
    CPANEL_API_TOKEN   a cPanel API token

USE A RESTRICTED TOKEN. cPanel tokens can be scoped per feature, and the one this needs only
has to read: Quota and Postgresql. A full-access token in a web app's environment is a token
that can drop a database if the app is ever compromised, and nothing here needs that power.
"""
import os
import time

DEFAULT_PORT = 2083
TIMEOUT = 20

# UAPI calls this module makes. Named here so the restricted token above can be scoped to
# exactly this list and no more.
READ_ONLY_CALLS = (("Quota", "get_quota_info"), ("Postgresql", "list_databases"))


def uapi(host, user, token, module, func, params=None, port=DEFAULT_PORT, timeout=TIMEOUT):
    """One UAPI call. Returns (ok, data, error_text).

    UAPI answers HTTP 200 with {"status": 0, "errors": [...]} for a refused operation, so the
    HTTP code alone says nothing — the status field is the one that matters, and both are
    checked. Errors are returned rather than raised because every caller here is decorating a
    dashboard, and a dashboard that 500s because a stats call failed is worse than one that
    says it could not read them.
    """
    import requests
    url = "https://%s:%d/execute/%s/%s" % (host, int(port), module, func)
    try:
        r = requests.get(url, headers={"Authorization": "cpanel %s:%s" % (user, token)},
                         params=params or {}, timeout=timeout)
    except Exception as e:
        return False, None, "connection failed: %s" % str(e)[:150]
    if r.status_code >= 400:
        return False, None, "HTTP %s %s" % (r.status_code, (r.text or "")[:160])
    try:
        body = r.json()
    except Exception:
        # A login page instead of JSON is what a bad token looks like from here.
        return False, None, "non-JSON reply (bad token, or a different port)"
    if not body.get("status"):
        errs = body.get("errors") or [body.get("error") or "refused, no reason given"]
        return False, None, "; ".join(str(e)[:160] for e in errs)
    return True, body.get("data"), ""


def creds():
    """(host, user, token) from the environment, or None if it is not configured."""
    host = os.environ.get("CPANEL_HOST") or ""
    user = os.environ.get("CPANEL_USER") or ""
    token = os.environ.get("CPANEL_API_TOKEN") or ""
    return (host, user, token) if (host and user and token) else None


def account_usage(host=None, user=None, token=None, port=DEFAULT_PORT, timeout=TIMEOUT):
    """What this cPanel account is using, normalized. None when not configured.

    Every field can independently be None — one of the two calls failing must not blank the
    other, because "we could not read the inode count" and "the inode count is zero" have to
    look different in a dashboard.

    `timeout` is per call and there are two of them, so a caller rendering a page should pass
    something well under its own patience: the default 20 s would let a slow cPanel stall an
    admin page for 40 s the first time it was loaded after the cache expired.
    """
    if not (host and user and token):
        got = creds()
        if not got:
            return None
        host, user, token = got

    out = {"host": host, "account": user, "at": time.time(), "errors": []}

    ok, q, err = uapi(host, user, token, "Quota", "get_quota_info", port=port, timeout=timeout)
    if ok and isinstance(q, dict):
        # cPanel spells "unlimited" as a 0 limit, in a STRING. Treated as a number it reads as
        # "your limit is zero and you are over it", which is the opposite of the truth.
        mb_limit = _num(q.get("megabyte_limit"))
        inode_limit = _num(q.get("inode_limit"))
        out["disk_mb"] = _num(q.get("megabytes_used"))
        out["disk_limit_mb"] = mb_limit if mb_limit else None       # None == unlimited
        out["inodes"] = _num(q.get("inodes_used"))
        out["inode_limit"] = inode_limit if inode_limit else None
        out["inode_pct"] = (100.0 * out["inodes"] / out["inode_limit"]
                            if (out["inodes"] is not None and out["inode_limit"]) else None)
    else:
        out["errors"].append("quota: %s" % err)

    ok, dbs, err = uapi(host, user, token, "Postgresql", "list_databases", port=port,
                        timeout=timeout)
    if ok and isinstance(dbs, list):
        rows = []
        for d in dbs:
            rows.append({"name": d.get("database") or "",
                         "bytes": int(d.get("disk_usage") or 0),
                         "users": list(d.get("users") or [])})
        rows.sort(key=lambda r: -r["bytes"])
        out["databases"] = rows
        out["db_total_bytes"] = sum(r["bytes"] for r in rows)
    else:
        out["errors"].append("postgresql: %s" % err)

    return out


def _num(v):
    """cPanel returns numbers as strings, and sometimes as "". None rather than 0 on failure —
    a dashboard must be able to render "—" instead of a confident zero."""
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None
