#!/usr/bin/env python3
"""
run_tests.py: run the test suite. THE single definition of what "the tests" means.

WHY THIS EXISTS. There is no pytest here — every suite is a plain script you run as
`python test_x.py`, and until now the only full list of them lived in three bash `for`
loops inside .github/workflows/python-tests.yml. Two consequences, both real:

  * Running everything locally meant retyping those loops, so nobody did, so suites went
    unrun between pushes.
  * The list drifted. test_sponsor_flag.py is offline-safe and has never once run in CI —
    it simply was not typed into either loop. Nothing could have caught that.

python-tests.yml now calls this script, so the list exists once. Adding a suite to SUITES
adds it to CI; there is no second place to forget.

WHY SUBPROCESSES, not in-process discovery. Three reasons, in order of how much they hurt:

  1. analytics.py reads EV_OFF ONCE, at import, into _OFF. In-process, a single suite that
     imports web before the variable is set poisons every suite after it — and the failure
     mode is writing real analytics rows, not an error. A fresh process per suite with
     EV_OFF=1 in its environment makes the rule unbreakable. One unguarded parity run once
     wrote 98.8% of all recorded feed_view events; that is what this is preventing.
  2. The root suites only execute their assertions from a `__main__` block that discovers
     test_* out of globals(). Importing them runs nothing and passes vacuously.
  3. A suite that hard-crashes the interpreter cannot take the other 42 with it.

FAILURE DETECTION IS BELT AND BRACES: the exit code, plus a scan of stdout for a FAILED
line. Two suites (scripts/test_visa.py, scripts/test_notify.py) printed "N FAILED" and then
exited 0 for months, so `set -e` in CI never saw them. That is fixed at the source, but the
bug class is cheap to keep catching.

    python scripts/run_tests.py                  # every offline suite, in parallel
    python scripts/run_tests.py --changed        # only suites your working diff can affect
    python scripts/run_tests.py --only resume    # one thing, while iterating
    python scripts/run_tests.py --list           # the inventory, grouped
    python scripts/run_tests.py --db             # add the suites needing a live database
    python scripts/run_tests.py --ci             # GitHub Actions mode
"""
import argparse
import ast
import collections
import concurrent.futures
import os
import re
import shutil
import subprocess
import tempfile
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

Suite = collections.namedtuple("Suite", "name path group needs")

# ---------------------------------------------------------------------------------------------
# THE INVENTORY.
#
# group  mirrors the three steps python-tests.yml used to have, so the Actions log keeps its
#        shape: root / scripts / parity.
# needs  offline  runs with sockets blocked and no .env — the CI-safe set.
#        node     also needs node on PATH: these lift the pure functions out of static/app.js
#                 BY SOURCE TEXT and evaluate them in node, so a copy cannot drift from what
#                 actually ships.
#        db       needs a live database. Excluded by default and gated behind --db, which
#                 REFUSES to run without credentials rather than passing vacuously — a suite
#                 that finds no users and exits 0 is a green tick that proves nothing, which
#                 is exactly why test_ext_contract sat outside CI for months.
# ---------------------------------------------------------------------------------------------
SUITES = (
    # ---- root -------------------------------------------------------------------------------
    Suite("test_backend_intent",    "test_backend_intent.py",            "root",   "offline"),
    Suite("test_board_health",      "test_board_health.py",              "root",   "offline"),
    Suite("test_canonical_url",     "test_canonical_url.py",             "root",   "offline"),
    Suite("test_clean_jd",          "test_clean_jd.py",                  "root",   "offline"),
    Suite("test_date_sources",      "test_date_sources.py",              "root",   "offline"),
    Suite("test_discover_screen",   "test_discover_screen.py",           "root",   "offline"),
    Suite("test_edu_domains",       "test_edu_domains.py",               "root",   "offline"),
    Suite("test_experience_years",  "test_experience_years.py",          "root",   "offline"),
    Suite("test_jd_lookup_order",   "test_jd_lookup_order.py",           "root",   "offline"),
    Suite("test_jd_persist",        "test_jd_persist.py",                "root",   "offline"),
    Suite("test_jobspy_adapter",    "test_jobspy_adapter.py",            "root",   "offline"),
    Suite("test_jobright_adapter",  "test_jobright_adapter.py",          "root",   "offline"),
    Suite("test_liveness",          "test_liveness.py",                  "root",   "offline"),
    Suite("test_password_rule",     "test_password_rule.py",             "root",   "offline"),
    Suite("test_pm_rule",           "test_pm_rule.py",                   "root",   "offline"),
    Suite("test_prompt_evals",      "test_prompt_evals.py",              "root",   "offline"),
    # Guards for the QA_AUDIT findings fixed on 2026-08-24. One file rather than six, because
    # most are one- or two-line fixes in code with no suite of its own; the finding id in each
    # test name is what keeps them traceable. See docs/QA_AUDIT.md.
    Suite("test_qa_audit_fixes",    "test_qa_audit_fixes.py",            "root",   "offline"),
    Suite("test_reposts",           "test_reposts.py",                   "root",   "offline"),
    Suite("test_resume_bullets",    "test_resume_bullets.py",            "root",   "offline"),
    Suite("test_resume_keywords",   "test_resume_keywords.py",           "root",   "offline"),
    Suite("test_resume_score",      "test_resume_score.py",              "root",   "offline"),
    Suite("test_resume_store",      "test_resume_store.py",              "root",   "offline"),
    Suite("test_resume_upload",     "test_resume_upload.py",             "root",   "offline"),
    Suite("test_scoring",           "test_scoring.py",                   "root",   "offline"),
    Suite("test_scrape_slice",      "test_scrape_slice.py",              "root",   "offline"),
    # Offline by its own docstring ("No database and no network -- the indexes are built
    # inline") and yet it appeared in NEITHER loop in python-tests.yml. It has never run in CI.
    Suite("test_sponsor_flag",      "test_sponsor_flag.py",              "root",   "offline"),
    Suite("test_templates_parse",   "test_templates_parse.py",           "root",   "offline"),
    Suite("test_title_filter",      "test_title_filter.py",              "root",   "offline"),
    Suite("test_us_location",       "test_us_location.py",               "root",   "offline"),
    Suite("test_verify_queue",      "test_verify_queue.py",              "root",   "offline"),
    Suite("test_workday_date",      "test_workday_date.py",              "root",   "offline"),

    # ---- scripts ----------------------------------------------------------------------------
    Suite("test_add_board_api",     "scripts/test_add_board_api.py",     "scripts", "offline"),
    Suite("test_companies_page",    "scripts/test_companies_page.py",    "scripts", "offline"),
    Suite("test_contrast",          "scripts/test_contrast.py",          "scripts", "offline"),
    Suite("test_doc_contrast",      "scripts/test_doc_contrast.py",      "scripts", "offline"),
    Suite("test_dbproxy",           "scripts/test_dbproxy.py",           "scripts", "offline"),
    Suite("test_ext_contract",      "scripts/test_ext_contract.py",      "scripts", "offline"),
    # Symmetrical on purpose: it asserts the paced case PASSES as well as the burst case tripping.
    # A cap of 1 would satisfy a trip-only test and break the feed for everyone.
    Suite("test_feed_ratelimit",    "scripts/test_feed_ratelimit.py",    "scripts", "offline"),
    Suite("test_fonts",             "scripts/test_fonts.py",             "scripts", "offline"),
    # core.html_to_text is the front door: every ATS blob and every page scrape becomes a
    # stored description through it, and it had NO test -- nothing pinned the separator, the
    # unescape-before-parse ordering, or that <script> text was not being removed.
    Suite("test_html_to_text",      "scripts/test_html_to_text.py",      "scripts", "offline"),
    # IBM is the only board whose posting PAGE cannot be read at all (202 + empty body), so
    # the api shape is the only thing standing between it and 216 blank descriptions.
    Suite("test_ibm_jd",            "scripts/test_ibm_jd.py",            "scripts", "offline"),
    Suite("test_jdrender",          "scripts/test_jdrender.py",          "scripts", "offline"),
    Suite("test_user_scores",       "scripts/test_user_scores.py",       "scripts", "offline"),
    Suite("test_job_page",          "scripts/test_job_page.py",          "scripts", "offline"),
    Suite("test_jobs_cache",        "scripts/test_jobs_cache.py",        "scripts", "offline"),
    Suite("test_logos",             "scripts/test_logos.py",             "scripts", "offline"),
    Suite("test_notify",            "scripts/test_notify.py",            "scripts", "offline"),
    # The role/employer norms. The synthetic half runs anywhere; the corpus half asserts
    # against the committed norms.json, which is where a builder regression shows up --
    # geography or an eligibility gate back inside "what this role asks for" passes every
    # synthetic test.
    Suite("test_norms",             "scripts/test_norms.py",             "scripts", "offline"),
    Suite("test_onboarding",        "scripts/test_onboarding.py",        "scripts", "offline"),
    Suite("test_paylocity",         "scripts/test_paylocity.py",         "scripts", "offline"),
    Suite("test_peoplesoft",        "scripts/test_peoplesoft.py",        "scripts", "offline"),
    Suite("test_pgrest",            "scripts/test_pgrest.py",            "scripts", "offline"),
    Suite("test_search_and_similar","scripts/test_search_and_similar.py","scripts", "offline"),
    # Offline by construction: the JD-memo half stubs db._fetch_all, and the _row_pending
    # half reads the LOCAL snapshot when there is one and falls back to synthetic shapes
    # when there is not -- so CI checks the logic and a laptop checks it against all ~22k
    # real rows.
    Suite("test_speed_caches",      "scripts/test_speed_caches.py",      "scripts", "offline"),
    Suite("test_transport",         "scripts/test_transport.py",         "scripts", "offline"),
    Suite("test_visa",              "scripts/test_visa.py",              "scripts", "offline"),

    # ---- parity: the highest-value gate in the repo ------------------------------------------
    # web.py::_filter_rows and static/app.js::matches() are deliberate twins and the feed
    # switches between them at 4,000 jobs. Nothing but these three keeps them in step.
    Suite("test_card_meta",         "scripts/test_card_meta.py",         "parity", "node"),
    Suite("test_filter_memory",     "scripts/test_filter_memory.py",     "parity", "node"),
    Suite("feed_parity",            "scripts/feed_parity.py",            "parity", "node"),

    # Applying is a claim the user makes, not a consequence of a click. Guards the /api/action
    # `via` whitelist, the wiring that stopped the Apply link writing an application on its own,
    # and the predicate scripts/reset_autologged_applies.py deletes on.
    Suite("test_apply_confirm",      "scripts/test_apply_confirm.py",     "core",   "none"),

    # ---- needs a live database (--db) --------------------------------------------------------
    Suite("test_prefs",             "scripts/test_prefs.py",             "db",     "db"),
    Suite("test_saved_search",      "scripts/test_saved_search.py",      "db",     "db"),
    Suite("smoke_app",              "scripts/smoke_app.py",              "db",     "db"),
    Suite("verify_parsers",         "scripts/verify_parsers.py",         "db",     "db"),
    Suite("verify_sponsor_counts",  "scripts/verify_sponsor_counts.py",  "db",     "db"),
    Suite("verify_visa_tags",       "scripts/verify_visa_tags.py",       "db",     "db"),
)

# Non-import dependencies, for --changed. A suite's first-party imports are derived from its
# source (see _touches), so this covers only what a suite reads by PATH rather than by import.
EXTRA_TOUCHES = {
    "test_contrast":           ("static/style.css",),
    # Neither of these is an import, so neither is discoverable from the AST: the suite reads
    # style.css and templates/ as DATA, and the woff2 files are what it asserts exist.
    "test_fonts":              ("static/style.css", "templates/", "static/fonts/"),
    "test_doc_contrast":       ("docs/doc.css", "static/style.css"),
    # templates/ IS NOT OPTIONAL HERE and its absence was a live hole: this suite opens
    # templates/job.html by path and asserts against its source, so every edit to that file was
    # invisible to --changed and reported green while breaking it. Found 2026-09-05 while moving
    # the card's facts onto the job page.
    "test_card_meta":          ("static/app.js", "templates/"),
    "test_apply_confirm":      ("static/app.js", "static/applyask.js",
                                "scripts/reset_autologged_applies.py"),
    "test_filter_memory":      ("static/app.js",),
    "feed_parity":             ("static/app.js",),
    "test_search_and_similar": ("static/app.js",),
    "test_onboarding":         ("templates/", "static/style.css"),
    "test_job_page":           ("templates/",),
    # The directory renders from a data file and a template, and its sector map lives in
    # a script it imports at runtime rather than at module scope -- neither is reachable
    # from the import walk below.
    "test_companies_page":     ("templates/", "static/companies.js", "companies.json",
                                "scripts/build_companies.py"),
    "test_jdrender":           ("scripts/fixtures/",),
    "test_jobs_cache":         ("templates/",),
    # It lifts slug() out of companies.js by source text, judges generated images with
    # the harvester's own rules, and drives --check against a temporary tree. None of
    # that is reachable from the import walk.
    "test_logos":              ("static/companies.js", "static/logos/", "companies.json",
                                "scripts/build_logos.py", "scripts/feed_parity.py"),
    "test_ext_contract":       ("extension/",),
    # The route and popup.js are two halves of one contract: the popup reads `error` and
    # `need_name`, and the route only bothers to set them because the popup shows them.
    "test_add_board_api":      ("extension/",),
    "test_resume_score":       ("resume_vocab.json", "resume_keywords.json"),
    "test_resume_keywords":    ("resume_keywords.json",),
}

FAIL_LINE = re.compile(r"^\s*FAIL\b|\b\d+\s+FAILED\b", re.M)


def _first_party():
    """Module names that live in this app, so an import of one is a real dependency."""
    names = set()
    for f in os.listdir(APP):
        if f.endswith(".py"):
            names.add(f[:-3])
        elif os.path.isfile(os.path.join(APP, f, "__init__.py")):
            names.add(f)
    return names


def _touches(suite, firstparty):
    """Files a change to which should re-run this suite.

    Derived from the suite's own imports rather than a hand list, so adding a suite needs no
    bookkeeping here and the mapping cannot drift from what the suite actually uses.
    """
    hit = {suite.path}
    hit.update(EXTRA_TOUCHES.get(suite.name, ()))
    try:
        # newline="" so nothing is silently translated; the working copy is mostly CRLF.
        with open(os.path.join(APP, suite.path), encoding="utf-8",
                  errors="replace", newline="") as fh:
            tree = ast.parse(fh.read().replace("\r\n", "\n"))
    except (OSError, SyntaxError):
        return hit
    for node in ast.walk(tree):
        mods = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods = [node.module]
        for m in mods:
            top = m.split(".")[0]
            if top in firstparty:
                hit.add(top + ("/" if top in ("scraper", "resume_brain") else ".py"))
    return hit


def _changed_paths():
    """Working-tree + staged changes, as paths relative to the APP dir.

    git reports from the repository root, which is this app's PARENT (three projects live
    there), so the prefix has to come off before anything matches SUITES.
    """
    out = set()
    for args in (["diff", "--name-only", "HEAD"], ["diff", "--name-only", "--cached"]):
        try:
            r = subprocess.run(["git"] + args, cwd=APP, capture_output=True,
                               text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        for ln in (r.stdout or "").splitlines():
            ln = ln.strip().strip('"')
            if not ln:
                continue
            here = os.path.basename(APP) + "/"
            out.add(ln[len(here):] if ln.startswith(here) else ln)
    return out


def _select(args, firstparty):
    picked = [s for s in SUITES if s.needs != "db"]
    if args.db:
        picked = list(SUITES)
    if args.group:
        picked = [s for s in picked if s.group == args.group]
    if args.only:
        picked = [s for s in picked if args.only.lower() in s.name.lower()]
    if args.changed:
        changed = _changed_paths()
        if not changed:
            return []
        keep = []
        for s in picked:
            for t in _touches(s, firstparty):
                if any(c == t or c.startswith(t) for c in changed):
                    keep.append(s)
                    break
        picked = keep
    return picked


# One directory for the whole run, removed at the end. Per-run rather than per-suite so the
# cross-worker sharing these files exist for is still exercised, just not against real ones.
_SCRATCH = tempfile.mkdtemp(prefix="jobmatch-tests-")


def _run_one(suite, ci):
    env = dict(os.environ)
    # The one rule this script exists to make unbreakable. analytics.py caches it at import.
    env["EV_OFF"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # A FIXED, OBVIOUSLY-FAKE SESSION KEY, because since 2026-09-01 web.py refuses to import
    # with a real database configured and no APP_SECRET (it used to hash the Supabase key into
    # the session secret, and that key is gone). The --db suites set DB_PROXY_* and import web,
    # so without this they would fail on the guard rather than on anything they test. Set here
    # rather than in each suite so there is one place to look, and deliberately not random:
    # a stable value means a suite can sign a cookie in one process and read it in another.
    env.setdefault("APP_SECRET", "loadbearing-test-key-not-a-secret")
    # A SCRATCH CACHE DIRECTORY PER RUN, and this is a correctness rule now rather than tidiness.
    # row_cache/ and score_cache/ are keyed on (corpus fingerprint, derived signature) and a
    # worker will read one WITHOUT loading the corpus -- that is the whole point of _corpus_fp.
    # So a suite that stubs web.get_jobs with six synthetic jobs, and does nothing else, still
    # finds the developer's real 40,294-row file sitting under the real fingerprint and renders
    # THAT. It happened to scripts/test_job_page.py on 2026-09-01 and the symptom was six
    # unrelated assertions failing at once. Pointing both directories somewhere empty means no
    # suite can read, or corrupt, the caches the developer is actually running the app against.
    env["ROWS_DIR"] = os.path.join(_SCRATCH, "row_cache")
    env["SCORES_DIR"] = os.path.join(_SCRATCH, "score_cache")
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, suite.path], cwd=APP, env=env,
                           capture_output=True, text=True, errors="replace", timeout=900)
        out = (r.stdout or "") + (r.stderr or "")
        code = r.returncode
    except subprocess.TimeoutExpired:
        return suite, False, "TIMEOUT after 900s", time.time() - t0
    except OSError as e:
        return suite, False, "could not start: %s" % e, time.time() - t0
    # Belt and braces: a suite that prints FAILED and exits 0 still counts as failed.
    ok = code == 0 and not FAIL_LINE.search(out)
    if code == 0 and not ok:
        out += "\n[run_tests] exit code was 0 but output contains a FAILED line.\n"
    return suite, ok, out, time.time() - t0


def main():
    ap = argparse.ArgumentParser(description="Run the JobMatch test suite.")
    ap.add_argument("-j", "--jobs", type=int, default=max(2, (os.cpu_count() or 4) // 2),
                    help="parallel workers (default: half your cores)")
    ap.add_argument("--changed", action="store_true",
                    help="only suites your working diff can affect")
    ap.add_argument("--only", metavar="SUBSTR", help="suites whose name contains SUBSTR")
    ap.add_argument("--group", choices=("root", "scripts", "parity", "db"))
    ap.add_argument("--db", action="store_true",
                    help="also run the suites needing a live database")
    ap.add_argument("--list", action="store_true", help="print the inventory and exit")
    ap.add_argument("--ci", action="store_true",
                    help="GitHub Actions mode: ::group:: markers, sequential, no colour")
    args = ap.parse_args()

    firstparty = _first_party()

    if args.list:
        print("%-26s %-8s %-8s %s" % ("SUITE", "GROUP", "NEEDS", "PATH"))
        for s in SUITES:
            print("%-26s %-8s %-8s %s" % (s.name, s.group, s.needs, s.path))
        by = collections.Counter(s.needs for s in SUITES)
        print("\n%d suites: %s" % (len(SUITES),
                                   ", ".join("%d %s" % (n, k) for k, n in sorted(by.items()))))
        print("default run = offline + node (%d)" % (by["offline"] + by["node"]))
        return 0

    if args.db and not (os.environ.get("PG_DSN") or os.environ.get("DB_PROXY_SECRET")
                        or os.path.exists(os.path.join(APP, ".env"))):
        print("--db needs credentials: set PG_DSN, or DB_PROXY_URL + DB_PROXY_SECRET, or "
              "provide .env.\nRefusing to run them without — a suite that finds no data and "
              "exits 0 is a green tick that proves nothing.")
        return 2

    picked = _select(args, firstparty)

    if not picked:
        print("nothing selected." + (" Working tree is clean." if args.changed else ""))
        return 0

    if not shutil.which("node"):
        skipped = [s for s in picked if s.needs == "node"]
        if skipped:
            print("node not on PATH -- skipping %d parity suite(s): %s"
                  % (len(skipped), ", ".join(s.name for s in skipped)))
            print("These are the ONLY guard on web.py/app.js staying in step. Install node.\n")
            picked = [s for s in picked if s.needs != "node"]

    workers = 1 if args.ci else max(1, min(args.jobs, len(picked)))
    print("running %d suite(s), %d worker(s)\n" % (len(picked), workers))

    results, t0 = [], time.time()
    if workers == 1:
        for s in picked:
            if args.ci:
                print("::group::%s" % s.name, flush=True)
            res = _run_one(s, args.ci)
            if args.ci:
                sys.stdout.write(res[2])
                print("::endgroup::", flush=True)
            print("%s %-26s %5.1fs" % ("ok  " if res[1] else "FAIL", s.name, res[3]),
                  flush=True)
            results.append(res)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_run_one, s, args.ci): s for s in picked}
            for fut in concurrent.futures.as_completed(futs):
                res = fut.result()
                print("%s %-26s %5.1fs" % ("ok  " if res[1] else "FAIL", res[0].name, res[3]),
                      flush=True)
                results.append(res)

    bad = [r for r in results if not r[1]]
    if bad and not args.ci:
        for suite, _ok, out, _t in sorted(bad, key=lambda r: r[0].name):
            print("\n" + "=" * 90)
            print("FAILED  %s   (%s)" % (suite.name, suite.path))
            print("=" * 90)
            tail = out.strip().splitlines()
            print("\n".join(tail[-40:]) if len(tail) > 40 else "\n".join(tail))

    print("\n%d/%d passed in %.1fs" % (len(results) - len(bad), len(results), time.time() - t0))
    if bad:
        print("failed: %s" % ", ".join(sorted(r[0].name for r in bad)))
        if args.ci:
            print("::error::%d suite(s) failed: %s"
                  % (len(bad), ", ".join(sorted(r[0].name for r in bad))))
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        _code = main()
    finally:
        shutil.rmtree(_SCRATCH, ignore_errors=True)
    sys.exit(_code)
