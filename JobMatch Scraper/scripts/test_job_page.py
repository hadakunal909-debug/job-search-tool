#!/usr/bin/env python3
"""The /job page, through Flask's real test client.

Covers the three route outcomes, the markup contract, and the two things that would go wrong
QUIETLY rather than loudly:

  * job_open must fire exactly ONCE per GET with its five original props unrenamed. It moved out
    of /api/job when the modal was retired, and this project's usage numbers have already been
    distorted once by an event drifting, so the assertion is on the emit itself rather than on
    the page rendering.
  * the page must NOT ship #feed, or app.js would try to initialise a card grid here.

    python scripts/test_job_page.py
"""
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import web
import analytics

USER = "jobpage@test"
LIVE = "https://boards.example.com/acme/1"
DUPE_A = "https://boards.greenhouse.io/acme/jobs/77"      # aggregator host
DUPE_B = "https://job-boards.greenhouse.io/acme/jobs/77"  # employer's own host
GONE = "https://boards.example.com/acme/pruned"
# Two rows with no usable description, and the page must NOT say the same thing about them.
# PENDING has simply not been fetched yet; BLOCKED is on a host that refuses every server-side
# read (Tesla behind Akamai, iCIMS behind an AWS WAF challenge), so no future run changes it and
# "it'll get a match score once the full job description is fetched" is a promise we cannot keep.
PENDING = "https://boards.example.com/acme/pending"
BLOCKED = "https://www.walled-example.com/careers/9"

JD = ("About the Role\n"
      "Build and operate the payments service.\n\n"
      "Basic Qualifications:\n"
      "- Five years of Python in production\n"
      "- Strong SQL and Kubernetes\n\n"
      "What We Offer\n"
      "A short deploy pipeline and real ownership.")

JOBS = [
    {"url": LIVE, "title": "Staff Platform Engineer", "company": "Acme Corp",
     "location": "Boston, MA", "found_date": "2026-08-04", "match_score": 71,
     "is_active": True, "jd": JD, "sponsors_h1b": "", "first_seen": "2026-08-04"},
    # Same posting, two hosts: _dedupe_rows keeps ONE of them, so the other must 302 rather
    # than 404. A page built from get_jobs() instead of ranked_rows would render both.
    {"url": DUPE_A, "title": "Data Engineer", "company": "Acme Corp",
     "location": "Boston, MA", "found_date": "2026-08-02", "match_score": 55,
     "is_active": True, "jd": JD, "sponsors_h1b": "", "first_seen": "2026-08-02"},
    {"url": DUPE_B, "title": "Data Engineer", "company": "Acme Corp",
     "location": "Boston, MA", "found_date": "2026-08-02", "match_score": 55,
     "is_active": True, "jd": JD, "sponsors_h1b": "", "first_seen": "2026-08-02"},
    {"url": PENDING, "title": "Backend Engineer", "company": "Acme Corp",
     "location": "Boston, MA", "found_date": "2026-08-05", "match_score": 0,
     "is_active": True, "jd": "", "sponsors_h1b": "", "first_seen": "2026-08-05"},
    {"url": BLOCKED, "title": "Inference Engineer", "company": "Walled Co",
     "location": "Palo Alto, CA", "found_date": "2026-08-05", "match_score": 0,
     "is_active": True, "jd": "", "sponsors_h1b": "", "first_seen": "2026-08-05"},
]

# Keep this test off the network and off the database.
web.get_jobs = lambda: [dict(j) for j in JOBS]
web._session_dead = lambda u: ""
web._needs_onboarding = lambda u: False
web.current_profile = lambda: "python sql aws docker terraform spark airflow"
web.user_statuses = lambda u: {}
web.sponsor_counts = lambda: {}
web.sponsor_years = lambda: {}
db.get_job_jd = lambda url: next((j["jd"] for j in JOBS if j["url"] == url), "")
db.using_supabase = lambda: False          # so no research thread is ever started
db.get_brain_company = lambda dom: {}
db.list_brain_companies = lambda: {}
# scripts/close_dead_jds.py records this after probing; _host_jd_blocked reads it and caches.
web._jd_blocked_hosts = {"www.walled-example.com"}
web._rows_cache.clear()

EMITS = []
analytics.emit = lambda user, sid, name, **props: EMITS.append((name, props))
web.analytics = analytics

FAILS = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print("  %s %-58s %s" % ("ok " if cond else "FAIL", name, extra))


def client():
    c = web.app.test_client()
    with c.session_transaction() as s:
        s["user"] = USER
    return c


def get(url_value, **kw):
    del EMITS[:]
    return client().get("/job?u=" + urllib.parse.quote(url_value, safe=""), **kw)


print("=" * 88)
print("ROUTE OUTCOMES")
print("=" * 88)
r = client().get("/job")
check("no u redirects to the feed", r.status_code == 302 and "/" in r.headers.get("Location", ""),
      "%s -> %s" % (r.status_code, r.headers.get("Location")))

r = get(GONE)
check("a pruned url is 404, not a redirect", r.status_code == 404, str(r.status_code))
body404 = r.data.decode("utf-8", "replace")
check("the 404 body explains what happened",
      "no longer in your feed" in body404.lower() and GONE in body404)
check("no job_open is emitted for a dead url", not [e for e in EMITS if e[0] == "job_open"],
      "inventing a flagged variant is how this metric got distorted before")

# Which of the two hosts survived the dedupe is a render-time preference, so ask rather than
# assume, then check the OTHER one redirects to it.
rows = web.ranked_rows(USER, web.current_profile())
kept = [x["url"] for x in rows if x["url"] in (DUPE_A, DUPE_B)]
check("the two hosts collapse to one row", len(kept) == 1, repr(kept))
if len(kept) == 1:
    other = DUPE_B if kept[0] == DUPE_A else DUPE_A
    r = get(other)
    loc = r.headers.get("Location", "")
    # Compare the PARSED value, not the raw header. url_for leaves ":" and "/" unescaped in a
    # query component, which RFC 3986 permits and Flask parses straight back, so asserting on a
    # percent-encoded form would be testing Werkzeug's encoder rather than this redirect.
    sent = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("u", [""])[0]
    check("the deduped twin 302s to the survivor",
          r.status_code == 302 and sent == kept[0], "%s -> u=...%s" % (r.status_code, sent[-30:]))

print()
print("=" * 88)
print("THE PAGE")
print("=" * 88)
r = get(LIVE)
check("a live url renders 200", r.status_code == 200, "%s bytes" % format(len(r.data), ","))
body = r.data.decode("utf-8", "replace")

check("the title is on the page", "Staff Platform Engineer" in body)
check("the company links to its own page", 'href="/company?c=Acme' in body.replace("%20", "+")
      or "/company?c=Acme" in body)
check("Apply is an outbound link with data-apply", 'data-apply="1"' in body
      and 'target="_blank"' in body and 'rel="noopener"' in body)
# /brain used to BE the tailor form. It is now the résumé review panel, and the tailor form moved
# to /brain/tailor, so this link has to name the form explicitly — landing on the review panel with
# a job in hand would drop the job on the floor.
check("Tailor View points at the tailor form", "/brain/tailor?job=" in body,
      "not /brain, which is now the review panel")
check("Save and Hide post to /action", 'action="/action"' in body and 'name="_csrf"' in body,
      "so they work with JavaScript off")
check("jobpage.js is loaded, app.js is not",
      "jobpage.js" in body and "app.js" not in body)
# app.js keys off #feed to decide it is on a feed page. Shipping one here would have it try to
# render a card grid into the job page.
check("no #feed on the page", 'id="feed"' not in body)
check("the route attribute is present", 'data-route="' in body,
      re.search(r'data-route="([a-z_]*)"', body).group(1))

print()
print("THE DESCRIPTION")
check("the JD is rendered as blocks, not one blob",
      body.count("<p>") >= 2 and "<ul>" in body and "jdh" in body)
# Against the TEXT, not the raw HTML. Highlighting can insert a <mark> mid-phrase ("What
# <mark>We Offer</mark>"), which breaks a substring search on markup while leaving every word on
# the page — the first version of this assertion failed for exactly that reason.
body_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", body))  # noqa: E501  (reused below)
check("the employer's own heading wording survives",
      "Basic Qualifications" in body_text and "What We Offer" in body_text,
      "not rewritten to a canonical label")
check("sections are bucketed", 'data-sec="req"' in body and 'data-sec="resp"' in body)
check("the jump strip is rendered", 'class="jdjump"' in body and "#jdsec-req" in body)
check("keywords are highlighted inline", 'class="kw-' in body)
check("the JD is not inside its own scroll box",
      "max-height:46vh" not in open(os.path.join(
          os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "style.css"),
          encoding="utf-8").read(),
      "a page-level description must not nest a scroll container")

print()
print("ORDER: the description comes before the employer and the sponsorship history")
# The whole point of the 2026-08-11 reorder. Somebody opening a job wants the description, not a
# primer on visa categories, and asserting on POSITION is the only way that stays true.
pos = {k: body_text.find(v) for k, v in (
    ("jd", "Job Description"), ("kw", "Keywords in This Description"),
    ("tailor", "Tailor Your R"), ("about", "About Acme"), ("spon", "Sponsorship History"))}
check("all five sections are present", all(v >= 0 for v in pos.values()), repr(pos))
check("Job Description is first", pos["jd"] < min(pos["kw"], pos["about"], pos["spon"]))
check("the employer comes after the keywords", pos["about"] > pos["kw"])
check("sponsorship history is last", pos["spon"] == max(pos.values()))

print()
print("SPONSORSHIP, condensed to statements")
check("no per-route glossary table", "routelist" not in body and "routewhy" not in body,
      "five rows explaining what H-1B means is not what a job page is for")
check("the filing history is one statement",
      "has a federal filing history for" in body_text or "no federal filing record" in body_text)
check("the absence caveat is still verbatim from core", core.VISA_ABSENCE_NOTE in body,
      repr(core.VISA_ABSENCE_NOTE))
# Removed from THIS page on request. It stays on /welcome and /profile, which is where somebody is
# actually entering the dates it warns about; test_onboarding.py still asserts it there.
check("the immigration-advice callout is NOT on the job page",
      "Not immigration advice." not in body)

print()
print("ANALYTICS, the assertion this file exists for")
opens = [p for n, p in EMITS if n == "job_open"]
check("exactly one job_open per GET", len(opens) == 1, "%d emitted" % len(opens))
if opens:
    check("its five props are unrenamed",
          set(opens[0]) == {"job_url", "company", "source", "score", "pending"},
          repr(sorted(opens[0])))
    check("job_url is the posting", opens[0].get("job_url") == LIVE)

# app.js prefetches this page when the pointer settles on a card title, so the SAME GET now
# arrives for jobs nobody opened. Hovering is the most common thing anyone does in the feed,
# so without the Sec-Purpose guard a slow scan down one screen would report six opens. The
# page must still render and still be usable -- only the event is withheld.
r = get(LIVE, headers={"Sec-Purpose": "prefetch"})
check("a prefetch still renders the page", r.status_code == 200, str(r.status_code))
check("...but emits NO job_open", not [e for e in EMITS if e[0] == "job_open"],
      "%d emitted" % len([e for e in EMITS if e[0] == "job_open"]))
r = get(LIVE)
check("a real open still emits exactly one",
      len([e for e in EMITS if e[0] == "job_open"]) == 1,
      "%d emitted" % len([e for e in EMITS if e[0] == "job_open"]))

print()
print("THE ROUTE VALUE AGREES WITH THE CARD'S")
# _route_of must reproduce app.js cardHTML's one expression for every combination, or the card
# you clicked and the page you land on disagree about the colour of the same job.
CASES = [
    ({"sponsor_jd": "", "visa_likely": "h1b"}, "h1b"),
    ({"sponsor_jd": "", "visa_likely": "sponsor"}, "sponsor"),
    ({"sponsor_jd": "", "visa_likely": "stem_opt"}, "stem_opt"),
    ({"sponsor_jd": "", "visa_likely": ""}, "none"),
    ({"sponsor_jd": "open", "visa_likely": "h1b"}, "h1b"),
    # Blocked wins over the filing history, even when narrowing left stem_opt standing.
    ({"sponsor_jd": "blocked", "visa_likely": "stem_opt"}, "blocked"),
    ({"sponsor_jd": "blocked", "visa_likely": ""}, "blocked"),
]
for row, want in CASES:
    got = web._route_of(row)
    check("route(%s, %r)" % (row["sponsor_jd"] or "-", row["visa_likely"]), got == want, got)

print()
print("=" * 88)
print("NO-DESCRIPTION STATES")
print("=" * 88)
r = get(PENDING)
body = r.data.decode("utf-8", "replace")
check("a pending row renders and says the description is too short",
      r.status_code == 200 and "too short to score" in body, str(r.status_code))
check("a pending row does NOT claim the employer publishes nothing",
      "doesn" not in body.split("Profile Match")[-1][:400].replace("doesn't publish", "X"))

r = get(BLOCKED)
body = r.data.decode("utf-8", "replace")
check("a blocked row renders", r.status_code == 200, str(r.status_code))
check("a blocked row says the employer publishes nothing we can read",
      "publish a description we can read" in body)
check("a blocked row does NOT promise a score is coming",
      "too short to score" not in body,
      "that copy ends 'once the full job description is fetched' — it never will be")

rows = {j["url"]: web._build_row(j, 0) for j in JOBS if j["url"] in (PENDING, BLOCKED)}
check("jd_unavailable is set only for the walled host",
      rows[BLOCKED]["jd_unavailable"] is True and rows[PENDING]["jd_unavailable"] is False)
check("both are still score_pending, so neither shows a fake 0%",
      rows[BLOCKED]["score_pending"] and rows[PENDING]["score_pending"])

print()
print("LIST COPY")
check("_and_list uses commas and one 'and'",
      web._and_list(["H-1B", "Green Card", "E-3", "H-1B1"]) == "H-1B, Green Card, E-3 and H-1B1",
      web._and_list(["H-1B", "Green Card", "E-3", "H-1B1"]))
check("_and_list handles one and none",
      web._and_list(["H-1B"]) == "H-1B" and web._and_list([]) == "")

print()
if FAILS:
    print("FAILURES (%d):" % len(FAILS))
    for f in FAILS:
        print("   %s" % f)
    raise SystemExit(1)
print("ALL JOB PAGE CHECKS PASS")
