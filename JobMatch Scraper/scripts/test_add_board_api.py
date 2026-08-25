#!/usr/bin/env python3
"""/api/ext/detect_board must never refuse to add a board without saying why.

The extension's "➕ Add <company> to the daily scraper" button was broken for months and the
popup could not tell you so. The route answered {"ok": true, "added": false} with NO error key
for every one of its failure modes, and popup.js fell through to the same sentence each time —
"Couldn't add: try the ➕ Add board page." Reproduced 2026-08-23 against
smurfitwestrockta.wd1.myworkdayjobs.com: found=true, count=None, db.add_board never called.

The cause was a one-line guard, `if data.get("add") and n`, where n is probe_board's count.
None (could not read the board) and 0 (read it, empty today) both fell out of it silently. The
sibling /add-board page had already been hardened against exactly this on 2026-08-22 — it
refuses only None, and it prints the reason — so this is a PARITY test as much as a regression
test: the two entry points to db.add_board must agree.

Four things are asserted, all of them decisions the route makes rather than data it fetches, so
probe_board / board_display_name / db.add_board are stubbed and nothing here needs the network:

  * every non-add carries a non-empty `error`   <- the actual bug
  * count None refuses, count 0 adds            <- parity with /add-board
  * a sluglike name is resolved or asked for     <- "Smurfitwestrockta", "Hdpc", "Wfscorp"
  * the add click does not re-detect             <- pinned board_url survives a junk page URL

    python scripts/test_add_board_api.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ.setdefault("EV_OFF", "1")           # analytics.py reads it once, at import

import db                                      # noqa: E402
import scraper                                 # noqa: E402
import web                                     # noqa: E402

fails = []


def check(label, ok, extra=""):
    print("  %s  %s%s" % ("ok " if ok else "FAIL", label, ("  " + extra) if extra else ""))
    if not ok:
        fails.append(label)


# ---------------------------------------------------------------------------------------------
# Stubs. The route's job is to DECIDE; the three things it decides from are faked so this suite
# stays offline and deterministic. _account_state is stubbed because CI has no database and
# _ext_user checks the account only after the HMAC verifies — the token itself is real.
# ---------------------------------------------------------------------------------------------
GH = "https://job-boards.greenhouse.io/probe-fixture"     # not in SOURCES; never fetched
WD = "https://tenantcode.wd1.myworkdayjobs.com/External"   # slug carries no employer

web.app.config["TESTING"] = True
web._account_state = lambda u: {}

# THE EXT RATE LIMITER IS OFF FOR THIS SUITE, deliberately. detect_board carries a burst tier
# of 10 calls per 20 seconds, and the checks below make more POSTs than that in well under a
# second -- so from the 11th call on, every assertion was reading a 429 body instead of the
# handler's. It fails as `need_name=None`, which looks like a contract bug and is not one.
#
# Stubbing _rate_hit rather than _ext_rate_limit: the limiter is registered as a
# before_request hook, so Flask holds the original function object and rebinding the name on
# the module would change nothing. The hook resolves _rate_hit from module globals per call.
# The limiter itself is covered by scripts/test_feed_ratelimit.py; throttling this suite only
# made it test the limiter by accident.
web._rate_hit = lambda *a, **k: None

writes = []
probe = {"n": 7}
display = {"name": "Probe Fixture Inc"}

db.add_board = lambda url, ats, company, added_by="": (
    writes.append({"url": url, "ats": ats, "company": company}), (True, ""))[1]
scraper.probe_board = lambda burl, ats: probe["n"]
scraper.board_display_name = lambda burl, ats, timeout=15: display["name"]

with web.app.test_request_context():
    TOKEN = web._ext_token("probe-user")
c = web.app.test_client()


def call(**body):
    body.setdefault("token", TOKEN)
    r = c.post("/api/ext/detect_board", json=body)
    return r.status_code, (r.get_json() or {})


def add(url=GH, **extra):
    extra.setdefault("board_url", url)
    extra.setdefault("ats", "greenhouse")
    return call(url=url, add=True, **extra)


print("=" * 78)
print("a refusal always says why  (the bug: it never did)")
print("=" * 78)
probe["n"] = None
st, j = add()
check("count=None refuses the add", st == 200 and j.get("added") is False,
      "added=%r" % j.get("added"))
check("...and names the reason", bool((j.get("error") or "").strip()),
      "error=%r" % (j.get("error") or "")[:60])
check("...and writes nothing", not writes, "writes=%r" % writes)

probe["n"] = None
_, j = call(url=GH)                            # the CHECK click, so the popup can say it early
check("the check click reports the count it got", j.get("count") is None)

del writes[:]
_, j = call(url="https://example.com/not-a-board")
check("no board found carries an error too", j.get("found") is False and bool(j.get("error")),
      "error=%r" % (j.get("error") or "")[:50])

builtin = next(u for u, t, _ in scraper.SOURCES if t == "greenhouse")
_, j = call(url=builtin, add=True, board_url=builtin, ats="greenhouse")
check("a built-in source says it is already scraped", bool(j.get("builtin")) and bool(j.get("error")),
      "error=%r" % (j.get("error") or "")[:50])

print()
print("=" * 78)
print("a failing write reports the DATABASE's reason, not a generic hint")
print("=" * 78)
probe["n"] = 7
db.add_board = lambda *a, **k: (False, 'relation "boards" does not exist (42P01)')
_, j = add()
check("add_board returning False surfaces its message", "42P01" in (j.get("error") or ""),
      "error=%r" % (j.get("error") or "")[:60])


def boom(*a, **k):
    raise RuntimeError("connection refused")


db.add_board = boom
_, j = add()
check("add_board raising surfaces the exception", "connection refused" in (j.get("error") or ""),
      "error=%r" % (j.get("error") or "")[:60])

db.add_board = lambda url, ats, company, added_by="": (
    writes.append({"url": url, "ats": ats, "company": company}), (True, ""))[1]

print()
print("=" * 78)
print("count 0 is 'reachable but empty', not 'unreadable'  (parity with /add-board)")
print("=" * 78)
del writes[:]
probe["n"] = 0
_, j = add()
check("count=0 is added", j.get("added") is True and len(writes) == 1,
      "added=%r writes=%d" % (j.get("added"), len(writes)))
probe["n"] = 7

print()
print("=" * 78)
print("the employer name is resolved, never the URL slug in title case")
print("=" * 78)
# The nine garbled employers of 2026-08-22 all came from storing detect_board's third value:
# World Fuel Services as "Wfscorp", Goldman Sachs as "Hdpc". A name that matches nothing in the
# filing data carries no sponsorship signal, which is the whole point of the column.
del writes[:]
display["name"] = "Smurfit Westrock"
_, j = add(url=WD, ats="workday")
check("a sluglike guess is replaced by what the board calls itself",
      j.get("added") is True and writes and writes[0]["company"] == "Smurfit Westrock",
      "stored=%r" % (writes[0]["company"] if writes else None))
check("...and is not the tenant code", "Tenantcode" not in (writes[0]["company"] if writes else ""))

del writes[:]
display["name"] = ""                           # Workday renders its title client-side: no name
_, j = add(url=WD, ats="workday")
check("a board that publishes no name is refused, not guessed",
      j.get("added") is False and j.get("need_name") is True,
      "added=%r need_name=%r" % (j.get("added"), j.get("need_name")))
check("...with an error the popup can show", bool(j.get("error")))
check("...and nothing is written", not writes, "writes=%r" % writes)

_, j = add(url=WD, ats="workday", name="Smurfit Westrock")
check("a typed name is accepted", j.get("added") is True and writes
      and writes[-1]["company"] == "Smurfit Westrock",
      "stored=%r" % (writes[-1]["company"] if writes else None))

# name_is_sluglike is true for "Samsara" too, where the slug IS the company. It may only decide
# whether to go LOOK — re-testing its own answer would demand a typed name for a good board.
del writes[:]
display["name"] = "Probe Fixture"
_, j = add()
check("a board whose slug really is the company needs no typed name",
      j.get("added") is True and j.get("need_name") is False,
      "need_name=%r" % j.get("need_name"))

print()
print("=" * 78)
print("the ADD click does not re-run detection")
print("=" * 78)
# Half the server's detection chain is a live fetch. Re-deriving the board on the second click
# meant a transient miss reported "couldn't add" for a board the first click had just found.
del writes[:]
display["name"] = "Probe Fixture"
_, j = call(url="https://example.com/junk-page", add=True, board_url=GH, ats="greenhouse",
            name="Probe Fixture")
check("a pinned board survives a page URL that detects nothing",
      j.get("added") is True and writes and writes[0]["url"] == GH,
      "added=%r url=%r" % (j.get("added"), writes[0]["url"] if writes else None))

del writes[:]
_, j = call(url="https://example.com/junk-page", add=True, board_url=GH, ats="not-a-scraper",
            name="X")
check("a pinned ats that is not a real scraper is refused",
      j.get("added") is not True and not writes,
      "added=%r writes=%r" % (j.get("added"), writes))

del writes[:]
_, j = call(url="https://example.com/junk", add=True,
            board_url="javascript:alert(1)", ats="greenhouse", name="X")
check("a pinned board_url that is not a board is refused",
      j.get("added") is not True and not writes,
      "added=%r writes=%r" % (j.get("added"), writes))

print()
if fails:
    print("ADD-BOARD API BROKEN (%d):" % len(fails))
    for f in fails:
        print("   ", f)
    raise SystemExit(1)
print("ADD-BOARD API HOLDS.")
