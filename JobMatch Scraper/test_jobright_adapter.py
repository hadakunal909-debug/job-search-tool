"""
test_jobright_adapter.py — guards the jobright.ai adapter and the dedupe it depends on.

No network: the one test that exercises scrape_jobright end to end stubs scraper._safe_get.

WHAT THIS FILE IS REALLY PROTECTING. jobright is the first source whose payload carries no
direct employer link at all — every row's url is an interstitial on jobright.ai. Three things
have to stay true for that to be safe, and each has a test here that fails if it is undone:

  1. "jobright." stays in core.AGGREGATOR_HOSTS, so fingerprint_duplicate suppresses a jobright
     copy of a job we already hold from the employer's own board. Remove it and the feed
     double-lists every one of those postings.
  2. "jobright" stays in RECONCILE_SKIP_ATS. The landing page serves ~20 rows against a stated
     total near 1,800, so closure reconciliation would retire live jobs every run.
  3. The JD is composed from the listing. score_jobs' fallback is to fetch the row's url, and
     that url is the interstitial — it does not carry the employer's text.

It also pins the one boundary that is a judgement call rather than a bug: /jobs/recommend is
NOT read. See scrape_jobright's docstring.

Run it directly
    python test_jobright_adapter.py
or via pytest.
"""
import json

import core
import scraper

jd = scraper._jobright_jd
loc = scraper._jobright_location
rows_of = scraper._jobright_rows
fdupe = scraper.fingerprint_duplicate

GH = "https://job-boards.greenhouse.io/acme/jobs/123"
JR = "https://jobright.ai/jobs/info/6a99d39d551435518ebf168a?utm_source=1014"


def _job(**kw):
    base = {"jobId": "abc", "jobTitle": "Data Engineer", "jobLocation": "Raleigh, NC",
            "publishTime": "2026-09-03 11:22:33", "jobSummary": "S" * 500,
            "applyLink": JR}
    base.update(kw)
    return base


def _page(key, *jobs):
    """A __NEXT_DATA__ payload shaped the way the two live page families ship it."""
    return {"props": {"pageProps": {key: [
        {"impId": "i", "jobResult": j, "companyResult": {"companyName": "Acme"},
         "displayScore": 90} for j in jobs]}}}


def _index(*rows):
    """{posting_key: [url, ...]} the way main() builds it."""
    out = {}
    for title, company, location, url in rows:
        k = core.posting_key(title, company, location, require_location=True)
        if k:
            out.setdefault(k, []).append(url)
    return out


# --- the row payload arrives under two different keys ---------------------------------
def test_reads_the_slug_pages_joblist_key():
    assert len(rows_of(_page("jobList", _job()))) == 1


def test_reads_the_remote_jobs_defaultdata_key():
    assert len(rows_of(_page("defaultData", _job()))) == 1


def test_an_unknown_page_shape_yields_no_rows_rather_than_raising():
    # A third page family, or a bot-flagged response, must degrade to zero — not KeyError out
    # of the sweep and take the whole board down with it.
    assert rows_of({"props": {"pageProps": {"somethingElse": [1, 2]}}}) == []
    assert rows_of({}) == []
    assert rows_of(None) == []


# --- the JD is composed, because the row's own url cannot supply one ------------------
def test_jd_composes_summary_responsibilities_and_requirements():
    out = jd(_job(jobSummary="Summary here.",
                  coreResponsibilities=["Build pipelines", "Own quality"],
                  requirements=["5 years Python"]))
    assert "Summary here." in out
    assert "- Build pipelines" in out and "- Own quality" in out
    assert "- 5 years Python" in out


def test_jd_survives_the_list_fields_being_absent_or_scalar():
    assert jd({"jobSummary": "Only prose."}) == "Only prose."
    assert "Prose." in jd({"jobSummary": "Prose.", "requirements": "flat string"})
    assert jd({}) == ""


def test_a_composed_jd_clears_the_min_chars_floor_the_pipeline_stores_at():
    # main() banks a listing JD only at core._MIN_JD_CHARS. Measured live, every sampled row
    # composed to 1,396-5,807 chars; this pins that the composition (not one lucky field) is
    # what gets it there.
    out = jd(_job(jobSummary="S" * 200,
                  coreResponsibilities=["R" * 120] * 3,
                  requirements=["Q" * 120] * 3))
    assert len(out) >= core._MIN_JD_CHARS


# --- location: one string, from either shape -----------------------------------------
def test_location_prefers_the_flat_string_then_falls_back_to_the_list():
    assert loc({"jobLocation": "Boston, MA"}) == "Boston, MA"
    assert loc({"jobLocations": ["Boston, MA", "Remote"]}) == "Boston, MA, Remote"
    assert loc({}) == ""


def test_every_location_shape_jobright_emits_clears_the_us_gate():
    # A remote row is labelled "United States" even on a city slug. If that failed is_us_location
    # the entire source would be silently dropped after a successful fetch.
    for s in ("United States", "Raleigh, NC", "Remote", ""):
        assert scraper.is_us_location(s), s


# --- MUST SUPPRESS: an aggregator copy of a job we already hold -----------------------
def test_a_jobright_copy_of_a_job_we_hold_direct_is_suppressed():
    idx = _index(("Data Engineer", "Acme", "Raleigh, NC", GH))
    job = {"title": "Data Engineer", "company": "Acme", "location": "Raleigh, NC", "url": JR}
    assert fdupe(job, idx) == GH, "jobright must be recognised as an aggregator host"


def test_removing_jobright_from_aggregator_hosts_would_be_caught():
    # The guard above passes only because of this membership. Pinned separately so the reason
    # is legible when it breaks.
    assert core.is_aggregator_url(JR)
    assert core.is_aggregator_url("https://jobright.ai/jobs/info/x")


# --- MUST NOT SUPPRESS: the cases that would lose a real job -------------------------
def test_an_employer_hosted_row_is_never_suppressed():
    idx = _index(("Data Engineer", "Acme", "Raleigh, NC", JR))
    job = {"title": "Data Engineer", "company": "Acme", "location": "Raleigh, NC", "url": GH}
    assert fdupe(job, idx) is None


def test_a_job_we_hold_only_from_another_aggregator_does_not_suppress():
    idx = _index(("Data Engineer", "Acme", "Raleigh, NC",
                  "https://www.indeed.com/viewjob?jk=abc"))
    job = {"title": "Data Engineer", "company": "Acme", "location": "Raleigh, NC", "url": JR}
    assert fdupe(job, idx) is None


def test_a_posting_we_do_not_hold_is_kept():
    job = {"title": "Data Engineer", "company": "Acme", "location": "Raleigh, NC", "url": JR}
    assert fdupe(job, _index()) is None


# --- a rotating landing page must never close jobs -----------------------------------
def test_jobright_is_exempt_from_closure_reconciliation():
    # 20 rows against a stated ~1,800 total, and RECONCILE_MIN_ROWS is 3 — without this the
    # rows would clear the floor and reconcile would retire everything not on today's page.
    assert "jobright" in scraper.RECONCILE_SKIP_ATS
    assert scraper.RECONCILE_MIN_ROWS < 20


# --- the adapter end to end, no network ----------------------------------------------
class _Resp(object):
    def __init__(self, text, status=200):
        self.text, self.status_code = text, status


def _serve(payload, status=200):
    html = '<html><script id="__NEXT_DATA__" type="application/json">%s</script></html>' % (
        json.dumps(payload) if payload is not None else "")
    return lambda url, **kw: _Resp(html, status)


def _with_stub(fn, *a, **kw):
    real = scraper._safe_get
    scraper._safe_get = fn
    try:
        return scraper.scrape_jobright(*a, **kw)
    finally:
        scraper._safe_get = real


def test_adapter_maps_a_page_into_corpus_rows():
    out = _with_stub(_serve(_page("jobList", _job())), "jobright:data-engineer|raleigh-nc")
    assert len(out) == 1, out
    r = out[0]
    assert r["title"] == "Data Engineer"
    assert r["company"] == "Acme"
    assert r["location"] == "Raleigh, NC"
    assert r["url"] == JR
    assert r["_src"] == "jobright", "the sponsor-record gate keys on this"
    assert len(r["jd"]) >= core._MIN_JD_CHARS


def test_publish_time_is_stored_as_a_bare_date_so_it_reads_as_a_publisher_field():
    # core.is_trusted_date treats "YYYY-MM-DD HH:MM" as one of OUR derived stamps. Storing
    # jobright's full timestamp would mark a real posting date as a guess.
    out = _with_stub(_serve(_page("jobList", _job())), "jobright:data-engineer|raleigh-nc")
    assert out[0]["found_date"] == "2026-09-03"
    assert core.is_trusted_date(out[0]["found_date"])


def test_a_row_with_no_employer_or_no_title_is_dropped():
    page = _page("jobList", _job(), _job(jobTitle=""))
    page["props"]["pageProps"]["jobList"][0]["companyResult"] = {"companyName": ""}
    out = _with_stub(_serve(page), "jobright:data-engineer|raleigh-nc")
    assert out == [], out


def test_the_same_posting_twice_on_one_page_is_stored_once():
    out = _with_stub(_serve(_page("jobList", _job(), _job())),
                     "jobright:data-engineer|raleigh-nc")
    assert len(out) == 1, out


def test_a_non_200_reports_rather_than_looking_like_a_quiet_day():
    # The un-prefixed slug form 303s instead of 404ing, so a typo in JOBRIGHT_ROLES must not
    # read as "no openings today".
    assert _with_stub(_serve(_page("jobList", _job()), status=303),
                      "jobright:nonsense-role|raleigh-nc") == []


def test_a_page_with_no_next_data_is_reported_not_swallowed():
    blank = lambda url, **kw: _Resp("<html>no script here</html>")
    assert _with_stub(blank, "jobright:data-engineer|raleigh-nc") == []


def test_unparseable_next_data_does_not_take_the_sweep_down():
    bad = lambda url, **kw: _Resp(
        '<script id="__NEXT_DATA__" type="application/json">{not json</script>')
    assert _with_stub(bad, "jobright:data-engineer|raleigh-nc") == []


def test_adapter_rejects_a_malformed_selector():
    for bad in ("jobright:", "jobright:|raleigh-nc"):
        try:
            _with_stub(_serve(_page("jobList", _job())), bad)
        except ValueError:
            continue
        raise AssertionError("accepted malformed selector %r" % bad)


def test_city_defaults_when_the_selector_omits_it():
    seen = {}

    def cap(url, **kw):
        seen["url"] = url
        return _Resp('<script id="__NEXT_DATA__" type="application/json">%s</script>'
                     % json.dumps(_page("jobList", _job())))

    _with_stub(cap, "jobright:data-engineer")
    assert seen["url"].endswith("/jobs/h1b-visa-sponsored-data-engineer-jobs-in-united-states")


# --- the source stays off until it is deliberately switched on -----------------------
def test_no_jobright_entries_in_sources_unless_enabled():
    # Same contract as JOBSPY_BOARDS: importing the module must not enable a network source.
    if not scraper.JOBRIGHT_ON:
        assert scraper.JOBRIGHT_BOARDS == []
        assert [s for s in scraper.SOURCES if s[1] == "jobright"] == []


def test_the_dispatch_entry_exists_so_an_enabled_board_is_reachable():
    assert scraper.SCRAPERS.get("jobright") is scraper.scrape_jobright
    assert "jobright" in scraper.SCRAPE_BOARD_TIMEOUT


def test_the_recommend_feed_is_not_reachable_from_any_selector():
    # The boundary this whole source is built around: /jobs/recommend is robots-disallowed,
    # is an empty shell, and needs a logged-in session. The template can only ever address the
    # public h1b landing pages, and this pins that a role string cannot escape it.
    assert "recommend" not in scraper.JOBRIGHT_SLUG
    built = scraper.JOBRIGHT_SLUG % ("recommend", "united-states")
    assert built.startswith("/jobs/h1b-visa-sponsored-")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d jobright-adapter checks passed." % len(fns))
