#!/usr/bin/env python3
"""Filter + grouping parity between the server feed and the client feed.

web.py's `_filter_rows`/`_group_units` and app.js's `matches()`/`groupUnits()` are deliberate
twins: below FEED_INLINE_MAX the browser filters the whole corpus itself, above it the server
does, and the two paths have to agree row for row or the feed silently changes behaviour at
4,000 jobs. Nothing but a test keeps them in step.

This runs both implementations over one synthetic corpus (shaped like the real one — Amazon's
431 identical "Operations Manager" rows, Walmart's per-store pharmacy internships, an agency's
runs, plus the awkward singletons) across a matrix of filter settings, and diffs:

  1. which rows survive the filters      (_filter_rows  vs matches)
  2. the display units grouping produces (_group_units  vs groupUnits)

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

# The pure predicate/grouping functions app.js and web.py mirror. Order matters only for
# readability — node hoists function declarations.
JS_FUNCS = ["groupKey", "pickLeaders", "groupUnits", "flatUnits", "groupingOn",
            "locHit", "annualize", "matches"]


# ----------------------------- the corpus -----------------------------
STATES = ["TX", "CA", "WA", "NY", "MN", "IL", "MA", "FL", "AZ", "", "OH", "GA"]
CITIES = {"TX": "Dallas, TX", "CA": "Sunnyvale, CA", "WA": "Seattle, WA", "NY": "New York, NY",
          "MN": "Minneapolis, MN", "IL": "Chicago, IL", "MA": "Boston, MA", "FL": "Tampa, FL",
          "AZ": "Phoenix, AZ", "": "United States", "OH": "Columbus, OH", "GA": "Atlanta, GA"}
METROS = {"WA": "Seattle", "MA": "Boston", "NY": "New York", "IL": "Chicago", "CA": "Bay Area"}


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
        "sponsor_jd": rng.choice(["", "", "open", "blocked"]),
        "sponsors_h1b": "", "everify": rng.random() < 0.35,
        "agency": False, "cap_exempt": False, "intern": False,
        # Classified from the title exactly as _build_row does, so the track filter is
        # exercised against the real partition rather than a hand-written label.
        "track": web.core.role_track(title),
        "exp_years": rng.choice(["", "", 1, 3, 5, 7]), "exp_level": "",
    }
    r.update(over)
    return r


def build_corpus():
    """Synthetic but shaped like the live corpus, including the runs that motivated grouping."""
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

    # Group sizes straddling _GROUP_MIN, so the "don't collapse a tile that hides one row"
    # boundary is exercised from both sides.
    for size in (1, 2, 3, 4, 5):
        for i in range(size):
            add("Data Analyst %d" % size, "Boundary Co", STATES[i % len(STATES)])

    # Titles that differ ONLY by punctuation/case/spacing must land in one group...
    add("Software Engineer", "Normalize Inc", "CA")
    add("software  engineer", "Normalize Inc", "WA")
    add("Software-Engineer", "Normalize Inc", "NY")
    add("SOFTWARE ENGINEER!", "Normalize Inc", "MA")
    # ...while a genuinely different level must not.
    add("Software Engineer II", "Normalize Inc", "CA")
    add("Software Engineer III", "Normalize Inc", "CA")

    # A run where every member shares one state: the leader picker must fall back to rank order.
    for i in range(9):
        add("Store Manager", "OneState LLC", "OH")
    # A run whose state is blank throughout ("" is a state like any other to the picker).
    for i in range(6):
        add("Field Technician", "NoState Corp", "")

    # Rows that must never group: missing title or company.
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
        ("e-verify only", {"everify": "1"}),
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
        ("date any", {"date": "any"}),
        ("date 7d", {"date": "7"}),
        ("date 1d", {"date": "1"}),
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
        ("interns + 90d + e-verify", {"intern": "only", "date": "90", "everify": "1", "min": "0"}),
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


DRIVER_PREAMBLE = """\
// Generated by scripts/feed_parity.py — do not edit.
// Stubs standing in for app.js's DOM controls, so the real predicate/grouping functions can run
// under node exactly as the browser runs them.
var GROUP_LEAD = %(group_lead)d, GROUP_MIN = %(group_min)d, HOURS_PER_YEAR = %(hours)d;
var tab = "recommended", minVal = 0, sortBy = "score";
function ctl(v) { return { value: v }; }
function chk(v) { return { checked: !!v }; }
var q = ctl(""), dateSel = ctl("any"), expSel = ctl("any"), internSel = ctl("any"),
    trackSel = ctl("any"),
    locInp = ctl(""), minSalSel = ctl(""), sortSel = ctl("score"),
    hideNo = chk(false), everifyOnly = chk(false), remoteOnly = chk(false),
    hideAgency = chk(false), showClosed = chk(false);
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
  everifyOnly.checked = p.everify === "1";
  remoteOnly.checked = p.remote === "1";
  hideAgency.checked = p.hideagency === "1";
  showClosed.checked = p.showclosed === "1";

  // `cut` comes from the Python side rather than app.js's dateCutoff(): that function reads
  // UTC (toISOString) while _date_cutoff reads the local date, so feeding one value in keeps
  // this a test of the FILTER, not of a timezone. dateCutoff() is compared separately below.
  var matched = IN.rows.filter(function (j) { return matches(j, cs.cut); });
  matched.sort(function (a, b) {
    if (sortBy === "newest") return (b.date || "").localeCompare(a.date || "");
    return (b.score || 0) - (a.score || 0);
  });
  var units = groupingOn() ? groupUnits(matched) : flatUnits(matched);
  out.push({
    name: cs.name,
    urls: matched.map(function (j) { return j.url; }),
    units: units.map(function (u) { return [u.row.url, u.more || 0, u.key || ""]; })
  });
});
process.stdout.write(JSON.stringify(out));
"""


def run_js(rows, cases, cuts, scratch):
    src = io.open(APP_JS, encoding="utf-8").read()
    hours = js_const(src, "HOURS_PER_YEAR")
    if hours != web._HOURS_PER_YEAR:
        raise SystemExit("feed_parity: HOURS_PER_YEAR is %d in app.js but %d in web.py"
                         % (hours, web._HOURS_PER_YEAR))
    driver = DRIVER_PREAMBLE % {"group_lead": web._GROUP_LEAD, "group_min": web._GROUP_MIN,
                                "hours": hours}
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


# ----------------------------- invariants -----------------------------
def check_invariants(rows, statuses, cases):
    """Properties grouping must hold regardless of what the client does.

    The first is the one the feed header depends on: every matching job is either drawn as a card
    or counted by a tile, so "N of M jobs" keeps meaning jobs even though fewer cards are drawn.
    """
    fails = 0
    for name, params in cases:
        matched = web._filter_rows(rows, statuses, params)
        if not web._grouping_on(params):
            continue
        units = web._group_units(matched)
        note = "invariant %-30s" % name

        # 1. Nothing lost, nothing double-counted: cards drawn + rows the tiles stand for
        #    equals the jobs that matched.
        drawn, hidden = len(units), sum(more for _, _, more, _ in units)
        if drawn + hidden != len(matched):
            print("FAIL %s cards %d + hidden %d != %d matched" % (note, drawn, hidden, len(matched)))
            fails += 1

        # 2. No row is rendered twice.
        seen = [r["url"] for r, _, _, _ in units]
        if len(set(seen)) != len(seen):
            print("FAIL %s a row is rendered more than once" % note)
            fails += 1

        # 3. A tile exists iff its group was big enough, and it accounts for its whole group.
        by_key = {}
        for pr in matched:
            k = web._group_key(pr[0])
            if k:
                by_key.setdefault(k, []).append(pr)
        tiles = {gk: more for _, _, more, gk in units if gk}
        for k, members in by_key.items():
            want_tile = len(members) >= web._GROUP_MIN
            if want_tile != (k in tiles):
                print("FAIL %s group %r size %d: tile=%s expected=%s"
                      % (note, k, len(members), k in tiles, want_tile))
                fails += 1
                continue
            if not want_tile:
                continue
            leaders = web._pick_leaders(members, web._GROUP_LEAD)
            lead_urls = {m[0]["url"] for m in leaders}
            rest = [p for p in members if p[0]["url"] not in lead_urls]
            if len(lead_urls) != len(leaders):
                print("FAIL %s group %r picked the same leader twice" % (note, k))
                fails += 1
            if tiles[k] != len(rest):
                print("FAIL %s group %r tile says %d, expansion has %d"
                      % (note, k, tiles[k], len(rest)))
                fails += 1
            # 4. Leaders should come from different states whenever the group offers them.
            states = {(m[0].get("loc_state") or "").upper() for m in members}
            lstates = [(m[0].get("loc_state") or "").upper() for m in leaders]
            if len(states) >= len(leaders) and len(set(lstates)) != len(leaders):
                print("FAIL %s group %r has %d states but leaders are %r"
                      % (note, k, len(states), lstates))
                fails += 1
    print("invariants: %s" % ("all hold" if not fails else "%d violation(s)" % fails))
    return fails


# ----------------------------- the real routes -----------------------------
def check_routes(rows, statuses, cases):
    """Drive /api/feed and /api/group through Flask, with the DB-backed providers stubbed.

    Paging is where grouping could quietly break: /api/feed now pages over display units while
    still reporting a job count, and /api/group has to hand back exactly the rows a tile stands
    for. Walking every page and reassembling them is the only way to see that end to end.
    """
    orig = (web.ranked_rows, web.user_statuses, web.current_profile)
    web.ranked_rows = lambda u, r: rows
    web.user_statuses = lambda u: statuses
    web.current_profile = lambda: "parity harness profile"
    fails = 0
    try:
        web.app.config["TESTING"] = True
        client = web.app.test_client()
        with client.session_transaction() as s:
            s["user"] = "parity"
        for name, params in cases:
            matched = web._filter_rows(rows, statuses, params)
            expect = web._group_units(matched) if web._grouping_on(params) \
                else [(r, st, 0, "") for r, st in matched]
            qs = "&".join(["%s=%s" % (k, v) for k, v in params.items() if v != ""])
            note = "route     %-30s" % name

            # Walk /api/feed page by page and rebuild the whole unit list.
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
                got.extend((r["url"], r.get("group_more") or 0, r.get("group_key") or "")
                           for r in d["rows"])
                if d["total"] != len(matched):
                    print("FAIL %s total %d is not the matching JOB count %d"
                          % (note, d["total"], len(matched)))
                    fails += 1
                if d["units"] != len(expect):
                    print("FAIL %s units %d != %d" % (note, d["units"], len(expect)))
                    fails += 1
                if not d["has_more"]:
                    break
                offset += 25
            want = [(r["url"], more, gk) for r, _, more, gk in expect]
            if got != want:
                print("FAIL %s reassembled pages differ (%d vs %d units)" % (note, len(got), len(want)))
                fails += 1

            # Every tile's expansion must be exactly the rows it hides — in order, once each.
            for _, _, more, gk in expect:
                if not gk:
                    continue
                rest, offset, guard = [], 0, 0
                while True:
                    guard += 1
                    if guard > 400:
                        print("FAIL %s /api/group paging did not terminate" % note)
                        fails += 1
                        break
                    res = client.get("/api/group?%s&gk=%s&offset=%d&limit=25"
                                     % (qs, quote(gk, safe=""), offset))
                    if res.status_code != 200:
                        print("FAIL %s /api/group -> HTTP %d" % (note, res.status_code))
                        fails += 1
                        break
                    d = res.get_json()
                    rest.extend(r["url"] for r in d["rows"])
                    if d["total"] != more:
                        print("FAIL %s /api/group total %d != tile's %d" % (note, d["total"], more))
                        fails += 1
                    if not d["has_more"]:
                        break
                    offset += 25
                members = [p for p in web._filter_rows(rows, statuses, params)
                           if web._group_key(p[0]) == gk]
                leaders = {m[0]["url"] for m in web._pick_leaders(members, web._GROUP_LEAD)}
                want_rest = [p[0]["url"] for p in members if p[0]["url"] not in leaders]
                if rest != want_rest:
                    print("FAIL %s expansion of %r differs (%d vs %d)"
                          % (note, gk, len(rest), len(want_rest)))
                    fails += 1
    finally:
        web.ranked_rows, web.user_statuses, web.current_profile = orig
    print("routes:     %s" % ("/api/feed + /api/group agree with the units"
                              if not fails else "%d failure(s)" % fails))
    return fails


def main():
    rows = build_corpus()
    statuses = build_statuses(rows)
    cases = build_cases()
    pairs = [(r, statuses.get(r["url"], "")) for r in rows]

    print("corpus: %d rows, %d with a status, %d filter cases"
          % (len(rows), len(statuses), len(cases)))
    if web._GROUP_LEAD > 0:
        print("grouping: FEED_GROUP_LEAD=%d (collapse a (title, company) run at %d+)\n"
              % (web._GROUP_LEAD, web._GROUP_MIN))
    else:
        print("grouping: FEED_GROUP_LEAD=0 — grouping disabled, every row gets a card\n")

    cuts = {name: web._date_cutoff(p.get("date")) for name, p in cases}
    scratch = os.environ.get("TEMP") or os.environ.get("TMPDIR") or "."
    js = run_js(pairs, cases, cuts, scratch)

    fails = 0
    for name, params in cases:
        matched = web._filter_rows(rows, statuses, params)
        py_urls = [r["url"] for r, _ in matched]
        if web._grouping_on(params):
            units = web._group_units(matched)
        else:
            units = [(r, st, 0, "") for r, st in matched]
        py_units = [[r["url"], more, gk] for r, _, more, gk in units]

        got = js.get(name)
        if got is None:
            print("FAIL %-38s client produced no result" % name)
            fails += 1
            continue
        ok_f = py_urls == got["urls"]
        ok_g = py_units == got["units"]
        if ok_f and ok_g:
            collapsed = len(py_urls) - len(py_units)
            print("ok   %-38s %5d jobs -> %5d cards%s"
                  % (name, len(py_urls), len(py_units),
                     ("  (%d collapsed)" % collapsed) if collapsed else ""))
            continue
        fails += 1
        print("FAIL %-38s" % name)
        if not ok_f:
            print("       filters: %d vs %d rows — %s"
                  % (len(py_urls), len(got["urls"]), first_diff(py_urls, got["urls"])))
        if not ok_g:
            print("       grouping: %d vs %d units — %s"
                  % (len(py_units), len(got["units"]), first_diff(py_units, got["units"])))

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

    print("\n%d/%d filter+grouping cases agree." % (len(cases) - fails, len(cases)))
    fails += 1 if date_regression else 0
    fails += check_invariants(rows, statuses, cases)
    fails += check_routes(rows, statuses, cases)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
