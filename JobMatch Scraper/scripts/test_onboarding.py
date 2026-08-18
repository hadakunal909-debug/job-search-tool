#!/usr/bin/env python3
"""The first-login wizard, through the real app. No credentials, no database.

The case worth writing this for is the third one: POST /profile rebuilds its payload over all
39 text fields, so a partial form blanks everything it doesn't contain. Every wizard step is a
partial form. If /welcome ever grows a shortcut into that route, step 1 would silently wipe the
EEO answers, the notes and the salary expectation — and nothing would say so.

    python scripts/test_onboarding.py
"""
import inspect
import json
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP_DIR)
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
# Asserted against web.ONBOARD_QUESTIONS rather than a hardcoded list, so adding or reordering
# a question updates the test by construction instead of leaving it asserting a stale flow.
check("the flow is four questions", web.ONBOARD_STEPS == 4, str(web.ONBOARD_STEPS))
check("...and the question table agrees", len(web.ONBOARD_QUESTIONS) == web.ONBOARD_STEPS)
for q in web.ONBOARD_QUESTIONS:
    body = c.get("/welcome?step=%d" % q["n"]).data.decode("utf-8", "replace")
    check("question %d renders (%s)" % (q["n"], q["key"]), q["title"] in body)
    check("  ...and counts questions, not screens",
          ("Question %d of %d" % (q["n"], web.ONBOARD_STEPS)) in body)

    if q["key"] == "resume":
        # The résumé moved from LAST to FIRST: it is the only skip that costs something
        # irreversible, and at position five it sat behind sixteen low-value fields.
        check("  ...résumé is question 1", q["n"] == 1)
        check("  ...offers a file upload", 'type="file"' in body and 'name="resume_file"' in body)
        check("  ...is multipart, or the file never arrives",
              'enctype="multipart/form-data"' in body)
        check("  ...still offers paste, since a scanned PDF has no text to extract",
              'name="resume"' in body and "scanned" in body)
        check("  ...says the file itself isn't kept", "don't keep the file" in body)

    if q["key"] == "roles":
        check("  ...renders the shared role picker", 'class="rolepick' in body)
        # THE bug this replaced: the picker relied on JS to copy ticked boxes into a hidden
        # input, but app.js only loads on the feed. The checkboxes must post themselves.
        check("  ...checkboxes POST themselves, no JS required",
              'name="roles"' in body and 'type="checkbox"' in body)
        check("  ...and there is no hidden #roles to go stale", 'id="roles"' not in body)
        check("  ...with live corpus counts on each tile", 'class="rolen"' in body)
        check("  ...every family from core", all(
            ('data-role="%s"' % k) in body for k in core.ROLE_KEYS))
        check("  ...grouped into sections", all(
            ('data-group="%s"' % g) in body for g, _lab in core.ROLE_GROUPS))
        check("  ...and loads the picker script for search and the cap", "rolepick.js" in body)
        check("  ...offers 'everything' as a real button, not a footnote",
              'name="all_roles"' in body)

    if q["key"] == "sponsorship":
        # Eight fields collapsed to one question. Only these three answers drive the feed.
        check("  ...offers exactly the three answers that change the feed",
              all(('value="%s"' % k) in body for k in web.SPONSORSHIP_ANSWERS))
        check("  ...carries the not-advice wording", "Not immigration advice" in body)
        check("  ...and points at the DSO and uscis.gov", "DSO" in body and "uscis.gov" in body)
        # It has a component of its own so it cannot be quietly restyled into a generic note.
        check("  ...in the legal callout, not a generic note", 'class="callout legal"' in body)
        check("  ...and does NOT ask for immigration dates here",
              'name="opt_start_date"' not in body and 'name="program_end_date"' not in body)

    if q["key"] == "location":
        check("  ...reuses the feed's own location suggestions", 'list="locs"' in body)
        check("  ...and offers anywhere as one click", 'name="anywhere"' in body)

print()
print("the eight contact fields are GONE from onboarding")
# They exist to autofill application forms: nothing in the feed changes because you typed a
# GitHub URL, and the promise is meaningless to someone without the extension installed.
allbody = "".join(c.get("/welcome?step=%d" % n).data.decode("utf-8", "replace")
                  for n in range(1, web.ONBOARD_STEPS + 1))
for gone in ("first_name", "last_name", "phone", "linkedin", "github", "portfolio",
             "companies", "opt_end_date", "work_auth_status"):
    check("  no %s field anywhere in the flow" % gone, ('name="%s"' % gone) not in allbody)

print()
print("no step may write a profile key outside its own list")
# POST /profile rebuilds all 39 text keys, so a partial form blanks what it does not carry.
for n, fields in web.ONBOARD_STEP_FIELDS.items():
    body = c.get("/welcome?step=%d" % n).data.decode("utf-8", "replace")
    stray = [f for f in ("first_name", "email", "notes", "gender", "desired_salary")
             if ('name="%s"' % f) in body and f not in fields]
    check("  question %d posts only %s" % (n, list(fields) or "no profile keys"), not stray,
          str(stray))
print("\n" + "=" * 74)
print("A STEP MUST NOT BLANK THE REST OF THE PROFILE")
print("=" * 74)
STORE.clear()
# Fields a wizard step never renders, all of them in /profile's 39-key blanking list.
STORE.update({"notes": "keep me", "gender": "Prefer not to say", "desired_salary": "120000",
              "address_line1": "1 Main St", "how_did_you_hear": "a friend"})
c = client()
post(c, 4, location="Boston, MA")
check("the step's own field saved", STORE.get("location") == "Boston, MA",
      repr(STORE.get("location")))
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
# min_scale marks this search as already written on the CALIBRATED scale, so the floor is the
# user's own choice and must survive untouched. Without it normalize_prefs would (correctly)
# treat 60 as a raw-coverage floor from before calibrate_score existed and reset it — which is
# asserted separately below.
STORE.update({"search_prefs": {"min": 60, "min_scale": core.MIN_SCALE,
                               "loc": "boston", "hideagency": True}})
c = client()
post(c, 2, roles=["pm", "dataeng"])
sp = STORE.get("search_prefs") or {}
check("roles land in search_prefs", sp.get("roles") == "pm,dataeng", json.dumps(sp.get("roles")))
check("the rest of the saved search survives",
      sp.get("min") == 60 and sp.get("loc") == "boston" and sp.get("hideagency") is True,
      json.dumps({k: sp.get(k) for k in ("min", "loc", "hideagency")}))

print("\n" + "=" * 74)
print("a floor saved before the score was calibrated is not reinterpreted")
print("=" * 74)
# The displayed score changed scale: raw coverage of 45 (the old default) was the 94th
# percentile and is 94 now. Silently comparing a stored 45 against calibrated scores would
# widen a user's feed from the top 6% to the top 55% without them touching anything, so a
# pre-v2 floor is reset to the current default instead of translated.
mig = core.normalize_prefs({"min": 45, "loc": "boston"})
check("a pre-v2 floor is reset to the default",
      mig["min"] == core.DEFAULT_PREFS["min"], str(mig["min"]))
check("...and the rest of that search is still preserved", mig["loc"] == "boston", mig["loc"])
check("...and it is stamped so it only ever happens once",
      mig["min_scale"] == core.MIN_SCALE, str(mig["min_scale"]))
check("a floor already on the new scale is left alone",
      core.normalize_prefs({"min": 55, "min_scale": core.MIN_SCALE})["min"] == 55)
check("zero means 'no floor' on every scale and survives",
      core.normalize_prefs({"min": 0})["min"] == 0)
check("and NOT in extra, where nothing reads them",
      "target_roles" not in (STORE.get("extra") or {}))

print("\n" + "=" * 74)
print("extra is merged, not replaced")
print("=" * 74)
STORE.clear()
STORE.update({"extra": {"ev_off": True}})       # the analytics opt-out lives here too
c = client()
c.post("/welcome", data={"_csrf": token(c), "step": "3", "action": "skip"})
e = STORE.get("extra") or {}
check("a skipped question is recorded", e.get("onboarding_unanswered") == [3],
      json.dumps(e.get("onboarding_unanswered")))
check("the analytics opt-out was NOT clobbered", e.get("ev_off") is True)

print("\n" + "=" * 74)
print("junk in, nothing out")
print("=" * 74)
STORE.clear()
c = client()
post(c, 2, roles=["pm", "'; DROP TABLE jobs; --", "not_a_real_role"])
check("only known role keys are accepted",
      (STORE.get("search_prefs") or {}).get("roles") == "pm",
      json.dumps((STORE.get("search_prefs") or {}).get("roles")))
c = client()
post(c, 2, roles=["dataeng", "pm"])
check("...and the stored order is canonical, not whatever was posted",
      (STORE.get("search_prefs") or {}).get("roles") == "pm,dataeng",
      json.dumps((STORE.get("search_prefs") or {}).get("roles")))

print("\n" + "=" * 74)
print("finishing, skipping, and CSRF")
print("=" * 74)
STORE.clear()
c = client()
r = post(c, 4, location="Boston, MA")
check("the last step finishes and lands on the feed",
      (STORE.get("extra") or {}).get("onboarded") is True
      and (r.headers.get("Location") or "").rstrip("/").endswith(""))
c = client()
post(c, 1, resume="Kunal Singh, project manager, 4 years, Boston.")
check("the résumé saves on question 1", RESUMES and "project manager" in RESUMES[-1]["content"])

STORE.clear()
c = client()
r = c.post("/welcome", data={"_csrf": token(c), "step": "2", "action": "skip"})
# THE fix: skip used to write onboarded=True from any step, so one click on the first screen
# ended setup for good while the button said "Skip for now".
check("skipping does NOT end setup",
      (STORE.get("extra") or {}).get("onboarded") is not True,
      repr((STORE.get("extra") or {}).get("onboarded")))
check("...it advances to the next question",
      "step=3" in (r.headers.get("Location") or ""), r.headers.get("Location"))
check("...and records the question as unanswered",
      (STORE.get("extra") or {}).get("onboarding_unanswered") == [2],
      json.dumps((STORE.get("extra") or {}).get("onboarding_unanswered")))

STORE.clear()
c = client()
token(c)
c.post("/welcome", data={"step": "4", "action": "next", "location": "Nope"})
check("a POST with no CSRF token saves nothing", not STORE.get("location"),
      repr(STORE.get("location")))

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
    r = c.post("/welcome", data={"_csrf": token(c), "step": "1", "action": "next",
                                 "resume_file": (io.BytesIO(doc), "kunal_cv.docx")},
               content_type="multipart/form-data")
    check("an uploaded .docx is parsed and saved",
          RESUMES and "Program Manager, Globex" in RESUMES[-1]["content"],
          RESUMES[-1]["content"][:60] if RESUMES else "nothing saved")
    # The résumé is question 1 now, so a successful upload ADVANCES rather than finishing.
    check("and it moves on to the next question",
          "step=2" in (r.headers.get("Location") or ""), r.headers.get("Location"))

    # An unreadable upload must not cost the user what they typed.
    STORE.clear()
    RESUMES.clear()
    c = client()
    c.post("/welcome", data={"_csrf": token(c), "step": "1", "action": "next",
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
           data={"_csrf": token(c), "resume_file": (io.BytesIO(doc), "PM résumé v2.docx")},
           content_type="multipart/form-data")
    check("Resume Brain accepts an upload too",
          RESUMES and "Globex" in RESUMES[-1]["content"])
    check("...and names it after the file rather than 'Untitled résumé'",
          RESUMES and RESUMES[-1]["name"] == "PM résumé v2",
          RESUMES[-1]["name"] if RESUMES else "-")

    RESUMES.clear()
    c = client()
    c.post("/brain/resume/save", data={"_csrf": token(c), "name": "", "content": ""},
           content_type="multipart/form-data")
    check("an empty submit saves nothing", not RESUMES)
except ImportError:
    print("  (skipped — python-docx not installed)")

print("\n" + "=" * 74)
print("no résumé, no score — never somebody else's number")
print("=" * 74)
# user_scores used to fall through to the stored match_score for every row when the viewer had
# no résumé. That column is computed against the SCRAPER's resume.txt, so a brand-new account
# saw a feed of confident 62-64% rings derived from another person's CV, drawn identically to a
# real personalised match.
#
# The corpus here is SYNTHETIC, and has to be. The baseline check below is an anti-vacuity one
# — "every score was suppressed" proves nothing unless the rows really did carry baselines that
# could have leaked — so reading the live table made this section pass only for whoever had
# Supabase credentials. python-tests.yml passes none, jobs.csv and user_jobs.json are both
# gitignored, so in CI get_jobs() returned [] and BOTH assertions failed against an empty
# corpus rather than against the behaviour they describe. This file's docstring already
# promised "no credentials, no database"; this section was the one place that broke it.
STUB_JOBS = [
    {"url": "https://example.com/jobs/%d" % i,
     "title": "Project Manager", "company": "Example Corp",
     # The baseline that must never reach a résumé-less viewer. Deliberately VARIED per row,
     # which is what makes the "not the baseline" check below decisive: every row carries the
     # same jd_terms, so a real analysis scores them all identically and only a fallback to
     # match_score could produce a spread.
     "match_score": 60 + (i % 20),
     # Weighted terms chosen to overlap RESUME_TEXT below, so the "with a résumé" case
     # scores > 0 through core.score_against rather than through the baseline fallback.
     "jd_terms": core.pack_analyzed({"weight": {"project": 10.0, "manager": 8.0,
                                                "delivery": 6.0, "stakeholder": 4.0}})}
    for i in range(150)
]
RESUME_TEXT = "project manager with six years of delivery experience"
web.get_jobs = lambda force=False: STUB_JOBS
web._jdmeta = {}          # job_analysis prefers jdmeta.json; keep the stub's jd_terms in charge
web.current_profile = lambda: ""
web._score_cache.clear()
jobs = web.get_jobs()
scored = web.user_scores("nobody", "")
nonzero = [u for u, s in scored.items() if s]
check("every score is suppressed with no résumé", not nonzero,
      "%d rows still scored" % len(nonzero))
baseline = sum(1 for j in jobs if int(j.get("match_score") or 0) > 0)
check("...and the corpus really does carry baselines it could have leaked",
      baseline > 100, "%d rows have a stored match_score" % baseline)

web._score_cache.clear()
with_resume = web.user_scores("nobody", RESUME_TEXT)
check("a user WITH a résumé still gets scores", any(with_resume.values()))
# ...and through the ANALYSIS, not by falling back to the baseline. Without this, a stub whose
# jd_terms failed to unpack would still "pass" the line above on somebody else's number, which
# is the exact bug this section exists to catch. Identical jd_terms across rows means a real
# analysis returns ONE value; the varied baselines mean a fallback returns many.
check("...scored against the JD's terms, not the stored baseline",
      len(set(with_resume.values())) == 1
      and all(s != j["match_score"] for s, j in zip(with_resume.values(), STUB_JOBS)),
      "distinct scores: %s" % sorted(set(with_resume.values()))[:5])
web._score_cache.clear()

src = open(os.path.join(APP_DIR, "static", "app.js"), encoding="utf-8").read()
check("the card renders no percentage without a résumé",
      "if (!HAS_RESUME)" in src and 'class="score-none"' in src)
check("...and reads the flag the server sets",
      'data-hasresume' in open(os.path.join(APP_DIR, "templates", "_feedgrid.html"),
                               encoding="utf-8").read())
check("both pages that embed the grid pass has_resume",
      all("has_resume=" in s for s in [
          inspect.getsource(web.feed), inspect.getsource(web.company)]))

print("\n" + ("ALL ONBOARDING CHECKS PASS" if not fails else "FAILED: %s" % fails))
sys.exit(1 if fails else 0)
