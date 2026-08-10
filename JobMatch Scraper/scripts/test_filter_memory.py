#!/usr/bin/env python3
"""The feed's filter memory, exercised against the REAL app.js source.

The toolbar's only store used to be the DOM, so any navigation threw unsaved filters away.
saveFilterState/applyFilterState persist it in localStorage. Both are closures inside app.js's
IIFE, so this lifts them out by source text — the same trick scripts/feed_parity.py uses — and
runs them in node against a stub DOM. Lifting rather than re-typing is the point: a copy here
would be a second implementation that could drift from the one that ships.

    python scripts/test_filter_memory.py        (needs node on PATH)

The cases that matter most are the two that would silently corrupt state rather than fail
loudly: a partial save from the company page wiping the feed's filters, and a stored blob from
an older schema being half-applied.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from feed_parity import js_function          # reuse the lifter, don't re-type it

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(APP, "static", "app.js"), encoding="utf-8").read()

# Also assert the persistence call site exists, since the whole design rests on every change
# listener reaching it through render() -> markFilters().
FAILS = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print("  %s %s%s" % ("ok " if cond else "FAIL", name, ("  " + extra) if extra else ""))


check("markFilters() calls saveFilterState()",
      "saveFilterState();" in js_function(SRC, "markFilters"))
check("applyFilterState() runs before sortBy/minVal are read",
      SRC.index("var SAVED = applyFilterState();") < SRC.index("var minVal = minR"))

DRIVER = """
// ---- stubs -------------------------------------------------------------------
var STORE = {};
var localStorage = {
  getItem: function (k) { return Object.prototype.hasOwnProperty.call(STORE, k) ? STORE[k] : null; },
  setItem: function (k, v) { STORE[k] = String(v); },
};
function ctl(v, type) { return { value: v, type: type || "text", checked: v === true }; }
function box(v) { return { type: "checkbox", checked: !!v }; }

var IN = %(input)s;
var PAGE = IN.page;                       // "feed" | "company"
var feed = { getAttribute: function (a) { return a === "data-user" ? IN.user : null; } };

// Controls present on this page. company.html renders only #q and #sort.
var q, minR, sortSel, dateSel, expSel, internSel, minSalSel, locInp, visaSel, trackSel,
    hideNo, verifiedOnly, remoteOnly, hideAgency, showClosed, rail, tabBtns, tab;
var VISABOXES = [];
function reset(page, vals) {
  q = ctl(vals.q || "");
  sortSel = ctl(vals.sort || "score");
  if (page === "feed") {
    rail = {};                            // #filterrail exists only on the feed
    minR = ctl(vals.min || "0");
    dateSel = ctl(vals.date || "any"); expSel = ctl(vals.exp || "any");
    internSel = ctl(vals.intern || "any"); minSalSel = ctl(vals.minsal || "");
    locInp = ctl(vals.loc || ""); visaSel = ctl(vals.visatags || "");
    trackSel = ctl(vals.track || "any");
    hideNo = box(vals.hidenospon); verifiedOnly = box(vals.verifiedonly);
    remoteOnly = box(vals.remoteonly);
    hideAgency = box(vals.hideagency); showClosed = box(vals.showclosed);
    tabBtns = [{}, {}, {}, {}]; tab = vals.tab || "recommended";
    VISABOXES = ["h1b", "green_card", "stem_opt", "e3", "h1b1"].map(function (t) {
      return { type: "checkbox", checked: false, getAttribute: function () { return t; } };
    });
  } else {
    rail = null; minR = dateSel = expSel = internSel = minSalSel = null;
    locInp = visaSel = trackSel = hideNo = verifiedOnly = null;
    remoteOnly = hideAgency = showClosed = null;
    tabBtns = []; tab = "recommended"; VISABOXES = [];
  }
}
var document = {
  querySelectorAll: function (s) { return s.indexOf("data-vt") >= 0 ? VISABOXES : []; },
  querySelector: function () { return null; }
};

// ---- lifted verbatim from static/app.js ---------------------------------------
var FILTER_KEY = "jm_filters:" + (feed.getAttribute("data-user") || ""), FILTER_V = 1;
%(funcs)s

// ---- run ----------------------------------------------------------------------
var out = {};
reset(PAGE, IN.set);
if (IN.seed) { for (var sk in IN.seed) STORE[sk] = JSON.stringify(IN.seed[sk]); }
if (IN.store !== null) STORE[FILTER_KEY] = JSON.stringify(IN.store);
if (IN.action === "save") { saveFilterState(); out.stored = JSON.parse(STORE[FILTER_KEY] || "{}"); }
else {
  var s = applyFilterState();
  out.applied = s;
  out.dom = { q: q && q.value, sort: sortSel && sortSel.value,
              min: minR && minR.value, date: dateSel && dateSel.value,
              loc: locInp && locInp.value, visatags: visaSel && visaSel.value,
              hideagency: hideAgency && hideAgency.checked,
              verifiedonly: verifiedOnly && verifiedOnly.checked,
              showclosed: showClosed && showClosed.checked };
  out.visaboxes = VISABOXES.map(function (b) { return b.checked; });
}
console.log(JSON.stringify(out));
"""

FUNCS = "\n".join(js_function(SRC, n) for n in ("_ctlMap", "_readStore",
                                                "saveFilterState", "applyFilterState"))


def run(page, action, set_vals=None, store=None, user="kunal", seed=None):
    body = DRIVER % {"input": json.dumps({"page": page, "action": action, "user": user,
                                          "set": set_vals or {}, "store": store,
                                          "seed": seed or {}}),
                     "funcs": FUNCS}
    fh = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    fh.write(body)
    fh.close()
    try:
        r = subprocess.run(["node", fh.name], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("node failed:\n" + r.stderr[:2000])
        return json.loads(r.stdout)
    finally:
        os.unlink(fh.name)


print("saving from the feed")
got = run("feed", "save", {"min": "45", "date": "7", "loc": "Boston", "hideagency": True,
                           "visatags": "h1b,stem_opt", "sort": "sponsor", "tab": "liked",
                           "q": "analyst", "verifiedonly": True})["stored"]
check("verified-date filter captured", got.get("verifiedonly") is True)
check("every control captured", got.get("min") == "45" and got.get("date") == "7"
      and got.get("loc") == "Boston" and got.get("sort") == "sponsor")
check("checkboxes captured as booleans", got.get("hideagency") is True)
check("active tab captured", got.get("tab") == "liked")
check("search text captured on the feed", got.get("q") == "analyst")
check("version stamped", got.get("v") == 1)

print("\nrestoring on the feed")
got = run("feed", "apply", {}, store=dict(got))
check("min restored", got["dom"]["min"] == "45")
check("sort restored", got["dom"]["sort"] == "sponsor")
check("location restored", got["dom"]["loc"] == "Boston")
check("checkbox restored", got["dom"]["hideagency"] is True)
check("verified-date filter restored", got["dom"]["verifiedonly"] is True)
check("visa hidden input restored", got["dom"]["visatags"] == "h1b,stem_opt")
check("the five visa checkboxes re-ticked to match",
      got["visaboxes"] == [True, False, True, False, False], str(got["visaboxes"]))

print("\nthe company page must not clobber the feed's filters")
saved = run("feed", "save", {"min": "45", "loc": "Boston", "date": "7"})["stored"]
after = run("company", "save", {"sort": "newest", "q": "engineer"}, store=saved)["stored"]
check("feed-only filters survive a company-page save",
      after.get("min") == "45" and after.get("loc") == "Boston" and after.get("date") == "7",
      json.dumps({k: after.get(k) for k in ("min", "loc", "date")}))
check("sort IS shared across both pages", after.get("sort") == "newest")
check("company-page search text does NOT leak into the feed's q",
      after.get("q") != "engineer", repr(after.get("q")))

print("\none browser, two accounts — filters must not follow the machine")
# Found while testing a second account: their brand-new feed came up carrying the first
# account's saved sort, because localStorage belongs to the BROWSER, not the login.
saved = run("feed", "save", {"min": "45", "sort": "sponsor", "loc": "Boston"},
            user="kunal")["stored"]
got = run("feed", "apply", {"min": "0", "sort": "score", "loc": ""},
          user="onboardtest", seed={"jm_filters:kunal": saved})
check("a different user gets their OWN defaults, not the last user's",
      got["dom"]["sort"] == "score" and got["dom"]["min"] == "0" and got["dom"]["loc"] == "",
      json.dumps(got["dom"]))
back = run("feed", "apply", {"min": "0", "sort": "score"}, store=saved, user="kunal")
check("...and the first user still gets theirs back",
      back["dom"]["sort"] == "sponsor" and back["dom"]["min"] == "45")
own = run("feed", "save", {"sort": "newest"}, user="onboardtest",
          seed={"jm_filters:kunal": saved})
check("the second user's save doesn't touch the first user's key",
      own["stored"].get("sort") == "newest")

print("\nrubbish in localStorage is discarded whole, never half-applied")
for bad in ({"v": 99, "min": "80"}, {"min": "80"}, [], "nope", None):
    got = run("feed", "apply", {"min": "0"}, store=bad)
    check("ignored %r" % (bad,), got["dom"]["min"] == "0", "min=%s" % got["dom"]["min"])

print("\nmissing keys leave the server-rendered value alone")
got = run("feed", "apply", {"min": "45", "date": "30"}, store={"v": 1, "sort": "newest"})
check("untouched control keeps its seeded value", got["dom"]["min"] == "45"
      and got["dom"]["date"] == "30")
check("present key still applies", got["dom"]["sort"] == "newest")

print("\n" + ("ALL FILTER-MEMORY CHECKS PASS" if not FAILS else "FAILED: %s" % FAILS))
sys.exit(1 if FAILS else 0)
