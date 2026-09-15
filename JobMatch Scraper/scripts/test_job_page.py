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
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import db
import web
import analytics
import scraper.liveness as liveness

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
# ...and the third, which is neither. UNANALYSED holds a FULL description and no analysis of it,
# because the row arrived after the last scoring run. It is the case the owner reported on
# 2026-08-31: the page renders the whole description and the rail above it says "Not scored yet",
# because score_pending is derived from the jd_terms COLUMN while the page had the text in its
# hands the entire time. 169 active rows were in this state when it was measured.
UNANALYSED = "https://boards.example.com/acme/unanalysed"

JD = ("About the Role\n"
      "Build and operate the payments service.\n\n"
      "Basic Qualifications:\n"
      "- Five years of Python in production\n"
      "- Strong SQL and Kubernetes\n\n"
      "What We Offer\n"
      "A short deploy pipeline and real ownership.")

# LONG ENOUGH TO BE SCORED, which JD above is not: it is ~230 characters and
# core._MIN_JD_CHARS is 400, so core.analyze_jd flags it thin and the page is right to refuse a
# number for it. That is not incidental to this file — the first version of the UNANALYSED case
# below reused JD and "failed", and the failure was the thin guard doing its job. A test for
# "the page scores what it just read" needs a description a real scoring run would also accept.
JD_FULL = (
    "About the Role\n"
    "We are hiring a payments engineer to build and operate the settlement service that moves "
    "money between our merchants and their banks. You will own the pipeline end to end, from "
    "the ingestion of ledger events through reconciliation and into the reporting warehouse.\n\n"
    "What You Will Do\n"
    "- Design and ship Python services that process several million ledger events a day\n"
    "- Model and query the settlement data in SQL, and keep the warehouse tables trustworthy\n"
    "- Run the service on Kubernetes, with Terraform describing every piece of its "
    "infrastructure on AWS\n"
    "- Build and schedule the batch jobs in Airflow, and the streaming paths in Spark\n"
    "- Containerise the whole stack with Docker so a new engineer can run it in one command\n\n"
    "Basic Qualifications\n"
    "- Five or more years writing production Python\n"
    "- Strong SQL, and real experience with a columnar warehouse\n"
    "- Working knowledge of Kubernetes, Docker and Terraform on AWS\n\n"
    "What We Offer\n"
    "A short deploy pipeline, real ownership of the service, and a team that reviews code "
    "carefully and ships every day.")

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
    # A real description, and NO jd_terms — which is what makes _row_pending say pending. The
    # match_score is 0 for the same reason the live rows' were: it was scored before the text
    # arrived. Nothing here is exotic; it is the ordinary shape of a row between two cron runs.
    {"url": UNANALYSED, "title": "Payments Engineer", "company": "Acme Corp",
     "location": "Boston, MA", "found_date": "2026-08-06", "match_score": 0,
     "is_active": True, "jd": JD_FULL, "sponsors_h1b": "", "first_seen": "2026-08-06"},
]

# Keep this test off the network and off the database.
web.get_jobs = lambda: [dict(j) for j in JOBS]
# STUBBING get_jobs IS NOT ENOUGH ON ITS OWN. web._corpus_fp() answers the corpus
# fingerprint from a sidecar or the database probe WITHOUT loading the corpus, and
# _base_rows then reads row_cache/ under that key -- so with a real fingerprint in
# reach these synthetic jobs are silently replaced by whatever the developer last
# built. Filling _jobs_cache (rows, a fake fp, and a FRESH `at`) makes the memory
# branch win, which is the same thing .claude/devpreview.py does and for the same
# reason. scripts/run_tests.py also points ROWS_DIR at an empty directory.
web._jobs_cache.update(rows=[dict(j) for j in JOBS], fp=(len(JOBS), "jobpage"),
                       at=time.time())
web._base_rows_cache.update(fp=None, sig=None, rows=None, by_url=None, fresh=0,
                            persisted=None, meta=None)
web._session_dead = lambda u: ""
web._needs_onboarding = lambda u: False
web.current_profile = lambda: "python sql aws docker terraform spark airflow"
web.user_statuses = lambda u: {}
web.sponsor_counts = lambda: {}
web.sponsor_years = lambda: {}
db.get_job_jd = lambda url: next((j["jd"] for j in JOBS if j["url"] == url), "")
db.has_remote_db = lambda: False          # so no research thread is ever started
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
_route = re.search(r'data-route="([a-z_]*)"', body)
check("the route attribute is present", _route is not None,
      _route.group(1) if _route else "no data-route in %d bytes" % len(body))

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
# summary, not resp. "About the Role" is a role SUMMARY, and it used to classify as
# Responsibilities because the summary vocabulary lived inside the resp pattern — so the jump
# strip labelled an overview RESPONSIBILITIES. The six-bucket split is what fixed it.
check("sections are bucketed", 'data-sec="req"' in body and 'data-sec="summary"' in body)
check("a summary is not filed as a responsibility", 'data-sec="resp"' not in body,
      "this fixture has an overview and a qualifications list, and no duties section")
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
# REMOVED FROM THE PAGE 2026-09-03, at the owner's direction and after the trade-off was put
# to them: it is a caveat about our DATA on a page that is about the POSTING. The assertion is
# inverted rather than deleted so the removal stays deliberate -- if it reappears, that should be
# a decision somebody makes again, not a paste.
check("the absence caveat is NOT on the job page", core.VISA_ABSENCE_NOTE not in body,
      repr(core.VISA_ABSENCE_NOTE))
# The RULE it stated is what actually matters and it is unaffected: the constant still exists for
# the scripts that reason about it, and the feed still FLAGS sponsorship rather than filtering on
# it, which is the behaviour the sentence was describing.
check("...but the constant still exists for the code that reasons about it",
      bool(core.VISA_ABSENCE_NOTE))
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
# The copy stopped claiming "too short" on 2026-08-31: the server cannot know that. Feed rows
# do not carry the jd column and jd_unavailable is decided by a HOST blocklist, so a NULL
# jd_terms means "not analysed yet" and nothing about the length -- 30 rows carrying a real
# description were being told they were too short. What this suite actually freezes is the
# DISTINCTION below, not the wording: pending promises a score, blocked must not.
check("a pending row renders and says it is not scored yet",
      r.status_code == 200 and "Not scored yet" in body, str(r.status_code))
check("a pending row identifies the pending analysis",
      'data-score-state="pending"' in body and "Awaiting description analysis" in body)
check("a pending row does NOT claim the employer publishes nothing",
      "doesn" not in body.split("Profile Match")[-1][:400].replace("doesn't publish", "X"))

r = get(BLOCKED)
body = r.data.decode("utf-8", "replace")
check("a blocked row renders", r.status_code == 200, str(r.status_code))
check("a blocked row identifies an inaccessible description",
      'data-score-state="unavailable"' in body and "could not be accessed" in body)
check("a blocked row does NOT promise a score is coming",
      'data-score-state="pending"' not in body and "Not scored yet" not in body,
      "the pending copy promises a score; for a blocked employer one never arrives")

rows = {j["url"]: web._build_row(j, 0) for j in JOBS if j["url"] in (PENDING, BLOCKED)}
check("jd_unavailable is set only for the walled host",
      rows[BLOCKED]["jd_unavailable"] is True and rows[PENDING]["jd_unavailable"] is False)
check("both are still score_pending, so neither shows a fake 0%",
      rows[BLOCKED]["score_pending"] and rows[PENDING]["score_pending"])

# ---------------------------------------------------------------------------------------------
# WHICH VERDICTS COUNT AS "no description is coming". The block above stubs the resolved host
# SET; this exercises the step that builds it from the KV row close_dead_jds writes.
#
# Only "blocked" counted until 2026-09-02, and the larger class was "unknown" -- a 200 with a
# real page and nothing extractable, which is what a client-rendered apply app looks like
# (Actalent serves one 448 KB shell for every job). Both mean the same thing to a reader, so
# both must set the badge; the rest must not.
_saved_hosts = web._jd_blocked_hosts
_saved_get_kv = db.get_kv
VERDICTS = {"blocked": "walled.example.com", "unknown": "shell.example.com",
            "gone": "dead.example.com", "transient": "flaky.example.com",
            "mixed": "disagreed.example.com", "readable": "fine.example.com"}
db.get_kv = lambda key: ({"hosts": {h: {"verdict": v} for v, h in VERDICTS.items()}}
                         if key == web._JD_VERDICT_KEY else {})
web._jd_blocked_hosts = None                    # force the cached read to happen again
try:
    for verdict, host in sorted(VERDICTS.items()):
        want = verdict in ("blocked", "unknown")
        got = web._host_jd_blocked("https://%s/careers/1" % host)
        check("verdict %-10s -> %s" % (verdict, "unavailable" if want else "still pending"),
              got is want, "got %r" % got)
    check("'mixed' is close_dead_jds' own label for probes that DISAGREED, not a finding",
          "mixed" not in web._JD_UNREADABLE)
    check("every verdict web.py acts on is one liveness.classify can actually return",
          web._JD_UNREADABLE <= set(liveness.VERDICT_NOTES) | {"blocked", "unknown"})
finally:
    db.get_kv = _saved_get_kv
    web._jd_blocked_hosts = _saved_hosts
    web._rows_cache.clear()

print()
print("A ROW THAT HOLDS A DESCRIPTION NOBODY ANALYSED — the reported defect")
# The cache is cleared first ON PURPOSE. jd_meta() stores its analysis under the url, and
# _row_pending reads that cache BEFORE the column — so a second visit takes a different code
# path and would pass without the fix. The cold path is the one that was broken.
web._jdmeta.pop(UNANALYSED, None)
web._rows_cache.clear()
pending_row = web._build_row(next(j for j in JOBS if j["url"] == UNANALYSED), 0)
check("the row really is score_pending before the page runs",
      pending_row["score_pending"] is True and pending_row["jd_unavailable"] is False,
      "otherwise this test proves nothing")

web._jdmeta.pop(UNANALYSED, None)
web._rows_cache.clear()
r = get(UNANALYSED)
body = r.data.decode("utf-8", "replace")
check("it renders", r.status_code == 200, str(r.status_code))
check("the description is on the page", "settlement service" in body)
check("and it is one a scoring run would also accept",
      not core.analyze_jd(JD_FULL).get("thin"),
      "if this were thin the page would be RIGHT to withhold a number")
check("and so is a real percentage", re.search(r'class="meter-\w+">\d+%<', body) is not None,
      "the page analysed this description; it must show what that came to")
check("it does NOT also say it has not been read",
      "Not scored yet" not in body and 'data-score-state="pending"' not in body,
      "a page cannot render a description and call itself unread in the same breath")
# The event has to agree with the page for the same reason: "how many of the jobs I open have no
# score" is one of the few numbers here worth trusting.
opens = [p for n, p in EMITS if n == "job_open"]
check("job_open reports it as scored, not pending",
      len(opens) == 1 and opens[0]["pending"] is False and opens[0]["score"] > 0,
      repr(opens[0] if opens else None))

# THIN IS NOT THE SAME QUESTION and must keep the pending note: a score derived from a handful
# of generic terms is the fake ~100% core.analyze_jd's thin flag exists to prevent.
# FORCED AT core.job_meta, not at web.jd_meta, and the move is the point. Thinness originates
# there, and TWO readers now depend on it: web.jd_meta, which is how the PAGE analyses a
# description on demand, and web._live_analysis, which is how the CARD does. Stubbing only the
# page's door left the card scoring a description the page was refusing to score -- the exact
# card/page disagreement this file exists to catch, reappearing in the test's own scaffolding.
web._jdmeta.pop(UNANALYSED, None)
web._rows_cache.clear()
web._live_meta.update(fp=None, by_url={})
_real_meta = core.job_meta


def _thin_meta(jd, idf):
    m = dict(_real_meta(jd, idf))
    m["analyzed"] = dict(m["analyzed"], thin=True)
    return m


core.job_meta = _thin_meta
try:
    body = get(UNANALYSED).data.decode("utf-8", "replace")
finally:
    core.job_meta = _real_meta
    web._jdmeta.pop(UNANALYSED, None)
    web._rows_cache.clear()
    web._live_meta.update(fp=None, by_url={})
check("a THIN description still says it is not scored",
      "Not scored yet" in body,
      "an honest 'we have not read this' beats a confident wrong number")

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
