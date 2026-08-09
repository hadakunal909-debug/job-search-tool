#!/usr/bin/env python3
"""Filter + sort parity between the server feed and the client feed.

web.py's `_filter_rows` and app.js's `matches()` are deliberate twins: below FEED_INLINE_MAX the
browser filters the whole corpus itself, above it the server does, and the two paths have to
agree row for row or the feed silently changes behaviour at 4,000 jobs. Nothing but a test keeps
them in step.

This runs both implementations over one synthetic corpus (shaped like the real one — Amazon's
431 identical "Operations Manager" rows, Walmart's per-store pharmacy internships, an agency's
runs, plus the awkward singletons) across a matrix of filter settings, and diffs which rows
survive, in which order.

Those employer floods are kept even though the feed no longer collapses them into "+N more"
tiles: hundreds of rows tied on score is the best stable-sort-divergence probe in the file, and
it is exactly the shape that broke paging before.

The JS side runs in node. app.js is a DOM-bound IIFE, so rather than restructure it into modules
this extracts the pure functions by source text and evaluates them against stub controls — the
same functions the browser runs, byte for byte, so a divergence can't hide behind a copy.

    python scripts/feed_parity.py            # exit 0 = the twins agree

Synthetic, not live: it needs no Supabase and no résumé, so it can run in CI and on a laptop.
"""
import io
import os
import re
import sys
import json
import random
import datetime
import subprocess
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import web                                             # noqa: E402  (needs ROOT on sys.path)

APP_JS = os.path.join(ROOT, "static", "app.js")

# The pure predicate functions app.js and web.py mirror. Order matters only for readability —
# node hoists function declarations. Lifted from app.js BY SOURCE TEXT, so a name that no longer
# exists there is a hard SystemExit from js_function(), not a silent skip.
JS_FUNCS = ["locHit", "annualize", "rowDate", "visaWanted", "visaHit", "matches",
            # The sort comparator, lifted rather than re-typed. It used to be hand-copied into
            # DRIVER_MAIN below, which meant a third implementation nobody remembered to update
            # — exactly the drift this harness exists to catch, sitting inside the harness.
            "sponsorRank", "sortCmp"]

# _row() must emit exactly these. A field that _build_row produces but _row() forgets makes
# the parity run pass VACUOUSLY — the server sees None, JS sees undefined, both filter the
# same way, and the diff is empty no matter how badly the two implementations disagree.
ROW_KEYS = {
    "title", "company", "location", "loc_state", "loc_metro", "remote",
    "salary_min", "salary_max", "salary_period", "closed", "url", "score",
    "score_pending", "date", "date_verified", "first_seen", "sponsor_jd",
    "sponsors_h1b", "everify", "visa", "agency", "cap_exempt", "intern", "track",
    "exp_years", "exp_level",
    # Read only by the sponsor sort. Without them here the sponsor case would pass VACUOUSLY:
    # strength_n is None server-side and undefined in JS, both rank everything identically, and
    # the diff is empty however badly the two disagree.
    "strength", "strength_n",
}


# ----------------------------- the corpus -----------------------------
STATES = ["TX", "CA", "WA", "NY", "MN", "IL", "MA", "FL", "AZ", "", "OH", "GA"]
CITIES = {"TX": "Dallas, TX", "CA": "Sunnyvale, CA", "WA": "Seattle, WA", "NY": "New York, NY",
          "MN": "Minneapolis, MN", "IL": "Chicago, IL", "MA": "Boston, MA", "FL": "Tampa, FL",
          "AZ": "Phoenix, AZ", "": "United States", "OH": "Columbus, OH", "GA": "Atlanta, GA"}
METROS = {"WA": "Seattle", "MA": "Boston", "NY": "New York", "IL": "Chicago", "CA": "Bay Area"}


def _visa_for(n, sponsor_jd="", reason=""):
    """Tag list for row `n`, cycling all 32 subsets of the 5 routes, then narrowed by the
    posting's own sponsorship verdict exactly as _build_row narrows it."""
    mask = n % 32
    tags = [t for i, t in enumerate(web.core.VISA_TAGS) if mask & (1 << i)]
    return list(web.core.visa_tags_for_posting(tags, sponsor_jd, reason))


def _row(rng, n, title, company, state, **over):
    """One feed row in the exact shape _build_row emits (only the fields the filters read)."""
    r = {
        "title": title, "company": company,
        "location": CITIES.get(state, "United States"),
        "loc_state": state, "loc_metro": METROS.get(state, ""),
        "remote": False,
        "salary_min": None, "salary_max": None, "salary_period": "",
        "closed": False,
        "url": "https://boards.example.com/%s/%d" % (re.sub(r"\W+", "", company.lower()), n),
        # Deliberately coarse so ties are common: ties are where a stable-sort disagreement
        # between the two implementations would show up.
        "score": rng.choice([0, 12, 31, 45, 45, 52, 52, 68, 74, 91]),
        "score_pending": False,
        "date": (datetime.date.today() - datetime.timedelta(
            days=rng.choice([0, 1, 3, 9, 20, 29, 31, 64, 140]))).isoformat(),
        "date_verified": False,
        # Empty for every row except the undated cohort below — a normal row is filtered on
        # its posting date and never reaches the fallback.
        "first_seen": "",
        "sponsors_h1b": "",
        # USCIS approval volume, the sponsor sort's second key. Coarse and tie-heavy on purpose,
        # with 0 well represented: a tie inside a tier is where a stable-sort disagreement
        # between the two implementations surfaces, and 0 is the no-record case.
        "strength_n": rng.choice([0, 0, 0, 3, 90, 90, 240, 1100, 15542]),
        "agency": False, "cap_exempt": False, "intern": False,
        # Classified from the title exactly as _build_row does, so the track filter is
        # exercised against the real partition rather than a hand-written label.
        "track": web.core.role_track(title),
        "exp_years": rng.choice(["", "", 1, 3, 5, 7]), "exp_level": "",
    }
    # sponsor_jd and visa are COUPLED in _build_row (a JD that rules out sponsorship strips
    # the sponsorship routes), so derive them together here rather than independently — an
    # uncoupled fixture would let a real regression in that narrowing slip through.
    sj, reason = rng.choice([
        ("", ""), ("", ""), ("open", ""),
        ("blocked", "JD says no visa sponsorship"),          # keeps stem_opt
        ("blocked", "JD requires U.S. citizenship"),         # strips everything
        ("blocked", "JD requires a security clearance"),
    ])
    r["sponsor_jd"] = sj
    # Every one of the 32 tag combinations occurs, cycled by row index rather than randomised
    # so a failing case is reproducible.
    r["visa"] = _visa_for(n, sj, reason)
    r["everify"] = "stem_opt" in r["visa"]
    # Derived from strength_n by the same thresholds core.sponsor_strength uses, so the tier and
    # the count can never disagree the way two independent rng picks would.
    n_ = r["strength_n"]
    r["strength"] = "high" if n_ >= 1000 else "medium" if n_ >= 100 else "low" if n_ >= 1 else ""
    r.update(over)
    return r


def build_corpus():
    """Synthetic but shaped like the live corpus, employer floods and all.

    The floods are the point even now that the feed draws every posting its own card: hundreds
    of rows tied on score are the sharpest test of whether the two sorts stay stable together,
    and they are the shape that makes a paging off-by-one visible.
    """
    rng = random.Random(20260729)              # fixed: the whole point is a reproducible diff
    rows, n = [], 0

    def add(title, company, state, **over):
        nonlocal n
        n += 1
        rows.append(_row(rng, n, title, company, state, **over))

    # The three measured floods.
    for i in range(431):                       # Amazon, one title, many sites
        add("Operations Manager", "Amazon", STATES[i % len(STATES)])
    for i in range(144):                       # Walmart, one per store
        add("Pharmacy Pre-Grad Intern (WM)", "Walmart", STATES[i % 6], intern=True)
    for i in range(51):
        add("Project Manager", "Actalent", "MN", agency=True)
    for i in range(37):
        add("Project Manager", "Actalent", "IL", agency=True)
    for i in range(35):
        add("Environmental Project Manager", "Actalent", "NY", agency=True)

    # Runs of every small size, so a page boundary can land inside one.
    for size in (1, 2, 3, 4, 5):
        for i in range(size):
            add("Data Analyst %d" % size, "Boundary Co", STATES[i % len(STATES)])

    # Titles differing only by punctuation/case/spacing — the search filter lowercases both
    # sides, so these must all answer to a q= of "software engineer".
    add("Software Engineer", "Normalize Inc", "CA")
    add("software  engineer", "Normalize Inc", "WA")
    add("Software-Engineer", "Normalize Inc", "NY")
    add("SOFTWARE ENGINEER!", "Normalize Inc", "MA")
    add("Software Engineer II", "Normalize Inc", "CA")
    add("Software Engineer III", "Normalize Inc", "CA")

    # Employers that publish NO posting date (Tesla, and a ~180-row tail). These carry only a
    # first_seen, so both implementations have to fall back to it or the date filter diverges —
    # and before that fallback existed they passed EVERY "posted within" filter.
    def ago(days):
        return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()

    for k, age in enumerate([0, 0, 1, 2, 6, 8, 29, 31, 88, 91, 200]):
        for i in range(4):
            add("Production Associate %d" % k, "Undated Motors",
                STATES[(k + i) % len(STATES)], date="", first_seen=ago(age))
    # Neither date: must still survive every date filter, on both sides.
    for i in range(4):
        add("Mystery Role %d" % i, "No Dates Inc", "TX", date="", first_seen="")

    # A run entirely in one state, and one with no state at all — both have to survive the
    # location filter's two very different paths.
    for i in range(9):
        add("Store Manager", "OneState LLC", "OH")
    for i in range(6):
        add("Field Technician", "NoState Corp", "")

    # Missing title or company: nothing may crash on either.
    add("", "Ghost Co", "TX")
    add("Analyst", "", "TX")
    for i in range(5):
        add("", "", "TX")

    # Feature-flag rows so every filter has something to bite on.
    for i in range(40):
        st = STATES[i % len(STATES)]
        add("Remote Data Engineer", "Remote First", st, remote=True)
    for i in range(30):
        add("Paid Engineer", "Pays Well", STATES[i % 4],
            salary_min=95000 + i * 3000, salary_max=150000, salary_period="year")
    for i in range(8):
        add("Hourly Tech", "Pays Hourly", "AZ",
            salary_min=48, salary_max=61, salary_period="hour")
    for i in range(22):
        add("Summer Analyst", "Bank Co", STATES[i % 5], intern=True)
    for i in range(17):
        add("Closed Role", "Gone Inc", STATES[i % 3], closed=True)
    for i in range(60):                        # long tail of genuine one-offs
        add("Specialist %d" % i, "Company %d" % i, STATES[i % len(STATES)])

    # ranked_rows hands the filters a score-sorted list; both sides must start from the same order.
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def build_statuses(rows):
    """A plausible spread of liked/applied/hidden so the tab branches are covered."""
    rng = random.Random(7)
    st = {}
    for r in rows:
        v = rng.random()
        if v < 0.02:
            st[r["url"]] = "liked"
        elif v < 0.035:
            st[r["url"]] = "applied"
        elif v < 0.055:
            st[r["url"]] = "hidden"
    return st


# ----------------------------- the filter matrix -----------------------------
def build_cases():
    """Filter settings to compare under. Defaults first, then one knob at a time, then mixes."""
    base = {"tab": "recommended", "min": "45", "date": "30", "hideagency": "1"}
    cases = [("toolbar defaults", dict(base))]
    singles = [
        ("everything visible", {"min": "0", "date": "any", "hideagency": ""}),
        ("search: operations manager", {"q": "operations manager"}),
        ("search: amazon", {"q": "amazon"}),
        ("search: boston", {"q": "boston"}),
        ("search: no hits", {"q": "zzzznothing"}),
        ("loc: MA", {"loc": "MA"}),
        ("loc: boston", {"loc": "boston"}),
        ("loc: remote", {"loc": "remote"}),
        ("remote only", {"remote": "1"}),
        ("min salary 100k", {"minsal": "100000"}),
        ("min salary 60k", {"minsal": "60000"}),
        ("visa: h1b", {"visatags": "h1b", "min": "0"}),
        ("visa: green_card", {"visatags": "green_card", "min": "0"}),
        ("visa: stem_opt", {"visatags": "stem_opt", "min": "0"}),
        ("visa: e3", {"visatags": "e3", "min": "0"}),
        ("visa: h1b1", {"visatags": "h1b1", "min": "0"}),
        ("visa: h1b OR green_card", {"visatags": "h1b,green_card", "min": "0"}),
        ("visa: all five", {"visatags": "h1b,green_card,stem_opt,e3,h1b1", "min": "0"}),
        ("visa: junk is ignored", {"visatags": "nonsense,,h1b", "min": "0"}),
        ("visa: empty means no filter", {"visatags": "", "min": "0"}),
        ("hide no-sponsorship", {"hidenospon": "1"}),
        ("interns only", {"intern": "only"}),
        ("exclude interns", {"intern": "no"}),
        ("track: software & data", {"track": "dev", "min": "0", "date": "any"}),
        ("track: management", {"track": "mgmt", "min": "0", "date": "any"}),
        ("exp <=2 yrs", {"exp": "2"}),
        ("exp hide senior", {"exp": "senior"}),
        ("show closed", {"showclosed": "1"}),
        ("show agencies", {"hideagency": ""}),
        ("sort newest", {"sort": "newest"}),
        # The sponsor ladder is a 3-key sort (tier, then USCIS volume, then score), so it has
        # far more ways to disagree across the two implementations than the other two modes.
        # Run it wide open and against several filtered subsets.
        ("sort sponsor", {"sort": "sponsor"}),
        ("sort sponsor, min 0", {"sort": "sponsor", "min": "0"}),
        ("sort sponsor, min 0, any date", {"sort": "sponsor", "min": "0", "date": "any"}),
        # With hidenospon on, tier 4 is filtered out entirely — so this checks the ladder still
        # agrees when its bottom rung is absent.
        ("sort sponsor + hide blocked", {"sort": "sponsor", "hidenospon": "1", "min": "0"}),
        # Agencies carry sponsorship tags too, so unhiding them widens the tier spread.
        ("sort sponsor + agencies", {"sort": "sponsor", "hideagency": "", "min": "0",
                                     "date": "any"}),
        ("date any", {"date": "any"}),
        ("date 7d", {"date": "7"}),
        ("date 1d", {"date": "1"}),
        # The undated cohort: these hit the first_seen fallback rather than r["date"], and
        # each window straddles a couple of their ages so the boundary is really tested.
        ("undated rows, 1d", {"date": "1", "min": "0"}),
        ("undated rows, 7d", {"date": "7", "min": "0"}),
        ("undated rows, 30d", {"date": "30", "min": "0"}),
        ("undated rows, 90d + newest", {"date": "90", "sort": "newest", "min": "0"}),
        ("min 0", {"min": "0"}),
        ("min 68", {"min": "68"}),
    ]
    for name, over in singles:
        c = dict(base)
        c.update(over)
        cases.append((name, c))
    for t in ("liked", "applied", "hidden"):
        c = dict(base)
        c["tab"] = t
        cases.append(("tab: " + t, c))
        c2 = dict(c)
        c2["min"] = "0"
        c2["date"] = "any"
        cases.append(("tab: %s, unfiltered" % t, c2))
    mixes = [
        ("agencies + newest + any date", {"hideagency": "", "sort": "newest", "date": "any", "min": "0"}),
        ("search + loc + remote", {"q": "engineer", "loc": "WA", "remote": "1", "min": "0"}),
        ("interns + 90d + stem_opt", {"intern": "only", "date": "90", "visatags": "stem_opt", "min": "0"}),
        ("visa + loc + track", {"visatags": "h1b,e3", "loc": "MA", "track": "mgmt", "min": "0",
                                "date": "any"}),
        ("visa + newest + agencies", {"visatags": "green_card", "sort": "newest",
                                      "hideagency": "", "min": "0", "date": "any"}),
        ("salary + exp + closed", {"minsal": "60000", "exp": "5", "showclosed": "1", "min": "0"}),
        ("search amazon, all agencies, closed", {"q": "manager", "hideagency": "", "showclosed": "1",
                                                 "min": "0", "date": "any"}),
        ("dev track + remote + newest", {"track": "dev", "remote": "1", "sort": "newest",
                                         "min": "0", "date": "any"}),
        ("mgmt track + interns + agencies", {"track": "mgmt", "intern": "only", "hideagency": "",
                                             "min": "0", "date": "any"}),
        ("dev track + search + exp", {"track": "dev", "q": "engineer", "exp": "5", "min": "0"}),
    ]
    for name, over in mixes:
        c = dict(base)
        c.update(over)
        cases.append((name, c))
    return cases


# ----------------------------- the JS side -----------------------------
def js_function(src, name):
    """Lift `function <name>(...) {...}` out of app.js by brace matching.

    Naive on purpose — it counts braces without tokenizing strings or comments, which is safe
    for these functions (none embeds an unbalanced brace in a literal) and keeps the harness
    dependency-free. If it ever throws off, the parity diff will be nonsense rather than
    subtly wrong, and node will refuse to parse the driver.
    """
    m = re.search(r"(?m)^\s*function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise SystemExit("feed_parity: can't find function %s() in static/app.js" % name)
    i = src.index("{", m.end() - 1)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
    raise SystemExit("feed_parity: unbalanced braces reading %s() from app.js" % name)


def js_const(src, name):
    """Read a top-level `var NAME = <int>;` out of app.js (HOURS_PER_YEAR)."""
    m = re.search(r"var\s+" + re.escape(name) + r"\s*=\s*(\d+)", src)
    if not m:
        raise SystemExit("feed_parity: can't find var %s in static/app.js" % name)
    return int(m.group(1))


def js_json(src, name):
    """Read a top-level `var NAME = <json literal>;` out of app.js.

    Only works because those literals are written as strict JSON (double-quoted keys, no
    trailing commas) — see the comment above them in app.js. Lets us assert the JS and Python
    vocabularies are identical instead of trusting that nobody edited one of them.
    """
    m = re.search(r"var\s+" + re.escape(name) + r"\s*=\s*([\[{])", src)
    if not m:
        raise SystemExit("feed_parity: can't find var %s in static/app.js" % name)
    open_ch, close_ch = m.group(1), {"[": "]", "{": "}"}[m.group(1)]
    i, depth, instr, esc = m.start(1), 0, False, False
    while i < len(src):
        ch = src[i]
        if instr:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return json.loads(src[m.start(1):i + 1])
        i += 1
    raise SystemExit("feed_parity: unbalanced literal reading var %s from app.js" % name)


DRIVER_PREAMBLE = """\
// Generated by scripts/feed_parity.py — do not edit.
// Stubs standing in for app.js's DOM controls, so the real predicate functions can run under
// node exactly as the browser runs them.
var HOURS_PER_YEAR = %(hours)d;
var tab = "recommended", minVal = 0, sortBy = "score", COMPANY = "";
function ctl(v) { return { value: v }; }
function chk(v) { return { checked: !!v }; }
var q = ctl(""), dateSel = ctl("any"), expSel = ctl("any"), internSel = ctl("any"),
    trackSel = ctl("any"),
    locInp = ctl(""), minSalSel = ctl(""), sortSel = ctl("score"),
    hideNo = chk(false), visaSel = ctl(""), remoteOnly = chk(false),
    hideAgency = chk(false), showClosed = chk(false);
var VISA_TAGS = %(visa_tags)s;
"""

DRIVER_MAIN = """
var IN = JSON.parse(require("fs").readFileSync(process.argv[2], "utf8"));
var out = [];
IN.cases.forEach(function (cs) {
  var p = cs.params;
  tab = p.tab || "recommended";
  minVal = parseInt(p.min || 0, 10) || 0;
  sortBy = p.sort || "score";
  q.value = p.q || "";
  dateSel.value = p.date || "any";
  expSel.value = p.exp || "any";
  internSel.value = p.intern || "any";
  trackSel.value = p.track || "any";
  locInp.value = p.loc || "";
  minSalSel.value = p.minsal || "";
  hideNo.checked = p.hidenospon === "1";
  visaSel.value = p.visatags || "";
  remoteOnly.checked = p.remote === "1";
  hideAgency.checked = p.hideagency === "1";
  showClosed.checked = p.showclosed === "1";

  // `cut` comes from the Python side rather than app.js's dateCutoff(): that function reads
  // UTC (toISOString) while _date_cutoff reads the local date, so feeding one value in keeps
  // this a test of the FILTER, not of a timezone. dateCutoff() is compared separately below.
  var matched = IN.rows.filter(function (j) { return matches(j, cs.cut); });
  // sortCmp is lifted from app.js by JS_FUNCS, not re-typed here. It used to be copied inline,
  // which made the parity harness itself carry a third implementation of the comparator.
  matched.sort(function (a, b) { return sortCmp(a, b, sortBy); });
  out.push({ name: cs.name, urls: matched.map(function (j) { return j.url; }) });
});
process.stdout.write(JSON.stringify(out));
"""


def run_js(rows, cases, cuts, scratch):
    src = io.open(APP_JS, encoding="utf-8").read()
    hours = js_const(src, "HOURS_PER_YEAR")
    if hours != web._HOURS_PER_YEAR:
        raise SystemExit("feed_parity: HOURS_PER_YEAR is %d in app.js but %d in web.py"
                         % (hours, web._HOURS_PER_YEAR))
    # The card, the digest and the server filter all key off this vocabulary. If app.js and
    # core.py ever disagree the badges quietly mislabel routes, and no row-level diff would
    # catch it — so compare them directly.
    js_tags = js_json(src, "VISA_TAGS")
    if tuple(js_tags) != tuple(web.core.VISA_TAGS):
        raise SystemExit("feed_parity: VISA_TAGS is %r in app.js but %r in core.py"
                         % (js_tags, list(web.core.VISA_TAGS)))
    js_labels = js_json(src, "VISA_LABELS")
    if js_labels != dict(web.core.VISA_TAG_LABELS):
        raise SystemExit("feed_parity: VISA_LABELS differs between app.js and core.py:\n  js=%r\n  py=%r"
                         % (js_labels, dict(web.core.VISA_TAG_LABELS)))
    driver = DRIVER_PREAMBLE % {"hours": hours, "visa_tags": json.dumps(js_tags)}
    for fn in JS_FUNCS:
        driver += "\n" + js_function(src, fn) + "\n"
    driver += DRIVER_MAIN

    dpath = os.path.join(scratch, "_parity_driver.js")
    ipath = os.path.join(scratch, "_parity_input.json")
    with io.open(dpath, "w", encoding="utf-8") as f:
        f.write(driver)
    payload = {"rows": [dict(r, status=st) for r, st in rows],
               "cases": [{"name": n, "params": p, "cut": cuts[n]} for n, p in cases]}
    with io.open(ipath, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    res = subprocess.run(["node", dpath, ipath], capture_output=True, text=True)
    if res.returncode != 0:
        raise SystemExit("feed_parity: node failed\n" + (res.stderr or "")[:4000])
    return {c["name"]: c for c in json.loads(res.stdout)}


# ----------------------------- the diff -----------------------------
def first_diff(a, b):
    """Where two sequences part ways, as a short human-readable note."""
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return "index %d: server %r vs client %r" % (i, x, y)
    if len(a) != len(b):
        side = "server" if len(a) > len(b) else "client"
        extra = (a if len(a) > len(b) else b)[min(len(a), len(b))]
        return "%s has %d extra, first is %r" % (side, abs(len(a) - len(b)), extra)
    return "identical"


# ----------------------------- anti-vacuity -----------------------------
def check_not_vacuous(rows, statuses, cases):
    """Guard against the failure mode this harness is blind to by construction.

    The parity diff compares two implementations against each other. When a field is missing
    from _row(), the server reads None and JS reads undefined — both drop (or keep) every row
    identically, the diff is empty, and the run prints PASS however wrong the filter is.

    Two cheap structural checks close that hole for every case, past and future:
      1. _row() emits exactly the field set _build_row does.
      2. Every filter case matches somewhere between 1 and N-1 rows. A case that matches all
         of them or none of them isn't exercising its filter.
    """
    fails = 0
    got = set(_row(random.Random(0), 0, "Operations Manager", "Acme", "MA"))
    missing, extra = ROW_KEYS - got, got - ROW_KEYS
    if missing or extra:
        print("FAIL _row() field set drifted from ROW_KEYS: missing=%s extra=%s"
              % (sorted(missing) or "-", sorted(extra) or "-"))
        fails += 1

    total = len(rows)
    for name, params in cases:
        # "no hits" and the empty-by-design tabs are legitimately allowed to match nothing.
        if "no hits" in name or params.get("q") == "zzzznothing":
            continue
        n = len(web._filter_rows(rows, statuses, params))
        if n == 0:
            print("FAIL case %-36s matched 0 of %d rows — filter untested" % (name, total))
            fails += 1
        elif n == total and params.get("min") == "0" and "everything visible" not in name:
            print("FAIL case %-36s matched ALL %d rows — filter untested" % (name, total))
            fails += 1
    print("anti-vacuity: %s" % ("all cases discriminate" if not fails else "%d problem(s)" % fails))
    return fails


# ----------------------------- the real routes -----------------------------
def check_routes(rows, statuses, cases):
    """Drive /api/feed through Flask, with the DB-backed providers stubbed.

    Paging is where the feed could quietly break, so this walks every page and reassembles them
    rather than trusting one. It also covers /api/feed's `company` narrowing, which the row diff
    above deliberately cannot see: that clause is server-only (it runs before _filter_rows, so
    matches() has no twin to compare against), which means nothing else in this file tests it.
    """
    orig = (web.ranked_rows, web.user_statuses, web.current_profile, web._accounts)
    web.ranked_rows = lambda u, r: rows
    web.user_statuses = lambda u: statuses
    web.current_profile = lambda: "parity harness profile"
    # login_required re-checks that the signed-in account still exists and isn't disabled, so a
    # synthetic "parity" user is now correctly bounced to /login and every route returns 302.
    # That app behaviour is deliberate; the harness just has to present an account that exists,
    # the same way it already presents rows and statuses.
    web._accounts = lambda force=False: {"parity": {"username": "parity"}}
    fails = 0
    try:
        web.app.config["TESTING"] = True
        client = web.app.test_client()
        with client.session_transaction() as s:
            s["user"] = "parity"
        for name, params in cases:
            matched = web._filter_rows(rows, statuses, params)
            qs = "&".join(["%s=%s" % (k, v) for k, v in params.items() if v != ""])
            note = "route     %-30s" % name

            # Walk /api/feed page by page and rebuild the whole list.
            got, offset, guard = [], 0, 0
            while True:
                guard += 1
                if guard > 400:
                    print("FAIL %s /api/feed paging did not terminate" % note)
                    fails += 1
                    break
                res = client.get("/api/feed?%s&offset=%d&limit=25" % (qs, offset))
                if res.status_code != 200:
                    print("FAIL %s /api/feed -> HTTP %d" % (note, res.status_code))
                    fails += 1
                    break
                d = res.get_json()
                got.extend(r["url"] for r in d["rows"])
                if d["total"] != len(matched):
                    print("FAIL %s total %d is not the matching JOB count %d"
                          % (note, d["total"], len(matched)))
                    fails += 1
                if not d["has_more"]:
                    break
                offset += 25
            want = [r["url"] for r, _ in matched]
            if got != want:
                print("FAIL %s reassembled pages differ (%d vs %d rows)" % (note, len(got), len(want)))
                fails += 1
            # Paging must never repeat a row — the property the old grouping invariants covered.
            if len(set(got)) != len(got):
                print("FAIL %s a row is rendered more than once across pages" % note)
                fails += 1

        # The /company narrowing. Server-only by design (see the docstring), so assert it
        # directly rather than adding a `company` case above, which the JS diff would fail on.
        co = "Actalent"
        ckey = web.db.block_key(co)
        res = client.get("/api/feed?tab=recommended&min=0&company=%s&offset=0&limit=25"
                         % quote(co, safe=""))
        if res.status_code != 200:
            print("FAIL route     company narrowing -> HTTP %d" % res.status_code)
            fails += 1
        else:
            d = res.get_json()
            stray = [r["company"] for r in d["rows"] if web.db.block_key(r["company"]) != ckey]
            if stray:
                print("FAIL route     /api/feed?company= leaked %r" % stray[:3])
                fails += 1
            elif not d["rows"]:
                print("FAIL route     /api/feed?company=%s matched nothing (vacuous)" % co)
                fails += 1
    finally:
        web.ranked_rows, web.user_statuses, web.current_profile, web._accounts = orig
    print("routes:     %s" % ("/api/feed pages + company narrowing agree"
                              if not fails else "%d failure(s)" % fails))
    return fails


def main():
    rows = build_corpus()
    statuses = build_statuses(rows)
    cases = build_cases()
    pairs = [(r, statuses.get(r["url"], "")) for r in rows]

    print("corpus: %d rows, %d with a status, %d filter cases\n"
          % (len(rows), len(statuses), len(cases)))

    cuts = {name: web._date_cutoff(p.get("date")) for name, p in cases}
    scratch = os.environ.get("TEMP") or os.environ.get("TMPDIR") or "."
    js = run_js(pairs, cases, cuts, scratch)

    fails = 0
    for name, params in cases:
        matched = web._filter_rows(rows, statuses, params)
        py_urls = [r["url"] for r, _ in matched]

        got = js.get(name)
        if got is None:
            print("FAIL %-38s client produced no result" % name)
            fails += 1
            continue
        if py_urls == got["urls"]:
            print("ok   %-38s %5d jobs" % (name, len(py_urls)))
            continue
        fails += 1
        print("FAIL %-38s" % name)
        print("       filters: %d vs %d rows — %s"
              % (len(py_urls), len(got["urls"]), first_diff(py_urls, got["urls"])))

    # dateCutoff() used to build its cutoff from toISOString() (UTC) while web.py's
    # _date_cutoff uses the local date, so for the hours when those two dates differ the client
    # cut one day more than the server — measured as a 424-vs-410 split. That's since been fixed
    # to format local date parts, so this now guards against a regression rather than reporting a
    # known gap. Comments are stripped before the check: the fix explains itself by naming
    # toISOString() in a comment, which a naive search reads as the bug still being there.
    src = io.open(APP_JS, encoding="utf-8").read()
    body = re.sub(r"//[^\n]*", "", js_function(src, "dateCutoff"))
    date_regression = bool(re.search(r"toISOString\s*\(\s*\)", body))
    if date_regression:
        print("\nWARNING: dateCutoff() is back on toISOString() (UTC) while _date_cutoff uses the"
              "\n         local date — the 'posted within' cutoff will disagree by a day for part"
              "\n         of each day. Format local date parts instead.")

    print("\n%d/%d filter cases agree." % (len(cases) - fails, len(cases)))
    fails += 1 if date_regression else 0
    fails += check_not_vacuous(rows, statuses, cases)
    fails += check_routes(rows, statuses, cases)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
