"""
test_canonical_url.py — guards scraper.canonical_url(), which keeps one posting from
landing in the url-keyed `jobs` table twice.

No external test deps: run it directly
    python test_canonical_url.py
or via pytest if you have it (functions are named test_*).

The MUST-NOT-MERGE cases below are not hypothetical — each was measured against the live
corpus while writing this. Stripping gh_jid unconditionally collapsed 64 distinct Stripe
postings into one row; dropping the #fragment collapsed 424 JobDiva postings into one.
Keep those tests passing and over-aggressive normalization can't come back.
"""
import scraper

cu = scraper.canonical_url


# --- the bug this exists to fix: Greenhouse's two interchangeable hostnames ------------
def test_greenhouse_host_variants_collapse():
    old = "https://boards.greenhouse.io/flexport/jobs/7450536?gh_jid=7450536"
    new = "https://job-boards.greenhouse.io/flexport/jobs/7450536"
    assert cu(old) == cu(new) == new


def test_greenhouse_redundant_ghjid_dropped_only_when_it_matches_the_path():
    # redundant -> dropped
    assert cu("https://boards.greenhouse.io/acme/jobs/123?gh_jid=123") == \
        "https://job-boards.greenhouse.io/acme/jobs/123"
    # NOT redundant (points at a different posting than the path) -> kept, so two
    # genuinely different URLs stay two rows
    kept = cu("https://job-boards.greenhouse.io/acme/jobs/123?gh_jid=999")
    assert "gh_jid=999" in kept


# --- MUST NOT MERGE: gh_jid is the only identifier on company-hosted Greenhouse boards -
def test_company_hosted_ghjid_is_load_bearing():
    a = cu("https://stripe.com/jobs/search?gh_jid=7061338")
    b = cu("https://stripe.com/jobs/search?gh_jid=7176530")
    assert a != b, "stripping gh_jid off a non-greenhouse host merges distinct postings"
    assert "gh_jid=7061338" in a

    for base in ("https://www.mongodb.com/careers/job/?gh_jid=%s",
                 "https://www.pinterestcareers.com/jobs/?gh_jid=%s",
                 "https://instacart.careers/job/?gh_jid=%s",
                 "https://careers.datadoghq.com/detail/7065807/?gh_jid=%s"):
        assert cu(base % "111") != cu(base % "222"), base


# --- MUST NOT MERGE: JobDiva's portal is hash-routed ----------------------------------
def test_fragment_is_load_bearing():
    tok = "svjdnwzkulao5hqo7t0ifgvj8s71sf01d7dtgdstyhdixakxt6ty85zljsdyhgz2"
    a = cu("https://www1.jobdiva.com/portal/?a=%s#/jobs/25503098" % tok)
    b = cu("https://www1.jobdiva.com/portal/?a=%s#/jobs/26292521" % tok)
    assert a != b, "dropping the fragment merges every JobDiva posting into one"
    assert a.endswith("#/jobs/25503098")


# --- the plain normalizations ---------------------------------------------------------
def test_scheme_and_host_case():
    assert cu("http://Job-Boards.Greenhouse.IO/acme/jobs/5") == \
        "https://job-boards.greenhouse.io/acme/jobs/5"


def test_trailing_slash():
    assert cu("https://jobs.sap.com/job/Allen-Lead/1234/") == \
        "https://jobs.sap.com/job/Allen-Lead/1234"
    assert cu("https://example.com/") == "https://example.com/"     # bare root survives


def test_tracking_params_dropped_but_real_ones_kept():
    got = cu("https://ex.com/job?id=42&utm_source=li&utm_campaign=x&gh_src=abc&ref=keep")
    assert "id=42" in got and "ref=keep" in got
    assert "utm_" not in got and "gh_src" not in got


def test_adzuna_se_token_dropped_so_one_ad_is_one_row():
    # Adzuna mints a fresh `se=` on every API response, so the SAME advert arrived with a new url
    # each run and the url-keyed table stored it again. Measured on the live corpus 2026-08-09:
    # 473 ad ids held more than one row, 1,293 surplus rows, one ad stored seven times — identical
    # id, v=, title, company and found_date, differing only in `se=`.
    a = cu("https://www.adzuna.com/land/ad/5817622855?se=1h3UxlOT8RGWPoJqQXFPnw&v=B4F232F8")
    b = cu("https://www.adzuna.com/land/ad/5817622855?se=zs7XSXqS8RG9ks23jnlnsA&v=B4F232F8")
    assert a == b, (a, b)
    # v= is NOT dropped: it was identical across all seven copies, so it isn't what split them,
    # and this module only removes what provably cannot identify a posting.
    assert "v=B4F232F8" in a


def test_adzuna_se_rule_is_host_scoped():
    # "se" is two letters and could identify a posting on some other board, so the rule is scoped
    # to Adzuna's hosts rather than added to _TRACKING_PARAMS — same reasoning as gh_jid.
    assert "se=KEEPME" in cu("https://jobs.example.com/apply?se=KEEPME&id=7")
    assert "se=KEEPME" in cu("https://boards.greenhouse.io/acme/jobs/123?gh_jid=123&se=KEEPME")


# --- Indeed: the one host with an ALLOW-list, because its session tail keeps growing ---
def test_indeed_session_tail_dropped_so_one_ad_is_one_row():
    # The same posting arrives with a different referral tail depending on where it was found.
    # Only jk names the posting.
    a = cu("https://www.indeed.com/viewjob?jk=abc123&from=serp&tk=1h9qk&vjs=3")
    b = cu("https://www.indeed.com/viewjob?jk=abc123&from=rss&advn=99&acatk=xyz&xpse=q")
    assert a == b == "https://www.indeed.com/viewjob?jk=abc123", (a, b)


# --- MUST NOT MERGE: jk is the posting id, the same way gh_jid is on a company board ---
def test_indeed_two_jk_values_stay_two_rows():
    a = cu("https://www.indeed.com/viewjob?jk=1111111111111111")
    b = cu("https://www.indeed.com/viewjob?jk=2222222222222222")
    assert a != b, "the allow-list must keep jk — dropping it merges every Indeed posting"


def test_indeed_vjk_is_the_id_when_jk_is_absent():
    # On a search-results page the highlighted posting is named by vjk, not jk.
    got = cu("https://www.indeed.com/jobs?q=project+manager&vjk=deadbeef&from=web")
    assert got == "https://www.indeed.com/jobs?vjk=deadbeef", got
    # ...but when both are present, jk is the posting and vjk is just what was highlighted.
    both = cu("https://www.indeed.com/viewjob?jk=real&vjk=other")
    assert both == "https://www.indeed.com/viewjob?jk=real", both


def test_indeed_allowlist_only_fires_when_an_id_param_is_present():
    # A company listing page or a saved search carries no jk/vjk. Stripping its whole query
    # would merge unrelated URLs, so the rule stands down and the ordinary deny-list applies.
    raw = "https://www.indeed.com/cmp/Acme/jobs?q=project+manager"
    assert cu(raw) == raw
    assert "utm_source" not in cu(raw + "&utm_source=email")


def test_indeed_allowlist_is_host_scoped():
    # `from` and `tk` are ordinary params on any other board — only Indeed treats them as noise.
    assert cu("https://jobs.example.com/viewjob?jk=abc&from=serp") == \
        "https://jobs.example.com/viewjob?jk=abc&from=serp"


# --- LinkedIn -------------------------------------------------------------------------
def test_linkedin_tracking_dropped():
    got = cu("https://www.linkedin.com/jobs/view/4012345678"
             "?refId=abc&trackingId=xyz%3D&position=3&pageNum=0&trk=public_jobs")
    assert got == "https://www.linkedin.com/jobs/view/4012345678", got


# --- MUST NOT MERGE: currentJobId is the posting on a /jobs/search/ URL ----------------
def test_linkedin_currentjobid_is_load_bearing():
    a = cu("https://www.linkedin.com/jobs/search/?currentJobId=4011111111&refId=a")
    b = cu("https://www.linkedin.com/jobs/search/?currentJobId=4022222222&refId=b")
    assert a != b, "dropping currentJobId merges every LinkedIn search-page posting into one"
    assert "currentJobId=4011111111" in a and "refId" not in a


# --- Workday's optional locale segment -------------------------------------------------
def test_workday_locale_prefix_dropped_so_one_posting_is_one_row():
    # Found by probing Indeed's direct links against the corpus 2026-08-09: our Workday scraper
    # emits the bare form, aggregators hand out the /en-US/ form, and 31 of 7,941 stored Workday
    # rows already carry a locale — so both shapes were in the table before any aggregator.
    bare = "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site/job/CA---SF/PM_123"
    loc = "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site/job/CA---SF/PM_123"
    # The SITE SEGMENT COMES BACK LOWERCASED, which is the point: Workday serves one
    # requisition under whatever casing the link used, and `url` is the primary key on `jobs`,
    # so /External_Career_Site/ and /external_career_site/ were two rows for one posting. So
    # the canonical form is not `bare` -- it is `bare` with that one segment folded.
    canon = "https://salesforce.wd12.myworkdayjobs.com/external_career_site/job/CA---SF/PM_123"
    assert cu(loc) == cu(bare) == canon, (cu(loc), cu(bare))
    assert cu("https://msd.wd5.myworkdayjobs.com/en-GB/SearchJobs/job/Kansas/PM_9") == \
        "https://msd.wd5.myworkdayjobs.com/searchjobs/job/Kansas/PM_9"


def test_workday_locale_rule_does_not_eat_a_real_site_name():
    # Only an exact xx-XX FIRST segment goes. A site name is never that shape, and two different
    # Workday sites must stay two different URLs.
    keep = "https://acme.wd1.myworkdayjobs.com/abbottcareers/job/US/PM_1"
    assert cu(keep) == keep
    a = cu("https://acme.wd1.myworkdayjobs.com/External/job/US/PM_1")
    b = cu("https://acme.wd1.myworkdayjobs.com/Internal/job/US/PM_1")
    assert a != b
    # ...and the rule is host-scoped: an xx-XX segment elsewhere is left alone.
    other = "https://jobs.example.com/en-US/job/123"
    assert cu(other) == other


# --- MUST NOT MERGE: two Workday postings differing only in job id ---------------------
def test_workday_two_postings_stay_two_rows():
    base = "https://salesforce.wd12.myworkdayjobs.com/en-US/External_Career_Site/job/CA---SF/%s"
    assert cu(base % "PM_123") != cu(base % "PM_456")


def test_lever_source_param_dropped():
    # jobs.lever.co/<co>/<uuid> — the uuid is the identity; lever-source names the referral.
    # All 180 stored Lever rows are bare while an aggregator hands out ?lever-source=Indeed.
    bare = "https://jobs.lever.co/qualdoc/75b2e180-876a-45dd-ac40-d83a3bf2224a"
    assert cu(bare + "?lever-source=Indeed") == bare
    # two different postings still stay apart
    assert cu("https://jobs.lever.co/acme/aaa?lever-source=x") != \
        cu("https://jobs.lever.co/acme/bbb?lever-source=x")


# --- the payoff: an aggregator's direct link lands on the row we already have ----------
def test_jobspy_direct_url_merges_with_the_direct_scraper():
    # JobSpy hands back job_url_direct — the employer's own ATS link, usually tagged with the
    # aggregator as the referral source. Canonicalized it must equal what scrape_greenhouse
    # produced for the same posting, or the url-keyed table holds the job twice.
    from_aggregator = cu("https://boards.greenhouse.io/acme/jobs/123"
                         "?gh_jid=123&utm_source=indeed&gh_src=abc")
    from_direct_scraper = cu("https://job-boards.greenhouse.io/acme/jobs/123")
    assert from_aggregator == from_direct_scraper, (from_aggregator, from_direct_scraper)


def test_query_untouched_when_nothing_is_dropped():
    # No param removed -> the query string is passed through verbatim, so re-encoding
    # can never rewrite a URL we meant to leave alone.
    raw = "https://ex.com/j?b=2&a=1&x=hello+world&y=a%2Bb"
    assert cu(raw) == raw


def test_param_order_and_duplicates_preserved():
    raw = "https://ex.com/j?z=1&a=2&z=3"
    assert cu(raw) == raw           # NOT sorted/deduped: some boards are order-sensitive


def test_non_http_and_junk_pass_through_unchanged():
    for bad in ("javascript:alert(1)", "data:text/html,x", "", None, "not a url",
                "mailto:a@b.com"):
        assert cu(bad) == bad


def test_idempotent():
    for u in ("https://boards.greenhouse.io/flexport/jobs/7450536?gh_jid=7450536",
              "https://stripe.com/jobs/search?gh_jid=7061338",
              "https://www1.jobdiva.com/portal/?a=tok#/jobs/1",
              "https://jobs.sap.com/job/x/1/",
              "https://ex.com/job?id=42&utm_source=li",
              "https://www.indeed.com/viewjob?jk=abc123&from=serp&tk=1h9qk",
              "https://www.indeed.com/jobs?q=pm&vjk=deadbeef",
              "https://www.linkedin.com/jobs/view/4012345678?refId=abc&position=3",
              "https://www.linkedin.com/jobs/search/?currentJobId=401&trk=x",
              "https://x.wd1.myworkdayjobs.com/en-US/Site/job/US/PM_1",
              "https://jobs.lever.co/acme/uuid-1?lever-source=Indeed"):
        assert cu(cu(u)) == cu(u), u


def test_port_is_preserved():
    assert cu("https://ex.com:8443/job/1") == "https://ex.com:8443/job/1"


def test_ipv6_host_left_alone():
    # urlsplit().hostname drops the [brackets]; rebuilding would emit a malformed URL.
    raw = "http://[2001:db8::1]/job/1/"
    assert cu(raw) == raw


# --- the one-off migration's merge decisions (scraper/dedupe_urls.py) -----------------
def test_keeper_prefers_the_row_already_at_the_canonical_url():
    from scraper import dedupe_urls as d
    canon = "https://job-boards.greenhouse.io/acme/jobs/1"
    old = {"url": "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1", "match_score": 99}
    new = {"url": canon, "match_score": 3}
    # the richer row loses to the canonical one — no row has to be recreated
    assert d._pick_keeper(canon, [old, new])["url"] == canon
    # ...but with no row at the canonical URL, the richest is promoted
    assert d._pick_keeper(canon, [old])["url"] == old["url"]


def test_merge_takes_the_higher_score_and_earliest_date():
    from scraper import dedupe_urls as d
    keeper = {"url": "a", "match_score": 11, "found_date": "2026-05-29", "title": "PM"}
    loser = {"url": "b", "match_score": 13, "found_date": "2026-04-01", "title": "PM"}
    patch = d._merge_fields(keeper, [loser])
    assert patch["match_score"] == 13            # best score in the group survives
    assert patch["found_date"] == "2026-04-01"   # true posting date is the earliest


def test_merge_never_overwrites_a_value_the_keeper_already_has():
    from scraper import dedupe_urls as d
    keeper = {"url": "a", "title": "Real Title", "company": "Acme",
              "location": "Boston, MA", "match_score": 40}
    loser = {"url": "b", "title": "Other", "company": "Other Inc",
             "location": "Austin, TX", "match_score": 5}
    patch = d._merge_fields(keeper, [loser])
    for field in ("title", "company", "location", "match_score"):
        assert field not in patch, field


def test_merge_backfills_only_blank_keeper_fields():
    from scraper import dedupe_urls as d
    keeper = {"url": "a", "title": "PM", "location": "", "posted_verified": ""}
    loser = {"url": "b", "title": "PM", "location": "Chicago, IL",
             "posted_verified": "2026-06-01"}
    patch = d._merge_fields(keeper, [loser])
    assert patch["location"] == "Chicago, IL"
    assert patch["posted_verified"] == "2026-06-01"


def test_workday_site_segment_is_case_normalised():
    """One posting, two casings, one row.

    Workday serves the same requisition under whatever casing the link carried, and `url` is the
    primary key on `jobs` — so /external/… and /External/… were stored TWICE. That is the root
    cause of Applied Materials appearing in the feed as two employers ("Amat" and "Applied
    Materials"), 117 openings split across two /companies entries, a monogram instead of a logo
    on one of them, and /job listing a posting as similar to itself.
    """
    import scraper
    a = "https://amat.wd1.myworkdayjobs.com/external/job/Santa-ClaraCA/Data-Scientist_R2624867"
    b = "https://amat.wd1.myworkdayjobs.com/External/job/Santa-ClaraCA/Data-Scientist_R2624867"
    assert scraper.canonical_url(a) == scraper.canonical_url(b)
    # ...and with a locale prefix, which is stripped first, so the SITE is still what gets folded.
    c = "https://amat.wd1.myworkdayjobs.com/en-US/External/job/Santa-ClaraCA/Data-Scientist_R2624867"
    assert scraper.canonical_url(c) == scraper.canonical_url(a)


def test_workday_normalisation_does_not_touch_the_requisition_id():
    """Only the site segment folds. The req id and the title slug are case-sensitive, and two
    genuinely different postings must not collapse into one row."""
    import scraper
    base = "https://amat.wd1.myworkdayjobs.com/external/job/Santa-ClaraCA/"
    assert scraper.canonical_url(base + "Data-Scientist_R2624867") != \
           scraper.canonical_url(base + "Data-Scientist_r2624867")
    keep = scraper.canonical_url(base + "Data-Scientist_R2624867")
    assert "Data-Scientist_R2624867" in keep, keep


def test_non_workday_paths_keep_their_case():
    """The fold is Workday-specific on purpose — plenty of ATS paths mean different things in
    different cases, so this must not become a general lowercase."""
    import scraper
    u = "https://job-boards.greenhouse.io/Acme/jobs/Some-Role_1234"
    assert "Acme" in scraper.canonical_url(u)


def test_score_coercion_handles_both_backends():
    from scraper import dedupe_urls as d
    assert d._score({"match_score": 7}) == 7           # Supabase: number
    assert d._score({"match_score": "7"}) == 7         # local CSV: string
    for bad in ({}, {"match_score": None}, {"match_score": ""}, {"match_score": "n/a"}):
        assert d._score(bad) is None, bad


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d canonical-url checks passed." % len(fns))
