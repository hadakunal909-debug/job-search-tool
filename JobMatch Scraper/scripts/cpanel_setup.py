#!/usr/bin/env python3
"""Provision the PostgreSQL side of a cPanel account over cPanel's UAPI.

Creates the database, creates the user, grants it, and (optionally) uploads the two local cache
files the migration loads from. Everything the cPanel dashboard would do by hand, minus the
clicking.

THE TOKEN IS READ FROM A FILE AND NEVER PRINTED. Put it in `.cpanel_token` in the app directory
(gitignored) as a single line. It is not accepted as an argument and not read from the shell
history, because both of those leave it somewhere it outlives its usefulness — an argument shows
up in `ps` and in your shell history file, and a token in either is a token to rotate.

    echo "YOUR_TOKEN_HERE" > .cpanel_token

Then:

    python scripts/cpanel_setup.py --host stemjobs1.astrochakra.co --user bnrqpozr
    python scripts/cpanel_setup.py --host ... --user ... --apply
    python scripts/cpanel_setup.py --host ... --user ... --apply --upload

Dry run unless --apply. The database password is GENERATED here and written to `.pg_dsn` (also
gitignored) as a ready-to-use connection string, so it never passes through a terminal either.

cPanel's UAPI function names for PostgreSQL have moved between major versions. Rather than
guessing, every call surfaces cPanel's own error text verbatim -- if your host is on a version
that spells something differently, the message will say so and the fix is a one-line change here
rather than a debugging session.
"""
import argparse
import json
import os
import secrets
import string
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TOKEN_FILE = ".cpanel_token"
DSN_FILE = ".pg_dsn"
UPLOADS = ["jobs_snapshot.json.gz", "jd_cache.json.gz"]


def token():
    """The API token, from disk. Fails loudly rather than falling back to an env var or a
    prompt: a token that can arrive by three routes is a token nobody can tell you the
    provenance of."""
    try:
        t = open(TOKEN_FILE, encoding="utf-8").read().strip()
    except Exception:
        sys.exit("No %s. Put the cPanel API token in that file (one line) and re-run.\n"
                 "  cPanel -> Security -> Manage API Tokens -> Create." % TOKEN_FILE)
    if not t:
        sys.exit("%s is empty." % TOKEN_FILE)
    return t


def uapi(host, user, module, func, params=None, port=2083):
    """One UAPI call. Returns (ok, data, error_text).

    UAPI answers 200 with {"status": 0, "errors": [...]} for a refused operation, so the HTTP
    code alone says nothing -- the status field is the one that matters, and both are checked.
    """
    import requests
    url = "https://%s:%d/execute/%s/%s" % (host, port, module, func)
    try:
        r = requests.get(url, headers={"Authorization": "cpanel %s:%s" % (user, token())},
                         params=params or {}, timeout=45)
    except Exception as e:
        return False, None, "connection failed: %s" % str(e)[:150]
    if r.status_code >= 400:
        return False, None, "HTTP %s %s" % (r.status_code, (r.text or "")[:200])
    try:
        body = r.json()
    except Exception:
        # A login page instead of JSON is what a bad token looks like from here.
        return False, None, "non-JSON reply (bad token, or the host is on a different port)"
    if not body.get("status"):
        errs = body.get("errors") or [body.get("error") or "refused, no reason given"]
        return False, None, "; ".join(str(e)[:200] for e in errs)
    return True, body.get("data"), ""


def gen_password(n=28):
    """URL-safe by construction: the password goes into a DSN, and a '@' or '/' in it silently
    truncates the host when something later parses that string."""
    alphabet = string.ascii_letters + string.digits + "-._~"
    return "".join(secrets.choice(alphabet) for _ in range(n))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="cPanel hostname (no https://)")
    ap.add_argument("--user", required=True, help="cPanel account username")
    ap.add_argument("--db", default="jobmatch", help="database name, unprefixed")
    ap.add_argument("--dbuser", default="jobapp", help="database user, unprefixed")
    ap.add_argument("--port", type=int, default=2083)
    ap.add_argument("--upload", action="store_true", help="also upload the two cache files")
    ap.add_argument("--apply", action="store_true", help="actually make changes")
    a = ap.parse_args()

    # cPanel prefixes both names with the account. Sending the prefixed form is accepted and is
    # what every later connection string must use, so build it once here.
    dbname = "%s_%s" % (a.user, a.db)
    dbuser = "%s_%s" % (a.user, a.dbuser)

    print("=" * 74)
    print("cPanel PostgreSQL provisioning%s" % ("" if a.apply else "   [DRY RUN]"))
    print("=" * 74)
    print("  host     : %s:%d" % (a.host, a.port))
    print("  account  : %s" % a.user)
    print("  database : %s" % dbname)
    print("  db user  : %s" % dbuser)
    print("  token    : read from %s (%d bytes, not shown)" % (TOKEN_FILE, len(token())))

    # Cheapest possible call that proves the token AND that PostgreSQL is switched on for this
    # account -- do it before anything is created, so a wrong token fails having changed nothing.
    ok, data, err = uapi(a.host, a.user, "Postgresql", "list_databases", port=a.port)
    if not ok:
        print("\n  PREFLIGHT FAILED: %s" % err)
        print("  If this says the feature is disabled, PostgreSQL is not enabled for the\n"
              "  account and nothing below can work until the host turns it on.")
        return 1
    existing = [d.get("database") if isinstance(d, dict) else d for d in (data or [])]
    print("  existing databases: %s" % (", ".join(map(str, existing)) or "(none)"))
    if dbname in existing:
        print("\n  %s already exists — nothing to create. Re-run the migrator instead." % dbname)
        return 0

    if not a.apply:
        print("\n  Would create the database, create the user, grant it,%s and write %s."
              % (" upload %d files," % len(UPLOADS) if a.upload else "", DSN_FILE))
        print("  Re-run with --apply.")
        return 0

    pw = gen_password()
    steps = [("create the database", "Postgresql", "create_database", {"name": dbname}),
             ("create the user", "Postgresql", "create_user",
              {"name": dbuser, "password": pw}),
             ("grant it on the database", "Postgresql", "grant_all_privileges",
              {"user": dbuser, "database": dbname})]
    for label, mod, fn, params in steps:
        ok, _, err = uapi(a.host, a.user, mod, fn, params, port=a.port)
        print("  %-28s %s" % (label, "ok" if ok else "FAILED: %s" % err))
        if not ok:
            print("\n  Stopped. Nothing after this point ran; re-run once the cause is fixed\n"
                  "  (the steps are idempotent enough that an already-created database is\n"
                  "  reported above rather than duplicated).")
            return 1

    # key=value ("conninfo") form, NOT a postgresql:// URI. Both psycopg and psql accept either,
    # but the URI has a grammar and this password goes in the middle of it: a generated password
    # produced `FATAL: no pg_hba.conf entry` from libpq while the very same credentials connected
    # fine through -U/-d flags, and a deliberately wrong password through the same URI produced a
    # normal auth failure. So libpq was mis-reading the URI on the password's characters, and the
    # error it chose to report pointed at pg_hba -- which sent us looking for a missing server
    # rule that was never missing. conninfo has no userinfo section, so no character in a password
    # can change how the host or database is read.
    #
    # 127.0.0.1 rather than localhost: localhost resolves to ::1 first, and cPanel's pg_hba has no
    # IPv6 rule -- that one IS a genuine "no entry".
    #
    # Either way it stays local. The app runs on this machine, so the database never has to accept
    # a connection from the internet; that is the whole security argument for putting it here, and
    # naming the public host would throw it away in one line.
    dsn = "host=127.0.0.1 port=5432 dbname=%s user=%s password=%s" % (dbname, dbuser, pw)
    with open(DSN_FILE, "w", encoding="utf-8") as fh:
        fh.write(dsn + "\n")
    print("\n  Wrote %s — the generated password lives there and nowhere else." % DSN_FILE)

    if a.upload:
        import requests
        for name in UPLOADS:
            if not os.path.exists(name):
                print("  %-28s missing locally, skipped" % name)
                continue
            try:
                with open(name, "rb") as fh:
                    r = requests.post(
                        "https://%s:%d/execute/Fileman/upload_files" % (a.host, a.port),
                        headers={"Authorization": "cpanel %s:%s" % (a.user, token())},
                        data={"dir": "/home/%s" % a.user}, files={"file-1": (name, fh)},
                        timeout=600)
                body = r.json() if r.status_code < 400 else {}
                ok = bool(body.get("status"))
            except Exception as e:
                ok, body = False, {"errors": [str(e)[:150]]}
            print("  upload %-21s %s" % (name, "ok" if ok else
                                         "FAILED: %s" % (body.get("errors") or "?")))

    print("\n  NEXT, on the cPanel box:")
    print("    1. load the schema:  psql \"$(cat %s)\" -f schema.sql" % DSN_FILE)
    print("    2. load the data  :  TARGET_PG_DSN=\"$(cat %s)\" \\" % DSN_FILE)
    print("                         python scripts/migrate_project.py --jobs --apply")
    print("    3. point the app  :  PG_DSN from %s, then restart Passenger" % DSN_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
