"""
test_backend_intent.py — db.py must never silently write to a backend nobody asked for.

No external test deps, no network, no database: run it directly
    python test_backend_intent.py
or via pytest if you have it (functions are named test_*).

WHY THIS FILE EXISTS. db.py falls back PG_DSN -> DB_PROXY_* -> Supabase -> jobs.csv, and every
step of that chain is silent. `dbproxy.client_from_env()` needs BOTH DB_PROXY_ vars and returns
None otherwise, which the caller reads as "use Supabase". So a missing or typo'd secret does not
fail — it redirects every write to the database this project moved off on 2026-08-15, and the run
reports success. Measured 2026-08-19: DB_PROXY_URL set with DB_PROXY_SECRET empty answers
"Supabase" from backend_name().

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
# Credentials shaped like the real thing but pointing nowhere. Present so the Supabase branch is
# reachable: without them the chain would fall to jobs.csv and the tests would prove less.
SUPA = {"SUPABASE_URL": "https://legacy.example.supabase.co", "SUPABASE_KEY": "not-a-real-key"}

# Touch _http so the LAZY transport selection actually runs — importing db alone resolves nothing.
PROBE = "import db; db._http.get; print('BACKEND=' + db.backend_name())"


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


def backend(extra):
    out, err = run(extra)
    for line in out.splitlines():
        if line.startswith("BACKEND="):
            return line.split("=", 1)[1]
    return "RAISED: " + err.strip().splitlines()[-1] if err.strip() else "NO OUTPUT"


# ------------------------------------------------------------------ the bug this file is about
def test_half_configured_proxy_refuses_instead_of_using_supabase():
    """The whole point. Either half alone must raise, in BOTH directions — a typo in the secret
    name is just as likely as a missing value."""
    for extra in (dict(SUPA, DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET=""),
                  dict(SUPA, DB_PROXY_URL="", DB_PROXY_SECRET="s3cret")):
        got = backend(extra)
        assert got.startswith("RAISED"), got
        assert "half-configured" in got, got
        assert "Supabase" in got, "the error must name what it refused to do: " + got


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


def test_choosing_supabase_deliberately_still_works():
    """Neither DB_PROXY var set is a CHOICE, not a misconfiguration. Local scripts and old
    checkouts rely on it, so the guard must not touch this path."""
    assert backend(dict(SUPA)) == "Supabase", backend(dict(SUPA))


def test_an_empty_environment_still_resolves_through_dotenv():
    """Scrubbing the environment is NOT enough to isolate db.py, and that is worth pinning.

    db.py calls _load_env_file() at module scope, relative to the WORKING DIRECTORY, so a run
    started in the repo picks up whatever .env holds even with every relevant variable removed
    from the environment. That is precisely the mechanism behind the PG_DSN-read-before-.env bug
    db.py documents, and it is why the cron script's `cd "$APP"` is load-bearing rather than tidy.
    """
    got = backend({})
    assert not got.startswith("RAISED"), got
    assert got in ("Supabase", "jobs.csv") or got.startswith(("the app at", "Postgres", "the local")), got


def test_the_csv_fallback_survives_when_there_is_genuinely_no_config():
    """Run from a directory with no .env, so nothing supplies credentials. The end of the chain
    has to stay reachable — it is what a fresh clone and the offline tests rely on."""
    import tempfile
    here = os.path.dirname(os.path.abspath(__file__))
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = here
    with tempfile.TemporaryDirectory() as tmp:
        p = subprocess.run([sys.executable, "-c", PROBE], capture_output=True, text=True,
                           env=env, cwd=tmp)
        out = p.stdout or ""
    assert "BACKEND=jobs.csv" in out, (out, (p.stderr or "")[-200:])


# ------------------------------------------------------------------ DB_REQUIRE
def test_db_require_proxy_passes_when_the_proxy_is_configured():
    got = backend(dict(SUPA, DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET="s",
                       DB_REQUIRE="proxy"))
    assert got == "the app at x", got


def test_db_require_proxy_refuses_a_supabase_run():
    """What the scrape workflow relies on: a run whose secrets never arrived dies in one second
    rather than writing 26 minutes of jobs somewhere the app does not read."""
    got = backend(dict(SUPA, DB_REQUIRE="proxy"))
    assert got.startswith("RAISED") and "DB_REQUIRE=proxy" in got, got
    assert "supabase" in got.lower(), got


def test_db_require_pg_refuses_when_pg_dsn_is_missing():
    """What bin/cron_scrape.sh relies on: db.py reads .env relative to the WORKING DIRECTORY, so
    a cron whose cd stops landing in $APP gets an empty PG_DSN and would write to Supabase or a
    CSV while reporting success."""
    got = backend(dict(SUPA, DB_REQUIRE="pg"))
    assert got.startswith("RAISED") and "PG_DSN is empty" in got, got


def test_db_require_supabase_is_honoured_too():
    """The flag states intent; it is not a synonym for "use the proxy"."""
    assert backend(dict(SUPA, DB_REQUIRE="supabase")) == "Supabase"
    got = backend(dict(SUPA, DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET="s",
                       DB_REQUIRE="supabase"))
    assert got.startswith("RAISED"), got


def test_an_unknown_db_require_value_is_rejected_not_ignored():
    """Silently ignoring a typo'd value would turn the guard off exactly when someone thought
    they had turned it on. "postgres" is the likely typo for "pg"."""
    got = backend(dict(SUPA, DB_REQUIRE="postgres"))
    assert got.startswith("RAISED") and "not one of" in got, got


def test_db_require_is_case_and_space_insensitive():
    got = backend(dict(SUPA, DB_PROXY_URL="https://x/api/db", DB_PROXY_SECRET="s",
                       DB_REQUIRE="  PROXY "))
    assert got == "the app at x", got


def test_an_empty_db_require_is_the_same_as_unset():
    assert backend(dict(SUPA, DB_REQUIRE="")) == "Supabase"


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
