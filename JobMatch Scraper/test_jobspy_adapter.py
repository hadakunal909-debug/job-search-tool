"""
test_jobspy_adapter.py — guards the JobSpy adapter and the posting fingerprint.

No network, no pandas, and python-jobspy does not need to be installed: the one test that
exercises scrape_jobspy end to end injects a stub module. That is deliberate — the adapter
converts to plain dicts immediately so nothing downstream depends on a DataFrame, and this
file proves it.

Run it directly
    python test_jobspy_adapter.py
or via pytest.

The MUST-NOT-SUPPRESS cases matter most. The fingerprint is the only filter in main() that can
be wrong in the "lost a real job" direction, so each guard has a test that fails if it is
removed. See scraper.fingerprint_duplicate for why each one exists.
"""
import sys
import types

import core
import scraper

best = scraper._jobspy_best_url
fdupe = scraper.fingerprint_duplicate

GH = "https://job-boards.greenhouse.io/acme/jobs/123"


def _index(*rows):
    """{posting_key: [url, ...]} the way main() builds it."""
    out = {}
    for title, company, location, url in rows:
        k = core.posting_key(title, company, location, require_location=True)
        if k:
            out.setdefault(k, []).append(url)
    return out


# --- _jobspy_best_url: the direct link is what makes a row mergeable ------------------
def test_direct_url_wins_over_the_aggregator_page():
    row = {"job_url": "https://www.indeed.com/viewjob?jk=abc",
           "job_url_direct": GH}
    assert best(row) == GH


def test_direct_url_pointing_back_at_an_aggregator_is_refused():
    # Indeed hands out indeed.com/applystart?... in this field often enough to matter; taking it
    # at face value would store an aggregator URL while believing it was the employer's.
    for bad in ("https://www.indeed.com/applystart?jk=abc",
                "https://www.linkedin.com/jobs/view/401",
                "https://www.glassdoor.com/job-listing/x"):
        row = {"job_url": "https://www.indeed.com/viewjob?jk=abc", "job_url_direct": bad}
        assert best(row) == "https://www.indeed.com/viewjob?jk=abc", bad


def test_unusable_direct_url_falls_back():
    for bad in ("", None, "   ", "javascript:void(0)", "not a url", "mailto:a@b.com"):
        row = {"job_url": "https://www.indeed.com/viewjob?jk=abc", "job_url_direct": bad}
        assert best(row) == "https://www.indeed.com/viewjob?jk=abc", repr(bad)


def test_location_accepts_both_shapes():
    assert scraper._jobspy_location({"location": " Boston, MA "}) == "Boston, MA"
    assert scraper._jobspy_location({"city": "Austin", "state": "TX"}) == "Austin, TX"
    assert scraper._jobspy_location({"city": "Remote", "state": None}) == "Remote"
    assert scraper._jobspy_location({}) == ""


# --- the fingerprint: it must catch the relist ----------------------------------------
def test_aggregator_relist_of_a_job_we_hold_is_caught():
    idx = _index(("Project Manager", "Acme", "Boston, MA", GH))
    job = {"title": "Project Manager", "company": "Acme", "location": "Boston, MA",
           "url": "https://www.indeed.com/viewjob?jk=abc"}
    assert fdupe(job, idx) == GH


# --- MUST NOT SUPPRESS: guard 1, an employer-hosted row is ground truth ---------------
def test_employer_hosted_candidate_is_never_suppressed():
    # Two real openings, same title/company/city, both on the employer's own board. Amazon lists
    # 431 "Operations Manager" roles; suppressing the second would delete real inventory. This
    # guard is also what makes a wrong call self-healing.
    idx = _index(("Operations Manager", "Amazon", "Seattle, WA",
                  "https://amazon.jobs/en/jobs/111"))
    job = {"title": "Operations Manager", "company": "Amazon", "location": "Seattle, WA",
           "url": "https://amazon.jobs/en/jobs/222"}
    assert fdupe(job, idx) is None


# --- MUST NOT SUPPRESS: guard 2, look-alikes on ONE host are separate reqs -------------
def test_same_host_lookalikes_stay():
    idx = _index(("Project Manager", "Acme", "Boston, MA",
                  "https://www.indeed.com/viewjob?jk=first"))
    job = {"title": "Project Manager", "company": "Acme", "location": "Boston, MA",
           "url": "https://www.indeed.com/viewjob?jk=second"}
    assert fdupe(job, idx) is None


# --- MUST NOT SUPPRESS: guard 3, two aggregator copies are a plain url duplicate -------
def test_incumbent_on_another_aggregator_is_not_a_match():
    idx = _index(("Project Manager", "Acme", "Boston, MA",
                  "https://www.linkedin.com/jobs/view/401"))
    job = {"title": "Project Manager", "company": "Acme", "location": "Boston, MA",
           "url": "https://www.indeed.com/viewjob?jk=abc"}
    assert fdupe(job, idx) is None


# --- MUST NOT SUPPRESS: a blank location makes the key collide with everything ---------
def test_blank_location_refuses_to_form_a_key():
    assert core.posting_key("PM", "Acme", "", require_location=True) is None
    assert core.posting_key("PM", "Acme", "") == ("pm", "acme", "")   # render-time keeps it
    idx = _index(("Project Manager", "Acme", "Boston, MA", GH))
    job = {"title": "Project Manager", "company": "Acme", "location": "",
           "url": "https://www.indeed.com/viewjob?jk=abc"}
    assert fdupe(job, idx) is None


def test_missing_title_or_company_refuses_to_form_a_key():
    assert core.posting_key("", "Acme", "Boston") is None
    assert core.posting_key("PM", "", "Boston") is None


def test_a_genuinely_new_job_is_not_a_duplicate():
    idx = _index(("Project Manager", "Acme", "Boston, MA", GH))
    job = {"title": "Data Engineer", "company": "Acme", "location": "Boston, MA",
           "url": "https://www.indeed.com/viewjob?jk=abc"}
    assert fdupe(job, idx) is None
    assert fdupe(job, {}) is None                 # empty index never suppresses


def test_company_and_title_normalization_is_punctuation_insensitive():
    idx = _index(("Project Manager", "Acme, Inc.", "Boston, MA", GH))
    job = {"title": "project  manager", "company": "Acme Inc", "location": "boston ma",
           "url": "https://www.indeed.com/viewjob?jk=abc"}
    assert fdupe(job, idx) == GH


# --- wiring -----------------------------------------------------------------------------
def test_jobspy_is_registered_and_exempt_from_the_closed_posting_check():
    assert scraper.SCRAPERS.get("jobspy") is scraper.scrape_jobspy
    # An aggregator returns a QUERY's results, not a board's inventory, so its silence must
    # never be read as "these postings are gone".
    assert "jobspy" in scraper.RECONCILE_SKIP_ATS


def test_each_aggregator_gets_its_own_rate_gate():
    hk = scraper._host_key
    assert hk("jobspy:indeed|pm|US", "jobspy") == "jobspy:indeed"
    assert hk("jobspy:indeed|pm|US", "jobspy") != hk("jobspy:linkedin|pm|US", "jobspy")
    # ...while Adzuna's two entry points still share one, because they share one API key.
    assert hk("adzuna:Tesla", "adzuna") == hk("adzuna-search:pm", "adzuna-search") == "adzuna"


def test_jobspy_has_a_board_timeout_and_other_boards_do_not():
    # JobSpy brings its own HTTP stack and sets no request timeout, so it is the one source that
    # can hang. Everything else is bounded by SESSION and must stay on the untimed fast path.
    assert scraper.SCRAPE_BOARD_TIMEOUT.get("jobspy")
    for ats in ("greenhouse", "workday", "adzuna-search", "lever"):
        assert scraper.SCRAPE_BOARD_TIMEOUT.get(ats) is None, ats


def test_dormant_by_default():
    # JOBSPY_SITES is unset in a normal run: no entries in SOURCES, library never imported.
    assert scraper.JOBSPY_BOARDS == [] or scraper.JOBSPY_SITES
    assert scraper.JOBSPY_FINGERPRINT_ENFORCE is False, "enforce must be opt-in"


# --- the whole adapter, against a stub library ------------------------------------------
class _StubFrame:
    def __init__(self, records):
        self._records = records
        self.empty = not records

    def to_dict(self, orient):
        assert orient == "records"
        return list(self._records)


def _with_stub_jobspy(records, capture=None):
    mod = types.ModuleType("jobspy")

    def scrape_jobs(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return _StubFrame(records)

    mod.scrape_jobs = scrape_jobs
    return mod


def test_adapter_maps_rows_without_pandas():
    records = [
        # direct link present -> stored under the EMPLOYER's url, mergeable with the corpus
        {"title": " Project Manager ", "company": "Acme", "location": "Boston, MA",
         "job_url": "https://www.indeed.com/viewjob?jk=a",
         "job_url_direct": GH + "?utm_source=indeed",
         "date_posted": "2026-08-07", "description": "Lead projects."},
        # no direct link -> falls back to the aggregator url
        {"title": "Data Analyst", "company": "Beta LLC", "city": "Austin", "state": "TX",
         "job_url": "https://www.indeed.com/viewjob?jk=b&from=serp",
         "date_posted": None, "description": ""},
        # duplicate of the first within one query -> collapsed here, before main() sees it
        {"title": "Project Manager", "company": "Acme", "location": "Boston, MA",
         "job_url": "https://www.indeed.com/viewjob?jk=c", "job_url_direct": GH},
        # unusable -> dropped
        {"title": "Ghost", "company": "X", "location": "Y", "job_url": ""},
    ]
    captured = {}
    sys.modules["jobspy"] = _with_stub_jobspy(records, captured)
    scraper.JOBSPY_JDS.clear()
    calls0, rows0 = scraper.JOBSPY_CALLS[0], scraper.JOBSPY_ROWS[0]
    try:
        out = scraper.scrape_jobspy("jobspy:indeed|project manager|United States")
    finally:
        del sys.modules["jobspy"]

    assert [r["title"] for r in out] == ["Project Manager", "Data Analyst"], out
    # the direct link was preferred, and canonicalizing it lands on the corpus's own form
    assert out[0]["url"] == GH + "?utm_source=indeed"
    assert scraper.canonical_url(out[0]["url"]) == GH
    assert out[0]["found_date"] == "2026-08-07"
    assert out[0]["company"] == "Acme"
    # a dateless row keeps NO found_date, so main() stamps it and it ages by first_seen
    assert "found_date" not in out[1]
    assert out[1]["location"] == "Austin, TX"

    # the selector was parsed into the right call
    assert captured["site_name"] == ["indeed"]
    assert captured["search_term"] == "project manager"
    assert captured["location"] == "United States"
    assert captured["country_indeed"] == "usa"
    assert captured["linkedin_fetch_description"] is False

    # JD harvested for free, keyed on the CANONICAL url so main() can match it to a kept row
    assert scraper.JOBSPY_JDS.get(GH) == "Lead projects."
    # spend accounting moved
    assert scraper.JOBSPY_CALLS[0] == calls0 + 1
    assert scraper.JOBSPY_ROWS[0] == rows0 + len(records)


def test_pandas_nan_never_becomes_the_string_nan():
    # NaN is TRUTHY, so `str(v or "")` yields "nan". Measured on the first live run: 9 rows were
    # stored with company="nan", and "nan" MATCHED the federal sponsor data — a false
    # "h1b, green_card, 71 approvals" badge on real postings.
    nan = float("nan")
    assert scraper._text(nan) == ""
    assert scraper._text(None) == ""
    assert scraper._text("  Acme  ") == "Acme"
    assert scraper._text(123) == "123"
    assert scraper._jobspy_location({"city": "Austin", "state": nan}) == "Austin"


def test_adapter_drops_a_row_with_no_employer():
    # A nameless row means no sponsor lookup, no company page and a blank card. The NaN case
    # above is how they arise, so the two guards are tested together.
    records = [
        {"title": "Project Manager", "company": float("nan"), "location": "Durham, NC",
         "job_url": "https://www.indeed.com/viewjob?jk=nan1"},
        {"title": "PM II", "company": "", "location": "Mosinee, WI",
         "job_url": "https://www.indeed.com/viewjob?jk=nan2"},
        {"title": "Real One", "company": "Acme", "location": "Boston, MA",
         "job_url": "https://www.indeed.com/viewjob?jk=ok", "date_posted": float("nan")},
    ]
    sys.modules["jobspy"] = _with_stub_jobspy(records)
    try:
        out = scraper.scrape_jobspy("jobspy:indeed|project manager|United States")
    finally:
        del sys.modules["jobspy"]
    assert [r["company"] for r in out] == ["Acme"], out
    assert "found_date" not in out[0], "a NaN date must not become the string 'nan'"


def test_adapter_reports_an_empty_result_rather_than_swallowing_it():
    # Google has returned 0 and ZipRecruiter 403 since Sept 2025 (JobSpy #302), and a datacenter
    # IP gets refused where a laptop is served. A silent 0 must not look like a quiet day.
    sys.modules["jobspy"] = _with_stub_jobspy([])
    try:
        assert scraper.scrape_jobspy("jobspy:google|project manager|United States") == []
    finally:
        del sys.modules["jobspy"]


def test_adapter_rejects_a_malformed_selector():
    sys.modules["jobspy"] = _with_stub_jobspy([])
    try:
        for bad in ("jobspy:", "jobspy:indeed", "jobspy:|project manager|US"):
            try:
                scraper.scrape_jobspy(bad)
            except ValueError:
                continue
            raise AssertionError("accepted malformed selector %r" % bad)
    finally:
        del sys.modules["jobspy"]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d jobspy-adapter checks passed." % len(fns))
