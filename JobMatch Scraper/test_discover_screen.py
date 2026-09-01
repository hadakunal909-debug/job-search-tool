"""
test_discover_screen.py — guards the harvest_phrases column in scripts/discover_companies.py.

No network and no database: screen() reaches for the boards table and the sponsor indexes, so
all three lookups are stubbed the way test_jobspy_adapter.py stubs the jobspy module. Without
that, this suite would quietly become a live DB test that passes or fails on connectivity.

What is actually frozen here is one arithmetic invariant and one back-compat guarantee:

  * the per-phrase counts SUM TO postings_seen, which is the only reason the per-role report can
    be trusted rather than merely read; and
  * a CSV written before this column existed (discovered_2wk.csv, discovered_month.csv) still
    parses, as "no phrase data" and never as a crash.

Run it directly
    python test_discover_screen.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))

import discover_companies as dc


def _stub():
    """Cut the three lookups screen() makes, so the suite is genuinely offline."""
    dc.feb._known_sources = lambda: (set(), set())
    # Takes the boards-table names now (build_sources_matcher(extra=())), so the stub must
    # accept them too -- a zero-arg lambda here would TypeError inside screen() and the
    # failure would look like a screen() bug rather than a stale stub.
    dc.ce.build_sources_matcher = lambda extra=(): (lambda name: "")
    dc.scraper.custom_sources = lambda: []


def _rec(company, title, phrase, channel="linkedin"):
    return {"company": company, "title": title, "url": "", "location": "", "posted": "",
            "channel": channel, "phrase": phrase}


def test_fmt_orders_by_count_desc():
    import collections
    cell = dc._fmt_phrases(collections.Counter({"scrum master": 3, "product manager": 12}))
    assert cell == "product manager:12|scrum master:3", cell


def test_parse_roundtrip_survives_a_colon_in_the_phrase():
    import collections
    # ':' is the separator, so a phrase containing one must be squashed rather than corrupt the
    # cell. '|' cannot arrive -- --phrases already split the caller's list on it.
    c = collections.Counter({"a:b": 2, "data analyst": 1})
    assert dc.parse_phrases(dc._fmt_phrases(c)) == {"a b": 2, "data analyst": 1}


def test_parse_tolerates_a_missing_column():
    # THE BACK-COMPAT GUARD. Every CSV written before 2026-08-30 lacks this column entirely, and
    # DictReader hands back None for it. Crashing here would make three prior artifacts unusable.
    assert dc.parse_phrases("") == {}
    assert dc.parse_phrases(None) == {}
    assert dc.parse_phrases("product manager") == {"product manager": 1}   # bare, no count


def test_phrases_aggregate_across_queries_and_sum_to_postings_seen():
    _stub()
    rows = dc.screen([_rec("Acme Corp", "Data Analyst", "data analyst"),
                      _rec("Acme Corp", "Software Engineer", "software engineer"),
                      _rec("Acme Corp", "Warehouse Picker", "software engineer")])
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["harvest_phrases"] == "software engineer:2|data analyst:1", r["harvest_phrases"]
    # The invariant the report rests on. harvest does not dedupe across queries, so a posting
    # returned by two phrases is genuinely two hits and postings_seen already counts it twice.
    assert sum(dc.parse_phrases(r["harvest_phrases"]).values()) == r["postings_seen"]


def test_phrase_count_ignores_the_title_filter_but_pm_titles_kept_does_not():
    # Deliberate split: harvest_phrases answers "which query surfaced this employer" and must
    # count every posting, while pm_titles_kept answers "how many are on target". Gating the
    # former on title_verdict would conflate query quality with filter quality.
    _stub()
    r = dc.screen([_rec("Acme Corp", "Data Analyst", "data analyst"),
                   _rec("Acme Corp", "Warehouse Picker", "data analyst")])[0]
    assert dc.parse_phrases(r["harvest_phrases"]) == {"data analyst": 2}
    assert r["pm_titles_kept"] == 1, r["pm_titles_kept"]


def test_csv_channel_has_no_phrase_and_still_screens():
    _stub()
    r = dc.screen([{"company": "Acme Corp", "title": "", "url": "", "location": "",
                    "posted": "", "channel": "csv", "phrase": ""}])[0]
    assert r["harvest_phrases"] == "", r["harvest_phrases"]
    assert dc.phrase_stats([r]) == []


def test_fieldnames_covers_every_key_screen_emits():
    # write_csv uses DictWriter WITHOUT extrasaction="ignore", so a key screen() emits that
    # FIELDNAMES lacks raises ValueError on the first row -- i.e. after the whole harvest has
    # been paid for. Fail here instead.
    _stub()
    r = dc.screen([_rec("Acme Corp", "Data Analyst", "data analyst")])[0]
    missing = set(r) - set(dc.FIELDNAMES)
    assert not missing, missing


def test_only_counts_employers_exactly_one_phrase_found():
    # net_new double-counts and must never be summed; `only` is the partition that can be.
    rows = [{"harvest_phrases": "data analyst:1|software engineer:1", "in_sources": "no",
             "h1b_filings": "0", "stem_opt": "no", "cap_exempt": "no"},
            {"harvest_phrases": "data analyst:4", "in_sources": "no",
             "h1b_filings": "9", "stem_opt": "yes", "cap_exempt": "no"},
            {"harvest_phrases": "data analyst:1", "in_sources": "yes",
             "h1b_filings": "3", "stem_opt": "no", "cap_exempt": "no"}]
    stats = {d["phrase"]: d for d in dc.phrase_stats(rows)}
    da = stats["data analyst"]
    assert da["employers"] == 3 and da["net_new"] == 2, da
    assert da["only"] == 1, da            # only the solo net-new row
    assert da["h1b"] == 1 and da["stem_opt"] == 1, da   # the in_sources row is excluded
    assert stats["software engineer"]["only"] == 0
    net_new_total = sum(1 for r in rows if r["in_sources"] != "yes")
    assert sum(d["only"] for d in stats.values()) <= net_new_total


def test_prior_artifacts_still_read_as_no_phrase_data():
    """A CSV written BEFORE harvest_phrases existed must still read as no phrase data.

    The property under test is about the COLUMN, not about the filename, and conflating the
    two made this suite fail the moment anyone actually ran the tool: discovered_companies.csv
    is discover_companies.py's DEFAULT --out, so a real harvest overwrites the 'prior artifact'
    with a current one that has the column populated -- and the assertion then fires on a file
    that is not prior at all. Measured 2026-09-01, after a li+indeed sweep wrote 1,933 rows
    over it. CI never saw it because all three names are gitignored and absent there, which is
    exactly the kind of gap that makes a suite pass in CI and fail on the machine that did the
    work. So decide on the header: a file carrying phrase data is skipped as CURRENT, and only
    a file genuinely lacking it is held to the no-phrase-data contract.
    """
    import csv
    here = os.path.dirname(os.path.abspath(__file__))
    seen, skipped = 0, 0
    for name in ("discovered_2wk.csv", "discovered_month.csv", "discovered_companies.csv"):
        path = os.path.join(here, name)
        if not os.path.exists(path):        # gitignored artifacts; absent on a fresh clone
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
            rows = list(reader)
        assert rows, name
        if "harvest_phrases" in cols and any((r.get("harvest_phrases") or "").strip()
                                             for r in rows):
            skipped += 1                    # a CURRENT artifact, not a prior one
            continue
        seen += 1
        assert dc.phrase_stats(rows) == [], name
    print("      (checked %d prior artifact(s), skipped %d current)" % (seen, skipped))


def test_sort_order_is_unchanged():
    # The screen() sort key must keep ranking on sponsor evidence, NOT on the new column.
    _stub()
    rows = dc.screen([_rec("Zeta Inc", "Data Analyst", "data analyst"),
                      _rec("Alpha Inc", "Data Analyst", "data analyst"),
                      _rec("Alpha Inc", "Data Engineer", "data engineer")])
    # No sponsor data for either synthetic name, so the tiebreak falls to pm_titles_kept desc
    # then company name -- Alpha has two, Zeta one.
    assert [r["company"] for r in rows] == ["Alpha Inc", "Zeta Inc"], rows


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d discover-screen checks passed." % len(fns))
