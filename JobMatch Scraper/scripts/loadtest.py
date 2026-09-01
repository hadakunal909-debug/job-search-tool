"""Hard load test against LOCAL worker processes. Never against production.

WHY THIS FILE EXISTS. docs/LOAD_SECURITY_QUALITY_REPORT.md's capacity figures were produced by
harnesses that lived in a session scratchpad and are gone, so none of those numbers can be
reproduced or re-run after a change. speedtest.py covers the DB layer and the two public routes,
and its own docstring says to measure the logged-in feed in DevTools. This is the missing piece.

    python scripts/loadtest.py                     # 4 workers, ramp to the knee
    python scripts/loadtest.py --workers 2 --budget 800
    python scripts/loadtest.py --scenario cold     # post-scrape, nothing pre-warmed

PRODUCTION IS NOT A TARGET AND CANNOT BE MADE ONE. CLAUDE.md, docs/ARCHITECTURE.md's invariants,
docs/OPERATIONS.md and the load report's §1 all forbid load-testing the live site: shared cPanel
throttles at the account level, no restart clears it, and the account was suspended once. There is
no --host flag here. The driver spawns its own workers on 127.0.0.1 and talks only to those.

THREE THINGS THAT MAKE THE NUMBERS MEAN SOMETHING, each of which was got wrong first:

  * OFFLINE IS ENFORCED, NOT ASSUMED. db._creds() falls through to .streamlit/secrets.toml, so
    db.using_supabase() is True on a developer box with no env vars set at all -- and the
    unstubbed reads (db.get_job_jd, get_brain_company, jobs_fingerprint) would then make live
    HTTPS calls, capped at 8 connections per process and retrying with Retry-After honoured. That
    injects multi-second stalls from a remote limiter into your p99, and hammers a third party.
    Each worker patches socket.connect to refuse anything that is not loopback.

  * DISTINCT RESUMES, ONE EACH. The score cache is keyed on md5 of the profile text, so N users
    sharing a résumé is one cache entry measured N times. One résumé each also keeps the score
    directory at N files, under _SCORES_MAX_FILES, so workers cannot evict each other's work.

  * WORKERS ARE SEPARATE PROCESSES. A thread pool inside one process measures the GIL, not the
    app: a feed render is CPU-bound, and Passenger runs 2-6 workers with no session affinity.
    The driver round-robins across ports, which is what "no affinity" means in practice.

The /api/feed rate limiter is deliberately LEFT ON (_FEED_TIERS, per username, per process), so
429s are reported separately from errors rather than suppressed -- an rps figure measured with it
disabled is one production can never deliver.
"""
import argparse
import ctypes
import glob
import json
import os
import random
import socket
import statistics as st
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT0 = 5071
NUSERS = 16
BASE_SKILLS = ("python flask sql postgres docker aws kubernetes terraform spark airflow pandas "
               "roadmap discovery stakeholder analytics experimentation jira agile pytest ")


def users(n=NUSERS):
    return ["lt%02d" % i for i in range(n)]


def resume_for(u, i):
    """Distinct per user, and distinct in LENGTH too, so scores actually differ."""
    return BASE_SKILLS * (2 + i % 7) + ("specialisation-%s " % u)


# --------------------------------------------------------------------------- worker (--serve)
def _block_offbox_sockets():
    """Refuse every outbound connection that is not loopback.

    Not a precaution: see the module docstring. A worker that can reach Supabase produces a
    latency distribution shaped by somebody else's rate limiter.
    """
    real = socket.socket.connect
    real_ex = socket.socket.connect_ex

    def guard(fn):
        def wrapped(self, address, *a, **k):
            host = address[0] if isinstance(address, tuple) else ""
            if host not in ("127.0.0.1", "::1", "localhost"):
                raise OSError("loadtest: outbound socket to %r refused (offline by design)" % (host,))
            return fn(self, address, *a, **k)
        return wrapped

    socket.socket.connect = guard(real)
    socket.socket.connect_ex = guard(real_ex)


def serve(port, scores_dir, budget_mb):
    os.chdir(APP)
    sys.path.insert(0, APP)
    os.environ["EV_OFF"] = "1"                 # analytics reads this ONCE at import
    os.environ["RESEARCH_ON_DEMAND"] = "0"     # default is 1: /job would offer a live crawl
    os.environ["SCORES_DIR"] = scores_dir
    os.environ["CACHE_BUDGET_MB"] = str(budget_mb)
    os.environ["JOBS_SNAPSHOT_MAX_AGE"] = "31536000"
    for k in ("DB_PROXY_URL", "DB_PROXY_SECRET", "PG_DSN", "DB_REQUIRE"):
        os.environ.pop(k, None)
    _block_offbox_sockets()

    import db                                   # noqa: E402
    import web                                  # noqa: E402
    from flask import session, request          # noqa: E402

    rows, _ = web._snapshot_read(10 ** 9)
    if not rows:
        raise SystemExit("no jobs_snapshot.json.gz -- run the app once online first")
    web.get_jobs = lambda force=False: rows
    # Read directly by _base_rows (fingerprint) and _cache_max (row count), not through get_jobs.
    web._jobs_cache.update(rows=rows, fp=(len(rows), "loadtest"), at=10 ** 12)

    U = users()
    PROFILES = {u: resume_for(u, i) for i, u in enumerate(U)}

    db.list_users = lambda: [{"username": u, "disabled_at": None} for u in U]
    db.profile_text = lambda u: PROFILES.get(u, "")
    db.get_user_statuses = lambda u: {}
    db.get_user = lambda u, cols=None: {"username": u}
    db.get_kv = lambda k, *a: None
    db.put_kv = lambda *a, **k: True
    db.insert_events = lambda rows_: True
    db._fetch_all = lambda *a, **k: []
    db.blocked_company_keys = lambda: set()
    db.get_job_jd = lambda url: "python sql aws flask docker kubernetes " * 40
    web._session_dead = lambda u: ""
    web._needs_onboarding = lambda u: False
    web.user_statuses = lambda u: {}
    web._profile_row = lambda u: {}
    web._ensure_resume_migrated = lambda u: None
    web.current_profile = lambda: PROFILES.get(session.get("user"), PROFILES[U[0]])

    @web.app.before_request
    def _login():
        session["user"] = request.args.get("asuser") or U[0]

    print("worker %d ready: %d rows, _cache_max()=%d, budget=%s MB"
          % (port, len(rows), web._cache_max(), budget_mb), flush=True)
    web.app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)


# --------------------------------------------------------------------------- driver
def avail_mb():
    try:
        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        m = MS()
        m.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / 1048576.0
    except Exception:
        return float("nan")


def rss_mb(pids):
    out = {}
    try:
        raw = subprocess.check_output(["tasklist", "/FO", "CSV", "/FI", "IMAGENAME eq python.exe"],
                                      stderr=subprocess.DEVNULL).decode("utf-8", "replace")
        for line in raw.splitlines()[1:]:
            f = [c.strip('"') for c in line.split('","')]
            if len(f) >= 5 and f[1].isdigit() and int(f[1]) in pids:
                out[int(f[1])] = int(f[4].replace(",", "").replace(" K", "")) / 1024.0
    except Exception:
        pass
    return out


def hit(url, timeout=300):
    t = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            r.read()
            return (time.perf_counter() - t) * 1000, r.status
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        return (time.perf_counter() - t) * 1000, e.code
    except Exception:
        return (time.perf_counter() - t) * 1000, 0


def level(ports, path_for, conc, seconds, U):
    """Round-robin across workers at fixed concurrency for `seconds`. Returns the sample."""
    stop = time.perf_counter() + seconds
    lat, codes, n = [], {}, [0]

    def worker(slot):
        out = []
        rnd = random.Random(slot)
        i = slot
        while time.perf_counter() < stop:
            u = U[i % len(U)]
            # THE PORT IS CHOSEN INDEPENDENTLY OF THE USER, and that is the whole point. Stepping
            # both from one counter looks like round-robin and is not: with 16 users over 4 ports
            # every index congruence pins a user to one worker, so each user would only ever see
            # one process's cache and "no session affinity" would go untested.
            p = rnd.choice(ports)
            ms, code = hit("http://127.0.0.1:%d%s" % (p, path_for(u)))
            out.append((ms, code))
            i += 1
        return out

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=conc) as ex:
        for chunk in ex.map(worker, range(conc)):
            for ms, code in chunk:
                lat.append(ms)
                codes[code] = codes.get(code, 0) + 1
    el = time.perf_counter() - t0
    lat.sort()
    q = lambda f: lat[min(len(lat) - 1, int(len(lat) * f))] if lat else float("nan")
    return {"n": len(lat), "rps": len(lat) / el if el else 0, "p50": q(.5), "p95": q(.95),
            "p99": q(.99), "max": lat[-1] if lat else float("nan"), "codes": codes}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--port", type=int, default=PORT0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--budget", type=int, default=256, help="CACHE_BUDGET_MB per worker")
    ap.add_argument("--scores", default="")
    ap.add_argument("--route", default="feed", choices=("feed", "api", "search"))
    ap.add_argument("--scenario", default="warm", choices=("warm", "cold"))
    ap.add_argument("--secs", type=int, default=15)
    ap.add_argument("--levels", default="4,8,16,32,64,128")
    ap.add_argument("--min-free-mb", type=int, default=600)
    a = ap.parse_args()

    # Outside the repo on purpose: a run must not leave an untracked directory behind, and
    # these files are disposable by definition.
    scores = a.scores or os.path.join(tempfile.gettempdir(), "jobmatch_loadtest_scores")
    if a.serve:
        return serve(a.port, scores, a.budget)

    os.makedirs(scores, exist_ok=True)
    if a.scenario == "cold":
        for f in glob.glob(os.path.join(scores, "*")):
            os.remove(f)
    U = users()
    ports = [PORT0 + i for i in range(a.workers)]
    procs = []
    print("spawning %d worker(s) on %s, budget %d MB, scores=%s"
          % (a.workers, ports, a.budget, scores))
    for p in ports:
        procs.append(subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--serve", "--port", str(p),
             "--budget", str(a.budget), "--scores", scores],
            cwd=APP, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    try:
        deadline = time.time() + 180
        while time.time() < deadline:
            if all(hit("http://127.0.0.1:%d/healthz" % p, 5)[1] == 200 for p in ports):
                break
        live = sum(1 for p in ports if hit("http://127.0.0.1:%d/healthz" % p, 5)[1] == 200)
        if live != len(ports):
            raise SystemExit("only %d/%d workers came up" % (live, len(ports)))
        print("all %d up" % live)

        # WARM EVERY WORKER FIRST. The first _build_row in a process lazily imports scraper
        # (8500+ lines) behind the import lock -- a one-time serialised spike that would
        # otherwise land inside a measured level and be read as the app being slow.
        if a.scenario == "warm":
            for p in ports:
                t = time.perf_counter()
                for u in U:
                    hit("http://127.0.0.1:%d/?asuser=%s" % (p, u))
                print("  warmed :%d in %.1f s" % (p, time.perf_counter() - t))

        paths = {"feed": lambda u: "/?asuser=%s" % u,
                 "api": lambda u: "/api/feed?asuser=%s&tab=recommended&min=0&sort=score"
                                  "&offset=0&limit=60" % u,
                 "search": lambda u: "/api/feed?asuser=%s&tab=recommended&min=0&sort=score"
                                     "&q=engineer&offset=0&limit=60" % u}
        pids = {pr.pid for pr in procs}
        print()
        print("%-6s %7s %8s %8s %8s %8s %9s  %s" %
              ("conc", "n", "rps", "p50", "p95", "p99", "freeMB", "codes"))
        base95 = None
        for conc in [int(x) for x in a.levels.split(",") if x.strip()]:
            free = avail_mb()
            if free < a.min_free_mb:
                print("STOP: only %.0f MB free (floor %d) -- not pushing further"
                      % (free, a.min_free_mb))
                break
            r = level(ports, paths[a.route], conc, a.secs, U)
            ok = r["codes"].get(200, 0)
            lim = r["codes"].get(429, 0)
            bad = r["n"] - ok - lim
            print("%-6d %7d %8.1f %8.0f %8.0f %8.0f %9.0f  %s%s"
                  % (conc, r["n"], r["rps"], r["p50"], r["p95"], r["p99"], free,
                     dict(sorted(r["codes"].items())),
                     "  <-- %d non-200/429" % bad if bad else ""))
            rss = rss_mb(pids)
            if rss:
                print("       worker RSS MB: %s" % ", ".join("%.0f" % v for v in rss.values()))
            tmp = len(glob.glob(os.path.join(scores, "*.tmp")))
            if tmp:
                print("       !! %d orphaned .tmp in the score dir (Windows os.replace clash)" % tmp)
            if base95 is None:
                base95 = r["p95"]
            elif r["p95"] > 10 * base95:
                print("STOP: p95 %.0f ms is >10x the baseline %.0f -- past the knee"
                      % (r["p95"], base95))
                break
            if bad > 0.01 * r["n"]:
                print("STOP: %d non-200/429 of %d (>1%%)" % (bad, r["n"]))
                break
    finally:
        for pr in procs:
            try:
                pr.kill()
            except Exception:
                pass
        print("\nworkers stopped")


if __name__ == "__main__":
    main()
