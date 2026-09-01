"""
test_backend_intent.py — db.py must never silently write to a backend nobody asked for.

No external test deps, no network, no database: run it directly
    python test_backend_intent.py
or via pytest if you have it (functions are named test_*).

WHY THIS FILE EXISTS. db.py falls back PG_DSN -> DB_PROXY_* -> jobs.csv, and every step of that
chain is silent. `dbproxy.client_from_env()` needs BOTH DB_PROXY_ vars and returns None
otherwise, so a missing or typo'd secret is indistinguishable from "no proxy wanted".

There was a fourth step until 2026-09-01: an unauthenticated Supabase session, and it was the
DEFAULT. A half-set pair therefore redirected every write to the database this project moved off
on 2026-08-15 and the run reported success — measured 2026-08-19, DB_PROXY_URL set with
DB_PROXY_SECRET empty answered "Supabase" from backend_name(). That transport is gone, and half
of this file now exists to keep it gone: stale SUPABASE_* credentials must select NOTHING.

That is the same failure family as the PG_DSN-read-before-.env bug db.py documents at module
scope: the configuration said one thing, the process did another, and nothing raised. The cost is
asymmetric and delayed — a failed run costs one cycle, a silently misrouted run costs every row
it wrote plus however long it takes anyone to notice the feed stopped moving.

Each case runs in a SUBPROCESS with a scrubbed environment, because db.py resolves its transport
once per process and caches it in a module global.
"""
import os
import subprocess
import sys

SCRUB = ("DB_PROXY_URL", "DB_PROXY_SECRET", "PG_DSN", "DB_REQUIRE",
         "SUPABASE_URL", "SUPABASE_KEY")
# Credentials shaped like the real thing but pointing nowhere. They used to be here so the
# Supabase branch was REACHABLE; they are here now to prove it is not. Every case below carries
# them, so a re-added fall-through would light this file up rather than pass quietly.
SUPA = {"SUPABASE_URL": "https://legacy.example.supabase.co", "SUPABASE_KEY": "not-a-real-key"}

# Touch _http so the LAZY transport selection actually runs — importing db alone resolves nothing.
PROBE = "import db; db._http.get; print('BACKEND=' + db.backend_name())"
# ...but since 2026-09-01 touching _http with nothing configured RAISES, because there is no
# third transport left to fall through to. That is the intended behaviour and PROBE is still the
# right probe for "which transport did it pick". It is the wrong one for "what does db think it
# has", which is a question about backend_name() alone -- so the cases below that ask THAT use
# this instead. Without the split, "no backend configured" and "the process crashed" look
# identical, and a test that cannot tell those apart will one day pass for the wrong reason.
NAME_PROBE = "import db; print('BACKEND=' + db.backend_name())"


def run(extra, code=PROBE):
    """(stdout, stderr) from a fresh interpreter with `extra` as the only relevant config."""
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env.update(extra)
    env["PYTHONIOENCODING"] = "utf-8"
    # cwd matters: db.py loads .env relative to the working directory, and the repo's own .env
    # would otherwise supply a real PG_DSN and make every case below moot.
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       env=env, cwd=os.path.dirname(os.path.abspath(__file__)))
    return (p.stdout or ""), (p.stderr or "")


def backend(extra, code=PROBE):
    out, err = run(extra, code)
    for line in out.splitlines():
        if line.startswith("BACKEND="):
            return line.split("=", 1)[1]
    return "RAISED: " + err.strip().splitlines()[-1] if err.strip() else "NO OUTPUT"


# ------------------------------------------------------------------ the bug this file is about
def test_half_configured_proxy_refuses_instead_of_has_remote_db():
    """The whole point. Either half alone must raise, in BOTH directions — a typo in the secret
    name is just as likely as a missing value."""
    for extra in (dict(SUPA, DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET=""),
                  dict(SUPA, DB_PROXY_URL="", DB_PROXY_SECRET="s3cret")):
        got = backend(extra)
        assert got.startswith("RAISED"), got
        assert "half-configured" in got, got
        # It used to have to say "Supabase", because that was what it refused to do. Now the
        # useful fact is which half is missing -- checked precisely in the next test -- and that
        # the process is not left thinking it has a remote.
        assert "no usable remote backend" in got, "the error must say what it refused: " + got


def test_the_error_names_the_variable_that_is_missing():
    """A misconfiguration error that does not say which half is missing costs a debugging round
    trip, and this one fires on a schedule where nobody is watching."""
    got = backend(dict(SUPA, DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET=""))
    assert "DB_PROXY_SECRET is empty" in got, got
    got = backend(dict(SUPA, DB_PROXY_URL="", DB_PROXY_SECRET="s"))
    assert "DB_PROXY_URL is empty" in got, got


# ------------------------------------------------------------------ nothing else changed
def test_a_fully_configured_proxy_is_accepted():
    got = backend(dict(SUPA, DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET="s"))
    assert got == "the app at x", got


def test_stale_supabase_credentials_select_nothing():
    """THE REMOVAL, asserted. Neither DB_PROXY var set is still a CHOICE rather than a
    misconfiguration -- the guard must not touch that path -- but the choice it now expresses is
    the local CSV, not a live remote.

    This is the case that would catch the fall-through coming back. A developer box still has
    real SUPABASE_* values in .streamlit/secrets.toml, so before 2026-09-01 this same input
    resolved to a live database with no environment variable set at all."""
    # NAME_PROBE, not PROBE: with nothing configured there is no session to resolve and PROBE
    # would report the (correct) refusal instead of the backend name.
    assert backend(dict(SUPA), NAME_PROBE) == "jobs.csv", backend(dict(SUPA), NAME_PROBE)
    # And the refusal itself is worth pinning: reaching for a session must fail, not improvise.
    got = backend(dict(SUPA))
    assert got.startswith("RAISED") and "no database backend is configured" in got, got


def test_an_empty_environment_still_resolves_through_dotenv():
    """Scrubbing the environment is NOT enough to isolate db.py, and that is worth pinning.

    db.py calls _load_env_file() at module scope, relative to the WORKING DIRECTORY, so a run
    started in the repo picks up whatever .env holds even with every relevant variable removed
    from the environment. That is precisely the mechanism behind the PG_DSN-read-before-.env bug
    db.py documents, and it is why the cron script's `cd "$APP"` is load-bearing rather than tidy.
    """
    got = backend({}, NAME_PROBE)
    assert not got.startswith("RAISED"), got
    assert got == "jobs.csv" or got.startswith(("the app at", "Postgres", "the local")), got


def test_the_csv_fallback_survives_when_there_is_genuinely_no_config():
    """Run from a directory with no .env, so nothing supplies credentials. The end of the chain
    has to stay reachable — it is what a fresh clone and the offline tests rely on."""
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = here
    with tempfile.TemporaryDirectory() as tmp:
        # NAME_PROBE: the end of the chain is still reachable, but "reachable" now means
        # has_remote_db() is False and backend_name() says jobs.csv -- NOT that a session
        # resolves. Nothing resolves when nothing is configured, by design.
        p = subprocess.run([sys.executable, "-c", NAME_PROBE], capture_output=True, text=True,
                           env=env, cwd=tmp)
        out = p.stdout or ""
    assert "BACKEND=jobs.csv" in out, (out, (p.stderr or "")[-200:])
    # ...and asking for a session there refuses, rather than inventing one.
    with tempfile.TemporaryDirectory() as tmp:
        q = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True,
                           env=env, cwd=tmp)
    assert "no database backend is configured" in (q.stderr or ""), (q.stderr or "")[-200:]


# ------------------------------------------------------------------ DB_REQUIRE
def test_db_require_proxy_passes_when_the_proxy_is_configured():
    got = backend(dict(SUPA, DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET="s",
                       DB_REQUIRE="proxy"))
    assert got == "the app at x", got


def test_db_require_proxy_refuses_an_unconfigured_run():
    """What the scrape workflow relies on: a run whose secrets never arrived dies in one second
    rather than writing 26 minutes of jobs somewhere the app does not read."""
    got = backend(dict(SUPA, DB_REQUIRE="proxy"))
    assert got.startswith("RAISED") and "DB_REQUIRE=proxy" in got, got
    # It used to name Supabase, because that is where the run would have gone. The destination
    # is now the local CSV, which is harmless but equally not what the workflow asked for.
    assert "csv" in got.lower(), got


def test_db_require_pg_refuses_when_pg_dsn_is_missing():
    """What bin/cron_scrape.sh relies on: db.py reads .env relative to the WORKING DIRECTORY, so
    a cron whose cd stops landing in $APP gets an empty PG_DSN and would write to a local CSV
    while reporting success."""
    got = backend(dict(SUPA, DB_REQUIRE="pg"))
    assert got.startswith("RAISED") and "PG_DSN is empty" in got, got


def test_db_require_supabase_now_refuses_by_name():
    """DB_REQUIRE=supabase is still ACCEPTED as a value and always fails, deliberately.

    A cron or workflow pinned to it must be told the backend is gone -- not that its
    configuration is malformed, which is what rejecting the value outright would say and would
    send whoever is on call looking for a typo. Kept until nothing in .github/workflows sets it.
    """
    got = backend(dict(SUPA, DB_REQUIRE="supabase"))
    assert got.startswith("RAISED"), got
    assert "removed on 2026-09-01" in got, got
    # ...and it fails the same way even when a perfectly good proxy is configured, because the
    # flag states an intent that can no longer be satisfied.
    got = backend(dict(SUPA, DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET="s",
                       DB_REQUIRE="supabase"))
    assert got.startswith("RAISED"), got


def test_an_unknown_db_require_value_is_rejected_not_ignored():
    """Silently ignoring a typo'd value would turn the guard off exactly when someone thought
    they had turned it on. "postgres" is the likely typo for "pg"."""
    got = backend(dict(SUPA, DB_REQUIRE="postgres"))
    assert got.startswith("RAISED") and "not one of" in got, got
    # The list it prints must no longer offer a backend that does not exist.
    assert "supabase" not in got.split("not one of", 1)[1].lower(), got


def test_db_require_is_case_and_space_insensitive():
    got = backend(dict(SUPA, DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET="s",
                       DB_REQUIRE="  PROXY "))
    assert got == "the app at x", got


def test_an_empty_db_require_is_the_same_as_unset():
    assert backend(dict(SUPA, DB_REQUIRE=""), NAME_PROBE) == "jobs.csv"


# ------------------------------------------------------------------ the workflow's own contract
def test_the_scrape_workflow_states_its_backend():
    """The guard is worthless if the caller never sets it. Read the real workflow rather than
    trusting that it was wired up."""
    here = os.path.dirname(os.path.abspath(__file__))
    y = open(os.path.join(here, "..", ".github", "workflows", "scrape.yml"),
             encoding="utf-8").read()
    assert "DB_REQUIRE: proxy" in y, "scrape.yml must state the backend it expects"
    assert "Preflight" in y, "scrape.yml should confirm the database before spending budget"


def test_the_cron_states_its_backend():
    here = os.path.dirname(os.path.abspath(__file__))
    sh = open(os.path.join(here, "bin", "cron_scrape.sh"), encoding="utf-8").read()
    assert "DB_REQUIRE=pg" in sh, "cron_scrape.sh runs on the box and must require PG_DSN"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d backend-intent checks passed." % len(fns))
