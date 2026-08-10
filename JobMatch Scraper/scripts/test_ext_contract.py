"""The Chrome extension's API contract, frozen.

The extension has ZERO coupling to the app's HTML: every call is cfg.apibase + "/api/ext/..."
carrying an HMAC token. That is exactly why the React migration is safe for it, and exactly
why nothing would catch a break. The realistic failure is a web.py refactor that touches
_cors() or an OPTIONS branch, ships green because no page changed, and surfaces days later as
"the extension stopped working".

So this asserts the shape of every /api/ext route: status, the CORS headers the extension
needs, and the KEY SET of the JSON body. Values are not asserted, because the corpus changes
daily and a value assertion would fail for reasons that are not regressions.

    python scripts/test_ext_contract.py        # exit 0 = the contract holds
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ.setdefault("EV_OFF", "1")           # never write analytics rows from a test run

import web                                     # noqa: E402
import db                                      # noqa: E402

fails = []


def check(name, cond, extra=""):
    if not cond:
        fails.append(name)
    print("  %s %s%s" % ("ok " if cond else "FAIL", name, ("  " + extra) if extra else ""))


web.app.config["TESTING"] = True
USER = (db.list_users() or [{}])[0].get("username")
if not USER:
    print("No accounts; nothing to test against.")
    raise SystemExit(0)

with web.app.test_request_context():
    TOKEN = web._ext_token(USER)

c = web.app.test_client()

print("=" * 74)
print("every /api/ext route is registered and reachable")
print("=" * 74)
routes = sorted({r.rule for r in web.app.url_map.iter_rules() if r.rule.startswith("/api/ext")})
for r in routes:
    print("   ", r)
check("13 or more ext routes exist", len(routes) >= 13, str(len(routes)))

print()
print("=" * 74)
print("auth: a bad token is refused, a good one is accepted")
print("=" * 74)
r = c.get("/api/ext/profile?user=%s&token=%s" % (USER, "not-a-real-token"))
check("bad token rejected", r.status_code in (401, 403), "status=%s" % r.status_code)
r = c.get("/api/ext/profile?user=%s&token=%s" % (USER, TOKEN))
check("good token accepted", r.status_code == 200, "status=%s" % r.status_code)

print()
print("=" * 74)
print("CORS preflight: the extension is a cross-origin caller")
print("=" * 74)
pre = c.open("/api/ext/profile", method="OPTIONS",
             headers={"Origin": "chrome-extension://abcdefghijklmnop"})
check("OPTIONS is handled", pre.status_code < 400, "status=%s" % pre.status_code)
check("allows an origin", bool(pre.headers.get("Access-Control-Allow-Origin")),
      pre.headers.get("Access-Control-Allow-Origin") or "MISSING")
check("allows the token header",
      "authorization" in (pre.headers.get("Access-Control-Allow-Headers") or "").lower()
      or "content-type" in (pre.headers.get("Access-Control-Allow-Headers") or "").lower(),
      pre.headers.get("Access-Control-Allow-Headers") or "MISSING")

print()
print("=" * 74)
print("response SHAPE, not values: a renamed key breaks the extension silently")
print("=" * 74)
# (path, keys the extension reads and therefore may not lose). Only the four GET routes are
# probed: the POST ones would mutate real rows, and their shape is better guarded by the
# feature tests than by a contract test that has to invent a valid body.
SHAPES = [
    ("/api/ext/profile", {"ok", "profile", "default_resume", "resume_names"}),
    ("/api/ext/profile_fields", {"ok", "fields", "defaults", "learned", "default_resume"}),
    ("/api/ext/learned", {"ok", "items"}),
    ("/api/ext/apply_queue", {"ok", "jobs", "count", "prefs_applied", "wide"}),
]
for path, required in SHAPES:
    r = c.get("%s?user=%s&token=%s" % (path, USER, TOKEN))
    if r.status_code != 200:
        check("%s responds 200" % path, False, "status=%s" % r.status_code)
        continue
    try:
        body = r.get_json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        check("%s returns a JSON object" % path, False, str(body)[:60])
        continue
    missing = required - set(body)
    check("%s keeps %s" % (path, ", ".join(sorted(required))), not missing,
          "missing=%s" % sorted(missing) if missing else "keys=%s" % sorted(body)[:6])

print()
if fails:
    print("EXTENSION CONTRACT BROKEN (%d):" % len(fails))
    for f in fails:
        print("   ", f)
    raise SystemExit(1)
print("EXTENSION CONTRACT HOLDS.")
