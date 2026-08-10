#!/usr/bin/env python3
"""The first-login wizard, through the real app. No credentials, no database.

The case worth writing this for is the third one: POST /profile rebuilds its payload over all
39 text fields, so a partial form blanks everything it doesn't contain. Every wizard step is a
partial form. If /welcome ever grows a shortcut into that route, step 1 would silently wipe the
EEO answers, the notes and the salary expectation — and nothing would say so.

    python scripts/test_onboarding.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core
import db
import web

USER = "wizard-test-user"
web.app.config["TESTING"] = True
STORE = {}
RESUMES = []
fails = []


def check(name, cond, extra=""):
    if not cond:
        fails.append(name)
    print("  %s %s%s" % ("ok " if cond else "FAIL", name, ("  " + extra) if extra else ""))


# Keep every write in memory: this must never touch the real profiles table.
db.get_profile = lambda u: dict(STORE)
db.save_profile = lambda u, fields: (STORE.update(fields), (True, ""))[1]
db.save_resume = lambda u, rec: (RESUMES.append(rec), (True, "id"))[1]
web.current_profile = lambda: ""
web._bust_profile = lambda user=None: None
# login_required re-checks per request that the account still exists and is enabled. Stubbed
# rather than borrowing a real username, so this test needs no particular account to exist.
web._session_dead = lambda u: ""


def client():
    c = web.app.test_client()
    with c.session_transaction() as s:
        s["user"] = USER
    return c


def token(c):
    """This session's CSRF token — seeded the way _csrf_token() would on first render."""
    with c.session_transaction() as s:
        s["_csrf"] = "test-token"
    return "test-token"


def post(c, step, **fields):
    body = {"_csrf": token(c), "step": str(step), "action": "next"}
    body.update(fields)
    return c.post("/welcome", data=body, follow_redirects=False)


print("=" * 74)
print("who gets sent to the wizard")
print("=" * 74)
STORE.clear()
check("a brand-new account does", web._needs_onboarding(USER))
STORE.update({"first_name": "Kunal"})
check("an account with contact details does NOT", not web._needs_onboarding(USER))
# All three signals matter. An account can predate this wizard and have been used for months
# without anyone typing a name — testing contact fields alone would ambush a long-standing
# user with a setup flow for an app they already know.
STORE.clear()
STORE.update({"search_prefs": {"min": 45}})
check("an account with a saved search does NOT", not web._needs_onboarding(USER))
STORE.clear()
web.current_profile = lambda: "years of project management experience"
check("an account with a résumé does NOT", not web._needs_onboarding(USER))
web.current_profile = lambda: ""
STORE.clear()
STORE.update({"extra": {"onboarded": True}})
check("someone who finished or skipped does NOT", not web._needs_onboarding(USER))
STORE.clear()
check("the feed redirects a new account to it",
      client().get("/").headers.get("Location", "").endswith("/welcome"))
STORE.update({"email": "a@b.com"})
r = client().get("/")
check("and does NOT redirect an existing one", "/welcome" not in (r.headers.get("Location") or ""))

print("\n" + "=" * 74)
print("every step renders")
print("=" * 74)
STORE.clear()
c = client()
for step, marker in [(1, "First name"), (2, "Work authorization"), (3, "What are you targeting"),
                     (4, "companies in mind"), (5, "Add your r")]:
    body = c.get("/welcome?step=%d" % step).data.decode("utf-8", "replace")
    check("step %d" % step, marker in body)
    if step == 3:
        check("  ...renders the shared role picker", 'class="rolepick' in body)
        # THE bug this replaced: the picker relied on JS to copy ticked boxes into a hidden
        # input, but app.js only loads on the feed. On the wizard nothing synced, the counter
        # read "0 of 6" with four boxes ticked, and the submitted roles were empty. The
        # checkboxes must post themselves.
        check("  ...checkboxes POST themselves, no JS required",
              'name="roles"' in body and 'type="checkbox"' in body)
        check("  ...and there is no hidden #roles to go stale", 'id="roles"' not in body)
        check("  ...with live corpus counts on each tile", 'class="rolen"' in body)
        check("  ...every family from core", all(
            ('data-role="%s"' % k) in body for k in core.ROLE_KEYS))
        check("  ...grouped into sections", all(
            ('data-group="%s"' % g) in body for g, _lab in core.ROLE_GROUPS))
        check("  ...and loads the picker script for search and the cap",
              "rolepick.js" in body)
    if step == 2:
        check("  ...carries the not-advice wording", "Not immigration advice" in body)
        check("  ...and points at the DSO and uscis.gov", "DSO" in body and "uscis.gov" in body)
    if step == 5:
        check("  ...offers a file upload", 'type="file"' in body and 'name="resume_file"' in body)
        check("  ...is multipart, or the file never arrives",
              'enctype="multipart/form-data"' in body)
        check("  ...still offers paste, since a scanned PDF has no text to extract",
              'name="resume"' in body and "scanned" in body)
        check("  ...says the file itself isn't kept", "don't keep the file" in body)

print("\n" + "=" * 74)
print("A STEP MUST NOT BLANK THE REST OF THE PROFILE")
print("=" * 74)
STORE.clear()
# Fields a wizard step never renders, all of them in /profile's 39-key blanking list.
STORE.update({"notes": "keep me", "gender": "Prefer not to say", "desired_salary": "120000",
              "address_line1": "1 Main St", "how_did_you_hear": "a friend"})
c = client()
post(c, 1, first_name="Kunal", last_name="Singh", email="k@example.com")
check("the step's own fields saved", STORE.get("first_name") == "Kunal"
      and STORE.get("email") == "k@example.com")
survived = {k: STORE.get(k) for k in
            ("notes", "gender", "desired_salary", "address_line1", "how_did_you_hear")}
check("everything the step didn't render survived",
      survived == {"notes": "keep me", "gender": "Prefer not to say",
                   "desired_salary": "120000", "address_line1": "1 Main St",
                   "how_did_you_hear": "a friend"},
      json.dumps(survived))

print("\n" + "=" * 74)
print("roles are a saved-search PREF, not profile extra")
print("=" * 74)
# They FILTER the feed and the digest, so they have to live where the other filters live and go
# through normalize_prefs. Saving them must merge over the stored search, not reset it.
STORE.clear()
STORE.update({"search_prefs": {"min": 60, "loc": "boston", "hideagency": True}})
c = client()
post(c, 3, roles=["pm", "dataeng"])
sp = STORE.get("search_prefs") or {}
check("roles land in search_prefs", sp.get("roles") == "pm,dataeng", json.dumps(sp.get("roles")))
check("the rest of the saved search survives",
      sp.get("min") == 60 and sp.get("loc") == "boston" and sp.get("hideagency") is True,
      json.dumps({k: sp.get(k) for k in ("min", "loc", "hideagency")}))
check("and NOT in extra, where nothing reads them",
      "target_roles" not in (STORE.get("extra") or {}))

print("\n" + "=" * 74)
print("extra is merged, not replaced")
print("=" * 74)
STORE.clear()
STORE.update({"extra": {"ev_off": True}})       # the analytics opt-out lives here too
c = client()
post(c, 4, companies="Stripe, Databricks ,, Northrop Grumman")
e = STORE.get("extra") or {}
check("companies saved and blanks dropped",
      e.get("target_companies") == ["Stripe", "Databricks", "Northrop Grumman"],
      json.dumps(e.get("target_companies")))
check("the analytics opt-out was NOT clobbered", e.get("ev_off") is True)

print("\n" + "=" * 74)
print("junk in, nothing out")
print("=" * 74)
STORE.clear()
c = client()
post(c, 3, roles=["pm", "'; DROP TABLE jobs; --", "not_a_real_role"])
check("only known role keys are accepted",
      (STORE.get("search_prefs") or {}).get("roles") == "pm",
      json.dumps((STORE.get("search_prefs") or {}).get("roles")))
c = client()
post(c, 3, roles=["dataeng", "pm"])
check("...and the stored order is canonical, not whatever was posted",
      (STORE.get("search_prefs") or {}).get("roles") == "pm,dataeng",
      json.dumps((STORE.get("search_prefs") or {}).get("roles")))

print("\n" + "=" * 74)
print("finishing, skipping, and CSRF")
print("=" * 74)
STORE.clear()
c = client()
r = post(c, 5, resume="Kunal Singh — project manager, 4 years, Boston.")
check("the last step finishes and lands on the feed",
      (STORE.get("extra") or {}).get("onboarded") is True
      and (r.headers.get("Location") or "").rstrip("/").endswith(""))
check("the résumé was saved", RESUMES and "project manager" in RESUMES[-1]["content"])

STORE.clear()
c = client()
c.post("/welcome", data={"_csrf": token(c), "step": "2", "action": "skip"})
check("skip marks it done so they aren't asked again",
      (STORE.get("extra") or {}).get("onboarded") is True)
check("and records where they gave up",
      (STORE.get("extra") or {}).get("onboarding_skipped_at_step") == 2)

STORE.clear()
c = client()
token(c)
c.post("/welcome", data={"step": "1", "action": "next", "first_name": "Nope"})
check("a POST with no CSRF token saves nothing", not STORE.get("first_name"),
      repr(STORE.get("first_name")))

print("\n" + "=" * 74)
print("résumé upload reaches the route")
print("=" * 74)
# The unit tests cover the parser; this covers the plumbing between the form and it, which is
# where an upload fails SILENTLY (a missing enctype posts the field name and no file).
import io
try:
    import core
    doc = core.resume_to_docx_bytes(
        "KUNAL SINGH\n\nEXPERIENCE\nProgram Manager, Globex\n- Ran six projects\n")
    STORE.clear()
    RESUMES.clear()
    c = client()
    c.post("/welcome", data={"_csrf": token(c), "step": "5", "action": "next",
                             "resume_file": (io.BytesIO(doc), "kunal_cv.docx")},
           content_type="multipart/form-data")
    check("an uploaded .docx is parsed and saved",
          RESUMES and "Program Manager, Globex" in RESUMES[-1]["content"],
          RESUMES[-1]["content"][:60] if RESUMES else "nothing saved")
    check("and it still completes the wizard",
          (STORE.get("extra") or {}).get("onboarded") is True)

    # An unreadable upload must not cost the user what they typed.
    STORE.clear()
    RESUMES.clear()
    c = client()
    c.post("/welcome", data={"_csrf": token(c), "step": "5", "action": "next",
                             "resume": "pasted fallback text about project management",
                             "resume_file": (io.BytesIO(b"not a pdf at all"), "cv.pdf")},
           content_type="multipart/form-data")
    check("a failed parse falls back to the pasted text",
          RESUMES and "pasted fallback" in RESUMES[-1]["content"],
          RESUMES[-1]["content"][:50] if RESUMES else "nothing saved")

    # Resume Brain's own form, the other upload surface.
    STORE.clear()
    RESUMES.clear()
    c = client()
    c.post("/brain/resume/save",
           data={"resume_file": (io.BytesIO(doc), "PM résumé v2.docx")},
           content_type="multipart/form-data")
    check("Resume Brain accepts an upload too",
          RESUMES and "Globex" in RESUMES[-1]["content"])
    check("...and names it after the file rather than 'Untitled résumé'",
          RESUMES and RESUMES[-1]["name"] == "PM résumé v2",
          RESUMES[-1]["name"] if RESUMES else "-")

    RESUMES.clear()
    c = client()
    c.post("/brain/resume/save", data={"name": "", "content": ""},
           content_type="multipart/form-data")
    check("an empty submit saves nothing", not RESUMES)
except ImportError:
    print("  (skipped — python-docx not installed)")

print("\n" + ("ALL ONBOARDING CHECKS PASS" if not fails else "FAILED: %s" % fails))
sys.exit(1 if fails else 0)
