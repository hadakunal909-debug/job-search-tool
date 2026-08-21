#!/usr/bin/env python3
"""Applying is something the user says happened, not something a click implies.

Until 2026-08-21 static/app.js posted status="applied" from the Apply link's own click handler,
so opening a posting recorded an application and the tracker held 129 of them that had never
been made. The click now parks the job (static/applyask.js) and the user is asked when they come
back; only "yes" writes.

Four properties, none of which had any test before this file:

  1. /api/action still records an apply, and stamps WHERE it came from, so the funnel can tell a
     confirmed application from an outbound click.
  2. `via` is a closed set. The browser chooses this value and it lands in an aggregate, so an
     arbitrary string would be a free dimension for anyone with a console open.
  3. The click path itself writes nothing. Asserted on the wiring, because the alternative is a
     JS runtime in CI.
  4. scripts/reset_autologged_applies.py deletes exactly the rows _autolog_application wrote and
     nothing a person maintained by hand -- the direction that matters, since a real application
     wrongly deleted cannot be recovered.

No database: db is stubbed, the way scripts/test_job_page.py stubs analytics.emit.
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["EV_OFF"] = "1"                 # before web is imported, or analytics records for real

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import analytics                            # noqa: E402
import db                                   # noqa: E402
import web                                  # noqa: E402

USER = "tester"
WROTE = []                                  # (kind, ...) for every db write attempted
EMITS = []                                  # (event, props)


def _reset():
    del WROTE[:], EMITS[:]


db.set_user_status = lambda u, url, st: WROTE.append(("status", u, url, st)) or True
db.save_application = lambda u, rec: (WROTE.append(("application", u, rec)), (True, "x"))[1]
db.find_application_by_url = lambda u, url: None
db.get_user_statuses = lambda u: {}
analytics.emit = lambda user, sid, name, **props: EMITS.append((name, props))
web.analytics = analytics
# _autolog_application needs a job to copy the company and title off; give it one so the
# application-row write is exercised rather than skipped.
web._job_for = lambda url: {"company": "Acme", "title": "Program Manager", "url": url}
web._default_resume = lambda u: "Tester.pdf"
web.user_statuses = lambda u: {}
# login_required re-checks on EVERY request that the account still exists and is not disabled
# (web._session_dead), against _accounts()' cache of the users table. With no database that
# lookup returns None, the session is cleared and every POST below 302s to /login -- which
# would look exactly like the CSRF failure it is not.
web._account_state = lambda u: {"username": u, "created_at": "2026-06-01T00:00:00Z",
                               "disabled_at": None, "token_epoch": 0}
web._accounts = lambda: {USER: {"username": USER}}
web.app.config["TESTING"] = True


def _client():
    """A logged-in client and a CSRF token that will actually pass.

    Not a monkeypatch: _require_csrf is registered with Flask as a before_request handler at
    import time, so rebinding web._require_csrf leaves the registered function in place and
    every write still 400s. The token is derived from the session, so minting it inside a
    request context on the same client is both real and the only thing that works.
    """
    c = web.app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = USER
    with web.app.test_request_context("/"):
        from flask import session as fsession
        fsession["user"] = USER
        tok = web._csrf_token()
        # _csrf_token seeds the session on first call; carry whatever it put there back to the
        # client, or the token it just minted will not match the one the request recomputes.
        seeded = dict(fsession)
    with c.session_transaction() as sess:
        sess.update(seeded)
    return c, tok


def _post(ct, body):
    c, tok = ct
    return c.post("/api/action", json=body, headers={"X-CSRF-Token": tok})


FAILED = []


def check(name, cond, detail=""):
    print("%s  %s%s" % ("ok " if cond else "FAIL", name, "" if cond else "  -- " + detail))
    if not cond:
        FAILED.append(name)


# ---------------------------------------------------------------------------------------------
def test_a_confirmed_apply_writes_all_three_places():
    _reset()
    r = _post(_client(), {"url": "https://acme.example/jobs/1", "status": "applied",
                          "via": "confirmed"})
    check("confirmed apply returns ok", r.status_code == 200 and r.get_json().get("ok"),
          "HTTP %s %s" % (r.status_code, r.get_data()[:120]))
    kinds = [w[0] for w in WROTE]
    check("it sets the user_jobs status", "status" in kinds, str(kinds))
    check("it logs a tracker row", "application" in kinds, str(kinds))
    ev = [p for n, p in EMITS if n == "action"]
    check("it emits one action event", len(ev) == 1, str(EMITS))
    check("stamped via=confirmed", ev and ev[0].get("via") == "confirmed",
          ev[0].get("via") if ev else "no event")
    check("and to=applied", ev and ev[0].get("to") == "applied", str(ev))


def test_via_is_a_closed_set():
    _reset()
    _post(_client(), {"url": "https://acme.example/jobs/2", "status": "applied",
                      "via": "totally-made-up"})
    ev = [p for n, p in EMITS if n == "action"]
    check("an unknown via is not recorded verbatim",
          ev and ev[0].get("via") != "totally-made-up", str(ev))
    check("it falls back to api", ev and ev[0].get("via") == "api",
          ev[0].get("via") if ev else "no event")
    _reset()
    # An older cached app.js sends no via at all and must still record its action.
    _post(_client(), {"url": "https://acme.example/jobs/3", "status": "liked"})
    check("a missing via still records the action",
          any(n == "action" for n, _ in EMITS), str(EMITS))
    check("every whitelisted value is spelled the same in JS and Python",
          set(web._ACTION_VIA) >= {"confirmed", "card", "api"}, str(web._ACTION_VIA))


def test_the_apply_click_itself_writes_nothing():
    """Wiring, not runtime. Three facts together are the property: the feed's Apply handler
    parks the job instead of posting, the shared module exists, and the only thing that posts
    status=applied inside it is the branch behind the affirmative answer."""
    app_js = io.open(os.path.join(HERE, "static", "app.js"), encoding="utf-8").read()
    ask_js = io.open(os.path.join(HERE, "static", "applyask.js"), encoding="utf-8").read()

    # The Apply-link branch of the feed's delegated click handler.
    branch = app_js.split('lnk.hasAttribute("data-apply")', 1)[1].split("return;", 1)[0]
    check("the Apply click no longer posts an action",
          "doAction(" not in branch, branch.strip()[:160])
    check("it parks the posting instead", "ApplyAsk.pend" in branch, branch.strip()[:160])
    check("it still beacons the outbound click", 'EV("apply_click"' in branch,
          branch.strip()[:160])
    check("the shared module posts via=confirmed", '"confirmed"' in ask_js)
    # The negative answer must write NOTHING. Its callback is the second of the two passed to
    # ask1(), and the only thing it may touch is the pending store.
    no_branch = ask_js.split("Writes NOTHING", 1)[1].split("});", 1)[0]
    check("answering no writes nothing",
          "fetch(" not in no_branch and "doAction" not in no_branch, no_branch.strip()[:160])


def test_the_reset_script_only_deletes_what_the_feed_wrote():
    sys.path.insert(0, os.path.join(HERE, "scripts"))
    import reset_autologged_applies as reset

    same_day = {"created_at": "2026-08-01 14:00", "applied_date": "2026-08-01",
                "notes": "", "status": "applied"}
    cases = [
        ("an auto-logged row", dict(same_day), True),
        ("a row with a note", dict(same_day, notes="phone screen booked"), False),
        ("a row that reached interview", dict(same_day, status="interview"), False),
        ("a back-dated row", dict(same_day, applied_date="2026-07-02"), False),
        ("a row with no dates at all", {"created_at": "", "applied_date": ""}, False),
        ("an ISO created_at with a T", dict(same_day, created_at="2026-08-01T14:00:00Z"), True),
    ]
    for label, row, want in cases:
        check("reset keeps/deletes correctly: %s" % label,
              reset.looks_autologged(row) is want,
              "got %r, wanted %r" % (reset.looks_autologged(row), want))


if __name__ == "__main__":
    for fn in (test_a_confirmed_apply_writes_all_three_places,
               test_via_is_a_closed_set,
               test_the_apply_click_itself_writes_nothing,
               test_the_reset_script_only_deletes_what_the_feed_wrote):
        print("\n== %s" % fn.__name__)
        fn()
    print("\n" + "=" * 70)
    if FAILED:
        print("%d FAILED: %s" % (len(FAILED), ", ".join(FAILED)))
        sys.exit(1)
    print("All apply-confirmation checks passed.")
