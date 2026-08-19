"""
test_reposts.py — thresholds for scraper.reposts.

No external test deps, no network, no database: run it directly
    python test_reposts.py
or via pytest if you have it (functions are named test_*).

The failure mode to guard is over-clustering. Calling two genuinely different openings a repost
tells Kunal a real job is a ghost req and he skips it, which is worse than saying nothing. So the
tests below spend most of their effort on things that must NOT cluster: seniority prefixes, roles
that merely share a common word, one posting seen on many scrape days, and a role a company
legitimately hires for twice a year.
"""
import scraper.reposts as rp


def row(title, company, url, seen):
    return {"title": title, "company": company, "url": url, "first_seen": seen}


# ------------------------------------------------------------------ company normalisation
def test_corporate_suffixes_do_not_split_one_employer():
    keys = {rp.normalize_company(n) for n in
            ("Acme Inc.", "Acme, LLC", "ACME Corporation", "Acme Co", "  acme  ")}
    assert keys == {"acme"}, keys


def test_different_companies_stay_different():
    assert rp.normalize_company("Acme") != rp.normalize_company("Acme Labs")


# ------------------------------------------------------------------ title matching
def test_an_exact_repost_matches():
    assert rp.titles_match("Data Engineer", "data engineer")


def test_word_order_does_not_matter():
    """Two recruiters advertising one job write the title two ways."""
    assert rp.titles_match("Engineer, Data Platform", "Data Platform Engineer")


def test_a_level_suffix_is_a_DIFFERENT_role():
    """Reversed after measuring the live corpus. Stripping seniority made Amazon's "Operations
    Manager" cluster with "Senior Operations Manager" and Walmart's "Principal, Software Engineer"
    swallow Senior / II / III. Those are separate requisitions at separate levels."""
    assert not rp.titles_match("Senior Data Engineer", "Senior Data Engineer II")
    assert not rp.titles_match("Operations Manager", "Senior Operations Manager")
    assert not rp.titles_match("Associate Test Engineer", "Lead Test Engineer")


def test_sharing_only_a_seniority_word_is_not_a_match():
    assert not rp.titles_match("Senior Manager", "Senior Engineer")
    assert not rp.titles_match("Lead Analyst", "Lead Designer")


def test_arrangement_and_country_noise_is_stripped():
    """The only thing the token path exists for: word order and decoration."""
    assert rp.titles_match("Engineer, Data Platform (Remote, USA)", "Data Platform Engineer")
    assert rp.titles_match("Data Engineer - Remote", "Data Engineer")
    assert rp.titles_match("Data Engineer (Contract)", "Data Engineer")


def test_sharing_one_real_word_is_not_enough():
    assert not rp.titles_match("Data Engineer", "Data Scientist")
    assert not rp.titles_match("Project Manager", "Product Manager")


def test_a_qualifier_makes_it_a_different_role():
    """The measurement that killed the Jaccard rule. Each pair below scored >= 0.6 and clustered,
    and every one of them is two different jobs."""
    assert not rp.titles_match("Architectural Project Manager", "Project Manager")
    assert not rp.titles_match("Lead Software Engineer", "Lead Software Engineer (Golang)")
    assert not rp.titles_match("(USA) Distinguished, Software Engineer",
                               "(USA) Principal, Software Engineer")
    assert not rp.titles_match(
        "Data Engineer",
        "Data Engineer for the Enterprise Analytics and Reporting Platform Team")


def test_a_title_that_is_only_seniority_words_matches_nothing():
    """Core would be empty, and an empty core equals every other empty core — so refuse."""
    assert not rp.titles_match("Senior II", "Principal III")
    # An identical string still matches, via the exact-string path before the core is computed.
    assert rp.titles_match("Senior", "Senior")


def test_junk_titles_never_match():
    for a, b in (("", "Data Engineer"), ("Data Engineer", ""), (None, None), ("!!!", "???")):
        assert not rp.titles_match(a, b)


# ------------------------------------------------------------------ clustering
def test_the_same_role_at_three_urls_inside_the_window_is_a_repost():
    rows = [row("Data Engineer", "Acme Inc.", "https://a.com/1", "2026-06-01"),
            row("Data Engineer", "Acme LLC", "https://a.com/2", "2026-07-01"),
            row("Data Engineer - Remote", "Acme", "https://a.com/3", "2026-08-01")]
    out = rp.find_reposts(rows)
    assert len(out) == 1, out
    c = out[0]
    assert c["count"] == 3 and c["span_days"] == 61, c
    assert c["company_key"] == "acme"


def test_one_url_seen_on_many_days_is_not_a_repost():
    """The guard that matters most in practice: a posting that survives ten scrapes is one
    posting. Without _collapse_by_url every long-lived job reads as a serial repost."""
    rows = [row("Data Engineer", "Acme", "https://a.com/1", "2026-08-%02d" % d)
            for d in range(1, 11)]
    assert rp.find_reposts(rows) == []


def test_the_earliest_sighting_of_a_url_is_the_one_kept():
    rows = [row("Data Engineer", "Acme", "https://a.com/1", "2026-08-01"),
            row("Data Engineer", "Acme", "https://a.com/1", "2026-06-01"),   # earlier, seen later
            row("Data Engineer", "Acme", "https://a.com/2", "2026-08-10")]
    out = rp.find_reposts(rows)
    assert out and out[0]["dates"][0] == "2026-06-01", out


def test_a_single_url_is_never_a_cluster():
    rows = [row("Data Engineer", "Acme", "https://a.com/1", "2026-08-01")]
    assert rp.find_reposts(rows) == []


def test_sightings_wider_than_the_window_are_not_a_repost():
    """Twice a year is a company that hires for this role periodically, not a ghost req."""
    rows = [row("Tax Analyst", "Acme", "https://a.com/1", "2026-01-05"),
            row("Tax Analyst", "Acme", "https://a.com/2", "2026-08-05")]
    assert rp.find_reposts(rows) == []
    assert len(rp.find_reposts(rows, window_days=365)) == 1


def test_one_role_at_many_locations_is_inventory_not_reposts():
    """The biggest false positive the live corpus exposed: Walmart's Pharmacy Pre-Grad Intern read
    as 106 reposts when it is one role advertised at 106 stores. core.posting_key already documents
    this exact lesson."""
    rows = [row("Pharmacy Pre-Grad Intern", "Walmart", "https://w.com/%d" % i,
                "2026-08-%02d" % (i + 1))
            for i in range(1, 9)]
    for i, r in enumerate(rows):
        r["location"] = "City%d, WI, United States" % i
    assert rp.find_reposts(rows) == []


def test_the_same_role_reposted_at_ONE_location_is_still_caught():
    rows = [dict(row("Pharmacy Pre-Grad Intern", "Walmart", "https://w.com/%d" % i,
                     "2026-08-%02d" % (i + 1)), location="Brown Deer, WI, United States")
            for i in range(1, 4)]
    out = rp.find_reposts(rows)
    assert len(out) == 1 and out[0]["count"] == 3, out
    assert out[0]["location_key"] == "brown deer wi", out[0]


def test_location_spelling_variants_still_cluster():
    rows = [dict(row("Data Engineer", "Acme", "https://a.com/1", "2026-08-01"),
                 location="Seattle, WA, United States"),
            dict(row("Data Engineer", "Acme", "https://a.com/2", "2026-08-09"),
                 location="Seattle,  WA"),
            dict(row("Data Engineer", "Acme", "https://a.com/3", "2026-08-15"),
                 location="seattle, wa, USA")]
    out = rp.find_reposts(rows)
    assert len(out) == 1 and out[0]["count"] == 3, out


def test_two_companies_do_not_cluster_with_each_other():
    rows = [row("Data Engineer", "Acme", "https://a.com/1", "2026-08-01"),
            row("Data Engineer", "Globex", "https://g.com/1", "2026-08-02")]
    assert rp.find_reposts(rows) == []


def test_distinct_openings_at_one_company_are_not_reposts():
    rows = [row("Data Engineer", "Acme", "https://a.com/1", "2026-08-01"),
            row("Data Scientist", "Acme", "https://a.com/2", "2026-08-02"),
            row("Project Manager", "Acme", "https://a.com/3", "2026-08-03")]
    assert rp.find_reposts(rows) == []


def test_clusters_are_ranked_worst_first():
    rows = ([row("Data Engineer", "Acme", "https://a.com/%d" % i, "2026-08-%02d" % (i + 1))
             for i in range(1, 5)]
            + [row("QA Tester", "Globex", "https://g.com/%d" % i, "2026-08-%02d" % (i + 1))
               for i in range(1, 3)])
    out = rp.find_reposts(rows)
    assert [c["count"] for c in out] == [4, 2], [c["count"] for c in out]


def test_rows_with_no_url_or_no_title_are_ignored_not_crashed_on():
    rows = [row("Data Engineer", "Acme", "", "2026-08-01"),
            row("", "Acme", "https://a.com/2", "2026-08-02"),
            {"company": "Acme"},
            row("Data Engineer", "", "https://a.com/3", "2026-08-03")]
    assert rp.find_reposts(rows) == []


def test_missing_and_malformed_dates_do_not_raise():
    rows = [row("Data Engineer", "Acme", "https://a.com/1", None),
            row("Data Engineer", "Acme", "https://a.com/2", "not a date"),
            row("Data Engineer", "Acme", "https://a.com/3", "2026-08-01 09:30")]
    out = rp.find_reposts(rows)
    assert len(out) == 1 and out[0]["count"] == 3, out
    assert out[0]["dates"] == ["2026-08-01"], out[0]["dates"]


def test_found_date_is_accepted_when_first_seen_is_absent():
    rows = [{"title": "Data Engineer", "company": "Acme", "url": "https://a.com/1",
             "found_date": "2026-07-01"},
            {"title": "Data Engineer", "company": "Acme", "url": "https://a.com/2",
             "found_date": "2026-07-20"}]
    out = rp.find_reposts(rows)
    assert len(out) == 1 and out[0]["span_days"] == 19, out


def test_an_empty_or_absent_corpus_is_fine():
    assert rp.find_reposts([]) == []
    assert rp.find_reposts(None) == []


def test_a_large_corpus_stays_fast_enough_to_run_on_prod():
    """Guards the greedy single-pass clustering. 4,000 rows over 200 companies used to be an
    O(n^2)-per-company comparison; if someone reintroduces that this test is where it shows."""
    import time
    rows = [row("Role %d" % (i % 25), "Company %d" % (i % 200),
                "https://x.com/%d" % i, "2026-08-%02d" % (i % 28 + 1))
            for i in range(4000)]
    t0 = time.time()
    out = rp.find_reposts(rows)
    el = time.time() - t0
    assert el < 5.0, "clustering 4k rows took %.1fs" % el
    assert out, "expected clusters in a corpus built to contain them"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d repost checks passed." % len(fns))
