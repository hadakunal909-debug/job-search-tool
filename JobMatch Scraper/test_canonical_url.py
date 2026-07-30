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
              "https://ex.com/job?id=42&utm_source=li"):
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
