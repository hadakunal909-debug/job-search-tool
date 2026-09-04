#!/usr/bin/env python3
"""Which database db.py talks to, for every combination of the environment.

THE MOST CONSEQUENTIAL BRANCH IN THE CODEBASE now that there are three transports. It decides
where every read and every write goes, it is chosen once per process from environment variables
set in three different places (cPanel's .env, GitHub Actions secrets, a developer's shell), and
getting it wrong does not raise — it silently sends the scraper's writes to a database nobody is
reading, which is the exact failure the whole migration is sequenced to avoid.

Subprocesses rather than reload(), because db.PG_DSN is read at import and a transport is cached
in a module global on first use. A test that reloaded the module would be testing something the
real processes never do.

    python scripts/test_transport.py
"""
import io
import os
import subprocess
import sys
import tempfile

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROBE = ("import db;"
         "print(type(db._http.__getattr__('get').__self__).__module__, db.has_remote_db())")
# A SECOND probe that does NOT touch _http. Needed since 2026-09-01: with no backend configured
# there is no session to resolve and touching _http raises, so PROBE reports ("?", False) for
# BOTH "nothing configured" and "the process crashed". This one answers the narrower question --
# does db think a remote exists, and what does it name -- without triggering that.
FLAG = "import db; print(db.has_remote_db(), '|', db.backend_name())"

fails, ran = [], []


def transport(_cwd=None, **env):
    """The transport a fresh process picks under `env`.

    `_cwd` matters more than it looks. db._creds() falls back to reading .env and
    .streamlit/secrets.toml RELATIVE TO THE WORKING DIRECTORY, so a subprocess started in the
    app directory finds this machine's real credentials no matter what the environment says.
    The "no credentials at all" case therefore has to run somewhere else, with PYTHONPATH
    pointing back here so `import db` still works — otherwise it silently tests the developer's
    laptop instead of the matrix, which is exactly how it first passed while asserting nothing.
    """
    e = dict(os.environ)
    # Cleared explicitly: a developer running this with a real .env or a PG_DSN exported in
    # their shell would otherwise test their machine's configuration rather than the matrix.
    for k in ("PG_DSN", "DB_PROXY_URL", "DB_PROXY_SECRET", "SUPABASE_URL", "SUPABASE_KEY"):
        e.pop(k, None)
    e.update({k: v for k, v in env.items() if v is not None})
    e["SUPABASE_URL"] = e.get("SUPABASE_URL", "https://example.supabase.co")
    e["SUPABASE_KEY"] = e.get("SUPABASE_KEY", "anon-key")
    if _cwd:
        e["PYTHONPATH"] = APP + os.pathsep + e.get("PYTHONPATH", "")
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=_cwd or APP, env=e,
                         capture_output=True, text=True).stdout.strip().split()
    return (out[0], out[1] == "True") if len(out) == 2 else ("?", False)


def check(label, got, want):
    ran.append(label)
    ok = got == want
    print("  %s  %-34s -> %s" % ("ok " if ok else "FAIL", label, got[0]))
    if not ok:
        fails.append(label)
        print("        wanted %s, got %s" % (want, got))


print("=" * 74)
print("TRANSPORT SELECTION")
print("=" * 74)

# STALE SUPABASE CREDENTIALS ARE INERT NOW, and this is the case that proves the removal.
# transport() deliberately injects SUPABASE_URL and SUPABASE_KEY (see the helper), which is what
# a real developer box still has sitting in .streamlit/secrets.toml. Until 2026-09-01 that alone
# selected a live Supabase session and this line asserted ("requests.sessions", True). There is
# no third transport to select any more, so the probe finds no backend and raises.
check("stale SUPABASE_* creds select nothing",
      transport(), ("?", False))

# A half-configured proxy must NOT half-switch — and as of 2026-08-19 it must not fall through to
# Supabase either. THIS EXPECTATION IS REVERSED FROM WHAT IT WAS, deliberately, and the reason is
# worth keeping because the original was right when it was written.
#
# It used to assert ("requests.sessions", True), i.e. Supabase, on the argument that GitHub renders
# a nonexistent secret as an empty string, so this is exactly what CI sees between adding one
# secret and the other — and breaking CI for that window was not worth it. True while Supabase was
# still the live database.
#
# The migration completed on 2026-08-15. From that day the identical fallback means "write a full
# 26-minute sweep into a database the app no longer reads, and report success", which is strictly
# worse than one failed run: the sweep is stateless and the next slot re-scrapes from the boards.
# db._check_backend_intent now raises, so the probe produces no output and transport() reports
# ("?", False).
HALF_SET = ("?", False)
check("proxy url but no secret refuses",
      transport(DB_PROXY_URL="https://x/api/db"), HALF_SET)
check("proxy secret but no url refuses",
      transport(DB_PROXY_SECRET="s"), HALF_SET)


def refusal_reason(**env):
    """Why the probe produced nothing. ("?", False) alone would pass for ANY crash — an import
    error, a syntax error, a missing module — so the message has to be read. A test that goes
    green for the wrong reason is worse than no test."""
    e = dict(os.environ)
    for k in ("PG_DSN", "DB_PROXY_URL", "DB_PROXY_SECRET", "SUPABASE_URL", "SUPABASE_KEY"):
        e.pop(k, None)
    e.update({k: v for k, v in env.items() if v is not None})
    e["SUPABASE_URL"], e["SUPABASE_KEY"] = "https://example.supabase.co", "anon-key"
    p = subprocess.run([sys.executable, "-c", PROBE], cwd=APP, env=e,
                       capture_output=True, text=True)
    return (p.stderr or "").strip()


for label, kw, missing in (("url only", {"DB_PROXY_URL": "https://x/api/db"}, "DB_PROXY_SECRET"),
                           ("secret only", {"DB_PROXY_SECRET": "s"}, "DB_PROXY_URL")):
    why = refusal_reason(**kw)
    # It must still refuse for the RIGHT reason, and the reason changed with the transport: the
    # message used to warn about falling through to Supabase and now names the variable that is
    # missing. Asserting on the variable name is also the stronger test -- it is the one thing
    # the reader needs and the one thing a reworded message must keep.
    check("%s refuses for the RIGHT reason" % label,
          ("half-configured" in why and missing in why, True), (True, True))
check("empty strings are not configuration",
      transport(DB_PROXY_URL="", DB_PROXY_SECRET="", PG_DSN=""), ("?", False))

check("both proxy vars: the scraper's path",
      transport(DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET="s"), ("dbproxy", True))

check("PG_DSN: the cPanel app's path",
      transport(PG_DSN="host=127.0.0.1 dbname=d"), ("pgrest", True))

# A process that can reach the database directly must never route over HTTP to reach itself --
# which is what the cPanel app would do to serve its own pages if the proxy won.
check("both set: PG_DSN wins",
      transport(PG_DSN="host=127.0.0.1 dbname=d",
                DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET="s"), ("pgrest", True))

# has_remote_db() is the "is there a remote database at all" gate, and every db function uses
# it to choose between the network and the local CSV. A transport that is configured but reports
# False would silently drop the whole application onto jobs.csv.
def remote_flag(_cwd=None, **env):
    """(has_remote_db, backend_name) without resolving a transport."""
    e = dict(os.environ)
    for k in ("PG_DSN", "DB_PROXY_URL", "DB_PROXY_SECRET", "SUPABASE_URL", "SUPABASE_KEY"):
        e.pop(k, None)
    e.update({k: v for k, v in env.items() if v is not None})
    if _cwd:
        e["PYTHONPATH"] = APP + os.pathsep + e.get("PYTHONPATH", "")
    out = subprocess.run([sys.executable, "-c", FLAG], cwd=_cwd or APP, env=e,
                         capture_output=True, text=True).stdout.strip()
    return out


with tempfile.TemporaryDirectory() as empty:
    # Asserted through FLAG rather than PROBE: with nothing configured there is no session to
    # resolve, so PROBE's ("?", False) cannot tell "fell back to the CSV" from "crashed".
    check("no credentials of any kind: the CSV fallback",
          (remote_flag(_cwd=empty, SUPABASE_URL="", SUPABASE_KEY=""), True),
          ("False | jobs.csv", True))
    check("...and SUPABASE_* present changes nothing",
          (remote_flag(_cwd=empty, SUPABASE_URL="https://example.supabase.co",
                       SUPABASE_KEY="anon-key"), True),
          ("False | jobs.csv", True))
    check("...and a PG_DSN still wins there",
          transport(_cwd=empty, SUPABASE_URL="", SUPABASE_KEY="",
                    PG_DSN="host=127.0.0.1 dbname=d"), ("pgrest", True))

# CONFIGURED VIA .env, NOT THE ENVIRONMENT. cPanel has no place to set process environment
# variables for a Passenger app, so every setting arrives through the app directory's .env --
# and PG_DSN was originally a module constant read ~45 lines BEFORE db.py loads that file. The
# app therefore ignored its own configuration and kept talking to Supabase, silently, with
# matching row counts because the two databases are identical copies. This is the regression
# test for that: same value, delivered the way the live app actually delivers it.
def write_env(directory, text):
    with io.open(os.path.join(directory, ".env"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


with tempfile.TemporaryDirectory() as d:
    write_env(d, "PG_DSN=host=127.0.0.1 dbname=d user=u password=p\n")
    check("PG_DSN from a .env file is honoured",
          transport(_cwd=d, SUPABASE_URL="", SUPABASE_KEY=""), ("pgrest", True))
    write_env(d, "DB_PROXY_URL=https://x/api/db\nDB_PROXY_SECRET=s\n")
    check("DB_PROXY_* from a .env file are honoured",
          transport(_cwd=d, SUPABASE_URL="", SUPABASE_KEY=""), ("dbproxy", True))

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all good - %d checks" % len(ran))
