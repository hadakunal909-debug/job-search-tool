"""Measure local processing only, with outbound sockets blocked and analytics disabled.

Uses this app's trusted local row cache and job snapshot without modifying either.
Compares pre-change functions from a git ref against current functions, requiring equal
results before timing. Scores are synthetic so no personal profile is needed. Warm-cache
medians alternate execution order; they are not production or end-to-end timings.
"""
import argparse, ast, gzip, json, os, pickle, socket, statistics, subprocess, sys, time
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent


def main():
    os.chdir(ROOT)
    parser = argparse.ArgumentParser(description="Offline before/after hot-path benchmark using local job caches.")
    parser.add_argument("--baseline-ref", required=True, help="Git commit or ref before the performance changes")
    parser.add_argument("--output", help="Optional JSON results file")
    args_cli = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    os.environ["EV_OFF"] = "1"
    os.environ["APP_SECRET"] = "offline-benchmark-only"
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for key in ("PG_DSN", "DB_PROXY_URL", "DB_PROXY_SECRET", "DB_REQUIRE"):
        os.environ.pop(key, None)
    try:
        import dotenv
        dotenv.load_dotenv = lambda *a, **k: False
    except ImportError:
        pass
    def blocked(*a, **k):
        raise AssertionError("Benchmark must stay offline")
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    import web, core
    from resume_brain import analyze
    PREFIX = subprocess.check_output(["git", "rev-parse", "--show-prefix"], text=True).strip()

    def original(path, names, scope):
        source = subprocess.check_output(["git", "show", args_cli.baseline_ref + ":" + PREFIX + path])
        tree = ast.parse(source.decode("utf-8"))
        tree.body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        env = dict(scope)
        exec(compile(tree, "baseline", "exec"), env)
        return env

    old = original("web.py", {"_filter_rows", "_relax_suggestions"}, vars(web))
    old_analyze = original("resume_brain/analyze.py", {"analyze", "symphony"}, vars(analyze))
    caches = list((ROOT / "row_cache").glob("*.rows.gz"))
    if not caches or not (ROOT / "jobs_snapshot.json.gz").is_file():
        raise SystemExit("Local row_cache/*.rows.gz and jobs_snapshot.json.gz are required; no data is fetched.")
    cache = max(caches, key=lambda p: p.stat().st_size)
    with gzip.open(cache, "rb") as f:
        rows = pickle.load(f)["rows"]
    for i, row in enumerate(rows):
        row["score"] = (i * 17) % 101
    web._jobs_cache.update(fp=(len(rows), "offline-bench"))
    print("Local cached rows:", len(rows), flush=True)
    results = []
    def bench(label, before, after, reps=15):
        expected = before()
        actual = after()
        assert expected == actual, label + " output changed"
        times = [[], []]
        for i in range(reps):
            for which in (i % 2, 1 - i % 2):
                start = time.perf_counter()
                (before if which == 0 else after)()
                times[which].append((time.perf_counter() - start) * 1000)
        a, b = (statistics.median(t) for t in times)
        rec = dict(label=label, before_ms=round(a, 3), after_ms=round(b, 3), improvement_pct=round(100*(a-b)/a, 1), equal=True)
        results.append(rec)
        print(json.dumps(rec), flush=True)

    for label, args in [("feed default", {}),
                        ("search without extra filters", {"q":"python"}),
                        ("no-match search", {"q":"zzzzzzzz"}),
                        ("filtered fuzzy search", {"q":"data scientst", "remote":"1", "exp":"2"}),
                        ("filtered newest search", {"q":"project manger", "roles":"program", "sort":"newest"})]:
        bench(label, lambda: old["_filter_rows"](rows, {}, args), lambda: web._filter_rows(rows, {}, args))
    from werkzeug.datastructures import MultiDict
    args = MultiDict({"q":"data scientst", "remote":"1", "exp":"2", "min":"60", "sort":"newest"})
    bench("filter relaxation suggestions", lambda: old["_relax_suggestions"](rows, {}, args), lambda: web._relax_suggestions(rows, {}, args), 7)
    with gzip.open(ROOT / "jobs_snapshot.json.gz", "rt", encoding="utf-8") as f:
        jobs = json.load(f)["rows"]
    analyses = [core.unpack_analyzed(j.get("jd_terms")) for j in jobs if j.get("jd_terms")]
    analyses = [a for a in analyses if a.get("terms")][:5000]
    resume = "python sql aws docker agile project management stakeholder roadmap analytics leadership"
    bench("refresh percentage scoring (%d jobs)" % len(analyses), lambda: [0 if a.get("thin") else core.score_against(resume,a)[0] for a in analyses], lambda: [0 if a.get("thin") else core.score_pct(resume,a) for a in analyses])
    jd = ("Responsibilities Lead delivery across teams Manage stakeholder expectations Build reporting tools Improve processes Qualifications Experience with Python and SQL Support customer success Collaborate with engineers ") * 10
    bench("resume analysis (synthetic description)", lambda: old_analyze["analyze"](jd, {}), lambda: analyze.analyze(jd, {}), 31)
    if args_cli.output:
        Path(args_cli.output).write_text(json.dumps(dict(baseline=args_cli.baseline_ref, rows=len(rows), analyses=len(analyses), results=results), indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
