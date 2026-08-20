#!/usr/bin/env python3
"""
build_docs.py: generate docs/INDEX.md and docs/MAP.md from the code itself.

WHY THIS EXISTS. Hand-written line numbers rot in days. db.py's own docstring pointed at
scraper/__init__.py:4715-4730 for a function that lives at 6287, and docs/ARCHITECTURE.md
described a Streamlit app reading jobs.csv for three months after that stopped being true.
Nothing was wrong with the prose; nothing checked it.

So the split, and it is the whole design:

    GENERATE what changes on the scale of a commit  -- line numbers, symbol lists,
                                                       section boundaries, route tables.
    HAND-WRITE what changes on the scale of a quarter -- why a thing exists, what breaks
                                                       when you touch it, which test guards it.

Then make CI refuse a mismatch. The hand-written half lives in INDEX below, keyed by
`module::symbol` and NEVER by a line number, and this script resolves each key to its
current line via ast. That is what makes it un-rottable and what gives --check teeth: a
renamed function fails the build naming the row that mentions it, instead of quietly
becoming a lie.

    python scripts/build_docs.py              write docs/INDEX.md + docs/MAP.md
    python scripts/build_docs.py --check      fail if they are stale or name something gone
    python scripts/build_docs.py --list-refs  every {mod::sym} the prose uses, resolved

DELIBERATELY DOES NOT IMPORT THE MODULES IT MAPS. Importing web builds the Flask app, reads
.env, and can write analytics rows. Text-scanned via ast instead, the same reasoning
scripts/build_deploy_zip.py gives for its own import check.
"""
import argparse
import ast
import collections
import difflib
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_OUT = os.path.join("docs", "INDEX.md")
MAP_OUT = os.path.join("docs", "MAP.md")

# Rooting every walk at APP is what excludes ../.claude/worktrees -- three complete stale copies
# of this tree, which make any repo-wide glob return 4x hits -- and the sibling Resume Tailoring
# project, which has its own web.py. Structural, so it cannot be forgotten.
SKIP_DIRS = {".claude", "node_modules", "__pycache__", "dist", ".vite", ".tectonic-cache",
             ".streamlit", "egress_probe", "fixtures", "vendor", ".git", "docs"}

# ---------------------------------------------------------------------------------------------
# WHAT TO MAP, in the order a newcomer should meet it.
# ---------------------------------------------------------------------------------------------
MODULES = (
    ("web.py",                  "The Flask app: every route, every request hook, the feed."),
    ("core.py",                 "The shared domain library. Imported by the app, the scraper, "
                                "the scorer and the digest, so nothing presentational lives here."),
    ("db.py",                   "Storage. One PostgREST-shaped interface over four backends."),
    ("scraper/__init__.py",     "The sweep and the intake filter, plus every ATS adapter."),
    ("scraper/score_jobs.py",   "Fetches descriptions and scores them against the resume."),
    ("resume_score.py",         "The offline resume rubric -- no network, no model."),
    ("resume_keywords.py",      "Which curated skills a track is expected to show."),
    ("resume_bullets.py",       "Per-bullet review: verb, scope, result."),
    ("jdrender.py",             "Job description -> HTML. Kept out of web.py and core.py."),
    ("pgrest.py",               "Transport 1: PostgREST verbs reimplemented over psycopg."),
    ("dbproxy.py",              "Transport 2: HMAC-signed HTTPS, how off-host code reaches the DB."),
    ("analytics.py",            "Event capture. Reads EV_OFF once, at import."),
    ("auth.py",                 "Password hashing and the login rule."),
    ("cpanelapi.py",            "cPanel UAPI calls for the admin disk/usage panel."),
    ("static/app.js",           "The client feed. Twin of web.py's filter and card builder."),
)

# Everything under these gets mapped after MODULES, alphabetically.
PACKAGES = ("scraper", "resume_brain")

# ---------------------------------------------------------------------------------------------
# THE HAND-WRITTEN HALF.
#
# {module::symbol} anywhere in the prose is resolved to `file.py:LINE` at render time. Never
# write a line number here -- that is the entire point. --check fails if a key stops resolving.
#
# sev  R = changing this alone breaks production SILENTLY
#      Y = lives in two or more places that must agree
#      B = one place, safe to change, but non-obvious to find
# ---------------------------------------------------------------------------------------------
Row = collections.namedtuple("Row", "sev symptom target coupled guard")

INDEX = (
    # ---- the feed ---------------------------------------------------------------------------
    Row("R", "The feed shows a job it shouldn't, or hides one it should",
        "{web.py::_filter_rows} -- the server-side filter",
        "{static/app.js::matches} (client) and {core.py::prefs_match} (email digest). All three "
        "must agree. The switch is {web.py::_FEED_INLINE_MAX} = 4000: below it the browser "
        "filters the whole corpus, above it the server does.",
        "python scripts/feed_parity.py"),
    Row("Y", "A field is missing from a feed card, or I added one",
        "{web.py::_build_row} -- one dict per card, and every key is consumed by the client",
        "{static/app.js::cardHTML}. ~16 function-for-function twins exist between these two "
        "files; this pair and the filter above are the ones that bite.",
        "python scripts/test_card_meta.py"),
    Row("B", "Search misses an obvious hit, or ranks badly",
        "{web.py::searchHit} and {web.py::searchRank} -- typo-tolerant matching",
        "mirrored in app.js function-for-function. {web.py::_search_tol} sets how much typo "
        "slack a term gets by length.",
        "python scripts/test_search_and_similar.py"),
    Row("B", "The feed is slow, or shows stale jobs after a scrape",
        "{web.py::get_jobs} -- TTL cache over the corpus, and {web.py::ranked_rows}",
        "{web.py::_invalidate_jobs} is what a write must call. The on-disk gzip snapshot is "
        "{web.py::_snapshot_read} / {web.py::_snapshot_write}, shared across Passenger workers.",
        "python scripts/test_jobs_cache.py"),
    Row("Y", '"Similar roles" on a job page looks unrelated',
        "{web.py::_title_index} and {web.py::_similar_roles}",
        "{web.py::_TITLE_STOP} -- but see Known-wrong B1 first, because the copy that actually "
        "runs is not the one next to this code.",
        "python scripts/test_search_and_similar.py"),

    # ---- intake -----------------------------------------------------------------------------
    Row("R", "A job exists on the employer's board but never reaches my feed",
        "the keep loop in {scraper/__init__.py::main} -- nine gates in order, each one tallied",
        "the drop reason is printed at the end of a run. Read the count, then find that exact "
        "string in the funnel diagram in docs/ARCHITECTURE.md, which is generated from the same "
        "dict. {core.py::admits_on_description} is the rescue path for a job whose TITLE matched "
        "nothing.",
        "python scripts/dump_titles.py"),
    Row("Y", "A whole board suddenly returns nothing",
        "the adapter for its ATS in {scraper/__init__.py::SCRAPERS} -- 29 entries, ats_type -> "
        "function",
        "{scraper/__init__.py::SOURCES} holds the built-in boards; app-added ones come from the "
        "boards table and are merged on top. {scraper/__init__.py::save_board_health} keeps an "
        "8-run rolling window per board, visible in /admin/data.",
        "python scripts/verify_parsers.py"),
    Row("R", "Jobs are disappearing that should still be live",
        "{scraper/__init__.py::reconcile_closed} -- the 3-consecutive-miss rule",
        "every guard around it moves together: CLOSED_AFTER_MISSES, RECONCILE_MIN_ROWS, "
        "RECONCILE_MIN_RATIO. Loosen one and a bot-walled board retires its whole inventory in "
        "a single pass. It sets is_active=false and never deletes.",
        "python test_liveness.py"),
    Row("Y", "The corpus is growing or shrinking unexpectedly",
        "{scraper/__init__.py::MAX_AGE_DAYS} on the way in, {db.py::prune_old_jobs} on the way out",
        "MAX_AGE_DAYS, PRUNE_DAYS and {db.py::AGE_LONG_DAYS} must move together. If the intake "
        "gate and the prune disagree, the corpus drifts to whichever is looser.",
        "python scripts/prune_stale.py --dry-run"),
    Row("B", "The same job appears twice",
        "{web.py::_dedupe_rows} for display, {core.py::posting_key} for identity",
        "{scraper/__init__.py::canonical_url} runs before insert, so the writer and the table "
        "cannot disagree about the key. Employer floods are GROUPED, never deduped -- that is "
        "deliberate.",
        "python test_canonical_url.py"),
    Row("B", "Adding a board from the app says it can't detect the ATS",
        "{scraper/__init__.py::detect_board} -- the detect chain, then "
        "{scraper/__init__.py::probe_board} which validates before any insert",
        "digitas is deliberately NOT emitted by detect_board: it reads a 14 MB sitemap and must "
        "not be pointable at an arbitrary host.",
        "python scripts/verify_parsers.py"),

    # ---- scoring ----------------------------------------------------------------------------
    Row("Y", "Match percentages look wrong across the board",
        "{core.py::score_against} and {core.py::core_terms}",
        "{core.py::CORE_WEIGHT_FRACTION} weights the terms that matter. If you change what the "
        "score MEANS, bump {core.py::MIN_SCALE} or every saved search keeps filtering on the old "
        "scale. Do not make the number relative -- see the note in CLAUDE.md.",
        "python test_scoring.py"),
    Row("R", "A new scoring field isn't persisting",
        "{scraper/score_jobs.py::_persist_derived}",
        "{db.py::COLS_SCORE} must contain every column the diff compares, or have.get(k) is None "
        "forever and every row re-upserts on all three runs a day.",
        "python test_scoring.py"),
    Row("R", "Scores changed and nobody touched the scorer",
        "idf.json, written by {core.py::save_idf} on the FULL pass only",
        "a partial rebuild silently re-weights every score in the corpus. It is committed source-"
        "of-truth, not a cache. {core.py::load_idf} is every reader.",
        "python test_scoring.py"),
    Row("Y", "A job page shows no description although we fetched one",
        "{scraper/score_jobs.py::_jd_corpus} -- the two-store reconciliation",
        "jd_cache.json.gz and the jobs.jd column are written at different moments and HAVE "
        "diverged, to 48% of pending rows. Any new writer of a description must touch both.",
        "python test_jd_persist.py"),
    Row("B", "A description is present but useless -- a cookie banner, a nav bar",
        "{scraper/score_jobs.py::_is_thin_jd} and the per-host retry ledger around it",
        "three different thresholds exist: {core.py::_MIN_JD_CHARS}, score_jobs' own page "
        "minimum, and the thin test. Editing score_jobs.py at all reopens every backed-off host "
        "for one round, because the ledger key hashes the file.",
        "python scripts/refetch_thin_jds.py --dry-run"),
    Row("R", "jd_terms round-trips wrong, or every row re-upserts every run",
        "{core.py::pack_analyzed} / {core.py::unpack_analyzed}",
        "jobs.jd_terms is TEXT, not jsonb, on purpose, and the KEY ORDER is semantic: it is "
        "analyze_jd's frozen term order, it breaks ties in the skill panel, and score_jobs diffs "
        "the stored string to decide whether to write at all.",
        "python test_jd_persist.py"),

    # ---- the database -----------------------------------------------------------------------
    Row("R", 'A scrape reported success but the database did not change',
        "{db.py::_LazyHTTP} -- which of the four backends you actually got",
        ".env is read relative to the CURRENT WORKING DIRECTORY, so a script that does not cd "
        "here first silently writes somewhere else and exits 0. {db.py::_check_backend_intent} "
        "plus DB_REQUIRE is how you make that fail loudly. {db.py::backend_name} tells you which "
        "backend you have; using_supabase() answers True for three of them and will not.",
        "python scripts/probe_db_proxy.py"),
    Row("Y", "A query works locally but 403s from GitHub Actions",
        "{dbproxy.py::ALLOWED_TABLES} -- the proxy's allowlist",
        "it must track db.py's table constants. A new table works on PG_DSN and is refused "
        "through the proxy, so the failure only ever appears in CI.",
        "python scripts/test_dbproxy.py"),
    Row("R", "A script hangs, or the egress bill jumps",
        "{db.py::load_jobs} called with no cols -- that is ~130 MB of descriptions",
        "{db.py::_warn_full_jd_read} prints the caller; it exists because four scripts did it. "
        "Also {db.py::_fetch_all} OVERWRITES limit with its 1000-row page size, so asking it for "
        "one row walks the whole table -- copy {db.py::newest_event_ts} for a one-row select.",
        "python scripts/probe_db_proxy.py"),
    Row("B", "A column exists in code but not in the live database",
        "schema.sql, regenerated by asking Postgres to describe itself",
        "{db.py::JOBS_DERIVED_SQL} describes the same table a second time. resume_files and "
        "brain_companies are in db.py and the proxy allowlist but absent from schema.sql -- "
        "brain_companies deliberately, see the admin health check.",
        "python scripts/dump_schema.py"),

    # ---- the app surface --------------------------------------------------------------------
    Row("Y", "A POST returns 400, or a new form silently fails",
        "{web.py::_require_csrf} -- runs before EVERY cookie-authenticated write",
        "{web.py::_CSRF_EXEMPT} has four entries and each carries its justification. New routes "
        "are protected by default; adding to that set needs the same standard of reasoning.",
        "python scripts/smoke_app.py"),
    Row("R", "Analytics numbers look inflated, or a test wrote real events",
        "{analytics.py::emit} and the auto page_view in {web.py::_ev_page_view}",
        "EV_OFF=1 must be in the environment BEFORE anything imports web -- analytics.py reads "
        "it once, at import. One unguarded parity run wrote 98.8% of all recorded feed_view "
        "events. scripts/run_tests.py sets it per subprocess so this cannot recur.",
        "python scripts/run_tests.py --only card_meta"),
    Row("Y", "The Chrome extension stopped working, or says it is out of date",
        "{web.py::EXT_MIN_VERSION} -- the server's floor",
        "extension/manifest.json's version must match, and NOTHING enforces that. Bumping the "
        "manifest alone tells every install it is current. See also Known-wrong B2.",
        "python scripts/test_ext_contract.py"),
    Row("Y", "An aggregator relist reached the apply queue",
        "{web.py::_QUEUE_SKIP_HOSTS}",
        "a verbatim retyped copy of {core.py::AGGREGATOR_HOSTS}. Adding a sixth aggregator "
        "updates the feed's dedupe and misses the queue. No test covers this yet.",
        None),
    Row("B", "A job description renders badly -- lost headings, mangled lists",
        "{jdrender.py::render_jd} -- typed nodes, not markup, then one HTML pass",
        "byte-frozen against scripts/fixtures/jd_html_baseline.json. Regenerating that baseline "
        "needs a reviewed diff; it is a snapshot, so it will happily bless a regression.",
        "python scripts/test_jdrender.py"),
    Row("B", "The resume grader scores something oddly",
        "{resume_score.py::score_resume} -- ~25 checks behind a weight table",
        "{resume_keywords.py::evaluate} owns the Skills category and decides presence via "
        "core's term matcher, so it agrees with the feed's match %. Voice rules are shared with "
        "the AI rewriter, so the grader and the rewriter cannot disagree about a filler word.",
        "python test_resume_score.py"),
    Row("B", "Sponsorship colour or tier looks wrong on a card",
        "{core.py::visa_tags} for the posting's own claim, {core.py::sponsor_strength} for "
        "federal history",
        "absence of a record is NOT evidence of non-sponsorship, and the feed is never filtered "
        "on it. {core.py::is_cap_exempt} is the separate universities-and-hospitals path.",
        "python test_sponsor_flag.py"),
    Row("B", "A management role shows in a dev search, or vice versa",
        "{core.py::role_track} -- management wins ties, deliberately",
        "{core.py::roles_match} and ROLE_FAMILIES drive the role picker. "
        "{core.py::reads_like_pm} is the description-level rule with its own anchor/point gates.",
        "python test_pm_rule.py"),
    Row("B", "grep or ripgrep finds nothing in static/app.js",
        "a raw NUL byte at offset 46372 (line 733) makes ripgrep classify the file as binary and "
        "return no line numbers; git marks it -text for the same reason",
        "use `grep -a`, `git grep`, or the app.js section of docs/MAP.md. It is the second-most-"
        "coupled file in the repo, so this matters more than it sounds.",
        None),
)

# ---------------------------------------------------------------------------------------------
# KNOWN-WRONG. A symptom row is allowed to point at a bug instead of pretending there is a fix.
# ---------------------------------------------------------------------------------------------
KnownWrong = collections.namedtuple("KnownWrong", "tag title detail workaround")

KNOWN_WRONG = (
    KnownWrong(
        "B1", "_TITLE_STOP is defined twice and the live copy is the wrong one",
        "{web.py::_TITLE_STOP} is bound once for the similar-roles title index and again, much "
        "later in the same file, with a different word list for /admin/usage. Python resolves "
        "globals at call time, so {web.py::_title_tokens} -- which sits thousands of lines above "
        "the second binding -- uses the SECOND list. That list strips engineer, manager, "
        "analyst, specialist, associate, developer, director, intern, senior, sr, jr, junior and "
        "lead, which are exactly the tokens that distinguish one role from another, and it keeps "
        "job, role, position, remote, hybrid and usa, which the intended list removes. So "
        "\"Similar roles at other employers\" ranks on boilerplate and location, and the first "
        "definition has no effect at all.",
        "Rename one of them. Nothing depends on the collision."),
    KnownWrong(
        "B2", "/extension is advertised as the update URL and 404s",
        "{web.py::ext_version} returns request.host_url + \"/extension\" to every extension that "
        "checks its version, and no route serves that path -- there is no rule for it in web.py "
        "and no .htaccess in the repo. The \"your extension is stale, update here\" link is dead.",
        "Point it at the repo's extension/ directory, or add the route."),
    KnownWrong(
        "B3", "Two test suites could not fail (FIXED 2026-08-20)",
        "scripts/test_visa.py and scripts/test_notify.py printed \"N FAILED\" and then exited 0. "
        "Both run in python-tests.yml under `set -e`, so neither could ever have failed a build. "
        "Verified with a forced failure: identical stdout, exit 0 before and exit 1 after.",
        "Fixed. scripts/run_tests.py also treats a FAILED line as failure regardless of exit "
        "code, because the bug class is cheap to keep catching."),
    KnownWrong(
        "B4", "GitHub Actions cannot reach the database",
        "The Actions DB_PROXY_SECRET has not matched the server's since roughly 2026-08-15, so "
        "scheduled runs die on HTTP 401 {\"error\":\"bad signature\"}. verify_dates, the "
        "analytics rollup and the digest email have not run there since.",
        "Repost detection is duplicated into bin/cron_scrape.sh and THAT copy works -- do not "
        "\"clean up the duplicate\". Fix by resetting the repository secret to match "
        ".db_proxy_secret."),
    KnownWrong(
        "B5", "Seven employers have no board at all",
        "ASML, Deutsche Bank, LTIMindtree, Marlabs, Qualcomm, Renesas and Tradeweb were reachable "
        "only through Adzuna, which was removed on 2026-08-16. Their boards rows still say "
        "ats_type=\"adzuna\" and are inert.",
        "Each needs its real ATS tenant id; guessed Workday and Greenhouse tenants did not "
        "resolve. `python scripts/probe_adzuna_replacements.py` walks the detect chain."),
)

REF = re.compile(r"\{([^{}]+?)::([^{}]+?)\}")
BANNER = re.compile(r"^#\s*[-=]{3,}\s*(.*?)\s*[-=]*\s*$")
JS_FUNC = re.compile(r"^\s*function\s+([A-Za-z_$][\w$]*)", re.M)
JS_VAR = re.compile(r"^\s{0,4}(?:var|const|let)\s+([A-Za-z_$][\w$]*)\s*=", re.M)
# One optional leading underscore: module-private constants like _FEED_INLINE_MAX and
# _CSRF_EXEMPT are the idiom throughout web.py, and they are exactly the ones worth indexing.
UPPER = re.compile(r"^_?[A-Z][A-Z0-9_]*$")

Sym = collections.namedtuple("Sym", "name kind line end doc routes")
Section = collections.namedtuple("Section", "label start end")


def read(rel):
    """Read a source file without letting anything translate its newlines.

    errors="replace" because static/app.js contains a raw NUL; newline="" so the CRLF working
    copy is seen as it is on disk. Callers normalise before parsing.
    """
    with open(os.path.join(APP, rel), encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read().replace("\r\n", "\n")


def symbols(rel, src):
    """Top-level defs, classes, UPPER_SNAKE constants and Flask routes.

    Routes fall straight out of the AST: @app.route("/x", methods=[...]) is a Call whose func is
    an Attribute named route, so the path, the methods and the handler name all come from one
    walk with no regex and nothing to drift.
    """
    if rel.endswith(".js"):
        out = []
        for pat, kind in ((JS_FUNC, "function"), (JS_VAR, "var")):
            for m in pat.finditer(src):
                out.append(Sym(m.group(1), kind, src[:m.start()].count("\n") + 1, 0, "", ()))
        seen, uniq = set(), []
        for s in sorted(out, key=lambda s: s.line):
            if s.name not in seen:
                seen.add(s.name)
                uniq.append(s)
        return uniq
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print("  ! %s: %s" % (rel, e))
        return []
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            routes = []
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and dec.func.attr == "route" and dec.args
                        and isinstance(dec.args[0], ast.Constant)):
                    methods = ""
                    for kw in dec.keywords:
                        if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                            methods = ",".join(e.value for e in kw.value.elts
                                               if isinstance(e, ast.Constant))
                    routes.append((dec.args[0].value, methods or "GET"))
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            out.append(Sym(node.name,
                           "class" if isinstance(node, ast.ClassDef) else "def",
                           node.lineno, node.end_lineno or node.lineno,
                           " ".join(doc[0].split()) if doc else "",
                           tuple(routes)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and UPPER.match(t.id):
                    out.append(Sym(t.id, "const", node.lineno, node.lineno, "", ()))
    return out


def sections(src):
    """Turn the author's own banner comments into a navigation table.

    Two styles are in use and both matter: inline `# ---- label ----`, and a sandwich where a
    rule, a `# text` line and another rule surround the label. Lifting the existing banners
    rather than inventing a taxonomy means the map matches how the code is already organised.
    """
    lines = src.split("\n")
    found = []
    i = 0
    while i < len(lines):
        m = BANNER.match(lines[i])
        if not m:
            i += 1
            continue
        label = m.group(1).strip()
        if not label and i + 1 < len(lines):
            nxt = lines[i + 1]
            if nxt.startswith("#") and not BANNER.match(nxt):
                label = nxt.lstrip("#").strip()
                if i + 2 < len(lines) and BANNER.match(lines[i + 2]):
                    i += 2
        label = label.strip(" -=").strip()
        if label and len(label) < 130:
            found.append((i + 1, label))
        i += 1
    out = []
    for n, (line, label) in enumerate(found):
        end = found[n + 1][0] - 1 if n + 1 < len(found) else len(lines)
        out.append(Section(label, line, end))
    return out


def collect():
    """{rel: (src, [Sym], [Section])} for every mapped file, in reading order."""
    rels = [m[0] for m in MODULES]
    for pkg in PACKAGES:
        d = os.path.join(APP, pkg)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            rel = "%s/%s" % (pkg, f)
            if f.endswith(".py") and rel not in rels:
                rels.append(rel)
    data = collections.OrderedDict()
    for rel in rels:
        if not os.path.exists(os.path.join(APP, rel)):
            print("  ! mapped file missing: %s" % rel)
            continue
        src = read(rel)
        data[rel] = (src, symbols(rel, src), sections(src))
    return data


def resolve(text, data, errors, where):
    """Replace every {module::symbol} with `module:LINE` `symbol`.

    An unresolvable key is an error, not a silent passthrough -- that is the mechanism that
    keeps the hand-written prose honest.
    """
    def sub(m):
        rel, name = m.group(1), m.group(2)
        if rel not in data:
            errors.append("%s references unmapped file %s" % (where, rel))
            return "`%s::%s`" % (rel, name)
        hits = [s for s in data[rel][1] if s.name == name]
        if not hits:
            errors.append("%s names %s::%s -- not found" % (where, rel, name))
            return "`%s::%s` **(MISSING)**" % (rel, name)
        out = "[`%s:%d`](../%s#L%d) `%s`" % (rel, hits[0].line, rel, hits[0].line, name)
        if len(hits) > 1:
            # Bound more than once at module level. Python resolves globals at call time, so the
            # LAST binding is the one that runs -- flagging it inline is how B1 stops being
            # invisible.
            out += " (also at %s — **shadowed, the last one wins**)" % ", ".join(
                "[%d](../%s#L%d)" % (h.line, rel, h.line) for h in hits[1:])
        return out
    return REF.sub(sub, text)


def shadowed(data):
    """Names bound more than once at module level, last-wins.

    Not a style nit: web.py binds _TITLE_STOP twice with different word lists 2,500 lines apart,
    and the copy that runs is NOT the one sitting next to the code that uses it. That was found
    by reading; this finds it by construction.

    Two shapes have to be told apart or the table is noise. `X = tuple(f(p) for p in X)` is a
    deliberate compile-in-place -- the second binding CONSUMES the first, so nothing is lost.
    A rebind whose value never mentions the name is the dangerous kind. Returns
    (rel, name, lines, benign).
    """
    out = []
    for rel, (src, syms, _secs) in data.items():
        if rel.endswith(".js"):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        # line -> whether that assignment's value references the name being assigned
        selfref = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name):
                nm = node.targets[0].id
                selfref[node.lineno] = any(
                    isinstance(x, ast.Name) and x.id == nm for x in ast.walk(node.value))
        seen = collections.defaultdict(list)
        for s in syms:
            seen[s.name].append(s.line)
        for name, lines in sorted(seen.items()):
            if len(lines) > 1:
                benign = all(selfref.get(n) for n in lines[1:])
                out.append((rel, name, lines, benign))
    return out


def suite_names():
    """Suite names known to scripts/run_tests.py, for validating the guard column."""
    try:
        src = read("scripts/run_tests.py")
    except OSError:
        return set()
    return set(re.findall(r'Suite\("([^"]+)"', src))


def render_index(data, errors):
    SEV = {"R": ("\U0001F534", "changing this alone breaks production **silently**"),
           "Y": ("\U0001F7E1", "lives in two or more places that must agree"),
           "B": ("\U0001F535", "one place, safe to change, but non-obvious to find")}
    suites = suite_names()
    L = ["# Index — symptom to source",
         "",
         "<!-- GENERATED by scripts/build_docs.py. Do not edit: your changes will be "
         "overwritten and CI will fail. The prose lives in the INDEX tuple in that script, "
         "keyed by symbol so the line numbers here are always current. -->",
         "",
         "Start here when something is wrong. Every line number below is resolved from the code "
         "at generation time, so it is correct by construction rather than by anyone's diligence.",
         "",
         "| | meaning |", "|---|---|"]
    for k in ("R", "Y", "B"):
        L.append("| %s | %s |" % (SEV[k][0], SEV[k][1]))
    L += ["", "| | Symptom | Go here | Must stay in step with | Guard |",
          "|---|---|---|---|---|"]
    for n, r in enumerate(INDEX):
        where = "INDEX row %d (%s)" % (n + 1, r.symptom[:44])
        guard = r.guard or "*none — write one*"
        if r.guard:
            # Every script a guard names must exist, and if it is a test suite it must be in
            # run_tests.py's SUITES -- otherwise the index promises a check that CI never runs.
            for script in re.findall(r"[\w/]+\.py", r.guard):
                if not os.path.exists(os.path.join(APP, script)):
                    errors.append("%s guard names %s, which does not exist" % (where, script))
                    continue
                stem = os.path.basename(script)[:-3]
                if stem.startswith(("test_", "feed_", "smoke_", "verify_")) \
                        and stem not in suites:
                    errors.append("%s guard names %s, which is not in run_tests.py SUITES"
                                  % (where, script))
            guard = "`%s`" % r.guard
        L.append("| %s | %s | %s | %s | %s |" % (
            SEV[r.sev][0],
            resolve(r.symptom, data, errors, where),
            resolve(r.target, data, errors, where),
            resolve(r.coupled, data, errors, where),
            guard))
    L += ["", "## Known-wrong",
          "",
          "Things that are broken or misleading right now. A symptom above may point here "
          "instead of at a fix — that is deliberate, because an index you can't trust is worse "
          "than no index.",
          ""]
    for kw in KNOWN_WRONG:
        L += ["### %s — %s" % (kw.tag, kw.title), "",
              resolve(kw.detail, data, errors, "Known-wrong %s" % kw.tag), "",
              "**Workaround:** %s" % resolve(kw.workaround, data, errors,
                                             "Known-wrong %s" % kw.tag), ""]
    dup = shadowed(data)
    if dup:
        L += ["## Shadowed definitions", "",
              "Bound more than once at module level. Python resolves globals at call time, so the "
              "**last** binding is the one that runs — even if the code using it sits thousands "
              "of lines above. Found automatically, which is how B1 stopped being invisible.",
              "",
              "*Rebind* means the second binding consumes the first (`X = tuple(f(p) for p in "
              "X)`) — deliberate, nothing lost. *Shadow* means it does not, so the earlier "
              "definition is dead code and whoever wrote it did not get what they intended.",
              "", "| File | Name | Bound at | |", "|---|---|---|---|"]
        for rel, name, lines, benign in dup:
            L.append("| `%s` | `%s` | %s | %s |" % (
                rel, name, ", ".join("[%d](../%s#L%d)" % (n, rel, n) for n in lines),
                "rebind" if benign else "⚠ **shadow**"))
        L.append("")
    L += ["---", "",
          "Full symbol map: [MAP.md](MAP.md) · How it works: "
          "[ARCHITECTURE.md](ARCHITECTURE.md) · How to do things: "
          "[OPERATIONS.md](OPERATIONS.md)", ""]
    return "\n".join(L)


def render_map(data):
    total_sym = sum(len(v[1]) for v in data.values())
    L = ["# Map — every file, section and symbol",
         "",
         "<!-- GENERATED by scripts/build_docs.py. Do not edit. -->",
         "",
         "%d files, %d top-level symbols. Section names are the code's own banner comments, not "
         "a taxonomy invented for this document." % (len(data), total_sym),
         "",
         "Tier 1 answers *which 300-line neighbourhood of a 7,000-line file do I want*. Tier 2 is "
         "the appendix — Ctrl+F it.",
         "", "## Contents", ""]
    roles = dict(MODULES)
    for rel, (src, syms, secs) in data.items():
        L.append("- [`%s`](#%s) — %d lines, %d symbols%s"
                 % (rel, anchor(rel), src.count("\n") + 1, len(syms),
                    (" — " + roles[rel]) if rel in roles else ""))
    L += ["", "---", "", "# Tier 1 — sections", ""]
    for rel, (src, syms, secs) in data.items():
        L += ["## `%s`" % rel, ""]
        if rel in roles:
            L += ["*%s*" % roles[rel], ""]
        L.append("%d lines · %d top-level symbols · %d sections"
                 % (src.count("\n") + 1, len(syms), len(secs)))
        L.append("")
        if not secs:
            L += ["*No banner comments in this file.*", ""]
            continue
        L += ["| Lines | Section | Symbols |", "|---|---|---|"]
        for s in secs:
            n = sum(1 for y in syms if s.start <= y.line <= s.end)
            L.append("| [%d–%d](../%s#L%d) | %s | %d |"
                     % (s.start, s.end, rel, s.start, md(s.label), n))
        L.append("")
    routes = [(rel, y) for rel, (_s, syms, _c) in data.items() for y in syms if y.routes]
    if routes:
        L += ["---", "", "# Routes", "",
              "%d rules on %d handlers. No blueprints — this is one module."
              % (sum(len(y.routes) for _r, y in routes), len(routes)),
              "",
              "*Section* is the banner span a handler physically sits in, not a curated feature "
              "grouping — so a route added at the end of one section shows that section's name "
              "even if it belongs to another. Positional, and honest about it.", ""]
        rel0 = routes[0][0]
        secs0 = data[rel0][2]
        cur = None
        L += ["| Route | Methods | Handler | Section |", "|---|---|---|---|"]
        for rel, y in routes:
            sec = next((s.label for s in secs0 if s.start <= y.line <= s.end), "")
            for path, methods in y.routes:
                L.append("| `%s` | %s | [`%s:%d`](../%s#L%d) `%s` | %s |"
                         % (path, methods, rel, y.line, rel, y.line, y.name, md(sec)))
        L.append("")
    L += ["---", "", "# Tier 2 — every symbol", ""]
    for rel, (src, syms, secs) in data.items():
        # "— symbols" keeps this heading's anchor distinct from Tier 1's, so the Contents links
        # above land on the section table rather than whichever heading GitHub deduplicated.
        L += ["## `%s` — symbols" % rel, ""]
        if not syms:
            L += ["*none found.*", ""]
            continue
        L += ["| Symbol | Kind | Line | What |", "|---|---|---|---|"]
        for y in sorted(syms, key=lambda y: y.line):
            note = md(y.doc)[:150] if y.doc else ""
            if y.routes:
                note = ("`" + "` `".join(p for p, _m in y.routes) + "` " + note).strip()
            L.append("| `%s` | %s | [%d](../%s#L%d) | %s |"
                     % (y.name, y.kind, y.line, rel, y.line, note))
        L.append("")
    return "\n".join(L)


def md(s):
    return (s or "").replace("|", "\\|").replace("\r", " ").strip()


def anchor(rel, suffix=""):
    """GitHub's heading-anchor rules: lowercase, drop non-word chars, KEEP underscores.

    An earlier version mapped _ to -, which silently broke every link to scraper/__init__.py.
    """
    base = re.sub(r"[^\w-]", "", rel.replace("/", "").replace(".", "")).lower()
    return base + suffix


def write(rel, text, check, errors):
    """Compare or write. LF unconditionally -- see .gitattributes for why that is load-bearing."""
    path = os.path.join(APP, rel)
    old = None
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as fh:
            old = fh.read()
    rel = rel.replace(os.sep, "/")          # messages read the same on Windows and the runner
    if check:
        if old is None:
            errors.append("%s does not exist -- run: python scripts/build_docs.py" % rel)
        elif old != text:
            d = list(difflib.unified_diff(old.splitlines(), text.splitlines(),
                                          "committed", "regenerated", lineterm="", n=1))
            errors.append("%s is stale (%d diff lines). Run: python scripts/build_docs.py\n%s"
                          % (rel, len(d), "\n".join("    " + x for x in d[:20])))
        return False
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if old == text:
        print("unchanged %s" % os.path.abspath(path))
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("wrote %s" % os.path.abspath(path))
    return True


def unmapped():
    """Modules in the app root that nothing maps -- a soft nudge, never a failure."""
    mapped = {m[0] for m in MODULES} | {"app.py", "manage_users.py", "speedtest.py",
                                        "passenger_wsgi.py"}
    out = []
    for f in sorted(os.listdir(APP)):
        if f.endswith(".py") and f not in mapped and not f.startswith("test_"):
            out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser(description="Generate docs/INDEX.md and docs/MAP.md.")
    ap.add_argument("--check", action="store_true",
                    help="fail if the committed docs are stale or name something gone")
    ap.add_argument("--list-refs", action="store_true",
                    help="print every {module::symbol} the prose uses, resolved")
    args = ap.parse_args()

    data = collect()
    errors = []

    if args.list_refs:
        seen = set()
        for r in INDEX:
            for field in (r.symptom, r.target, r.coupled):
                for m in REF.finditer(field):
                    seen.add((m.group(1), m.group(2)))
        for kw in KNOWN_WRONG:
            for field in (kw.detail, kw.workaround):
                for m in REF.finditer(field):
                    seen.add((m.group(1), m.group(2)))
        bad = 0
        for rel, name in sorted(seen):
            line = None
            if rel in data:
                line = next((s.line for s in data[rel][1] if s.name == name), None)
            print("%-4s %-26s %-34s %s" % ("ok" if line else "MISS", rel, name,
                                           line if line else "-"))
            bad += 0 if line else 1
        print("\n%d refs, %d unresolved" % (len(seen), bad))
        return 1 if bad else 0

    idx = render_index(data, errors)
    mp = render_map(data)

    changed = write(INDEX_OUT, idx, args.check, errors)
    changed = write(MAP_OUT, mp, args.check, errors) or changed

    extra = unmapped()
    if extra:
        print("\nnote: %d module(s) in the app root are not in MODULES: %s"
              % (len(extra), ", ".join(extra)))

    if errors:
        print("\n%d problem(s):" % len(errors))
        for e in errors:
            print("  - %s" % e)
        if args.check:
            print("\n::error::docs are stale or inconsistent. Run "
                  "'python scripts/build_docs.py' and commit the result.")
        return 1
    print("\nok: %d files, %d symbols, %d index rows, %d known-wrong entries."
          % (len(data), sum(len(v[1]) for v in data.values()), len(INDEX), len(KNOWN_WRONG)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
