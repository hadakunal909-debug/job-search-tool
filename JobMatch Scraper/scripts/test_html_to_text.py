#!/usr/bin/env python3
"""test_html_to_text.py — guards core's text extraction, which had NO test at all.

Nothing pinned the separator, the html.unescape-before-parse ordering, or the fact that
<script>/<style> text was not removed. That mattered because html_to_text is the front door:
every ATS HTML blob and every page scrape becomes a stored description through it, and what it
throws away nothing downstream can recover.

    python scripts/test_html_to_text.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import core
import jdrender

_HTML = ("<div><h2>Responsibilities</h2>"
         "<ul><li>Own the <b>product</b> roadmap</li><li>Manage budgets</li></ul>"
         "<h2>Minimum Qualifications</h2>"
         "<ul><li>5+ years in program management</li></ul>"
         "<p>We are an equal opportunity employer.</p></div>")


def _alnum(s):
    """The projection scripts/test_jdrender.py uses to assert no text was dropped."""
    return "".join(c.lower() for c in (s or "") if c.isalnum())


def test_entities_are_unescaped_before_the_parse():
    """Greenhouse's `content` field is HTML-ESCAPED HTML. Unescaping after the parse would
    leave the markup in the text; not unescaping at all would leave &lt;p&gt;."""
    out = core.html_to_text("&lt;p&gt;Design APIs&lt;/p&gt;&lt;ul&gt;&lt;li&gt;ship&lt;/li&gt;&lt;/ul&gt;")
    assert "&lt;" not in out and "<p>" not in out and "<" not in out, out
    assert "Design APIs" in out and "ship" in out, out


def test_native_greenhouse_reads_full_description_without_application_questions():
    from unittest.mock import patch
    from scraper import score_jobs as sj
    text = "<p>Build and operate payment services with Python and SQL.</p>" * 200
    for host in ("boards.greenhouse.io", "job-boards.greenhouse.io",
                 "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"):
        url = "https://%s/acme/jobs/123?gh_src=example" % host
        response = {"id": 123, "absolute_url": url, "content": text,
                    "first_published": "2026-09-01T12:00:00Z", "updated_at": "2026-09-21T12:00:00Z",
                    "questions": [{"label": "Do you need visa sponsorship?"}],
                    "demographic_questions": {"description": "Do not copy application questions"}}
        with patch.object(sj.scraper, "_get_json", return_value=response) as fetch, \
             patch.object(sj, "microdata_jd", side_effect=AssertionError("page fallback called")), \
             patch.object(core, "fetch_jd", side_effect=AssertionError("page fallback called")):
            got_url, description, date = sj.detail_jd(url)
        assert got_url == url and description == core.html_to_text(text)
        assert len(description) > 8000 and "sponsorship" not in description
        assert date == "2026-09-01"
        assert fetch.call_args.args[0] == "https://boards-api.greenhouse.io/v1/boards/acme/jobs/123"


def test_native_greenhouse_rejects_wrong_identity_without_page_fallback():
    from unittest.mock import patch
    from scraper import score_jobs as sj
    url = "https://job-boards.greenhouse.io/acme/jobs/123"
    for response in ({"id": 999, "content": _HTML},
                     {"id": 123, "absolute_url": "https://job-boards.greenhouse.io/other/jobs/123", "content": _HTML},
                     {"id": 123, "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/999", "content": _HTML},
                     {"content": _HTML}, []):
        with patch.object(sj.scraper, "_get_json", return_value=response), \
             patch.object(sj, "microdata_jd", side_effect=AssertionError("unsafe fallback")), \
             patch.object(core, "fetch_jd", side_effect=AssertionError("unsafe fallback")):
            assert sj.detail_jd(url) == (url, "", "")
    with patch.object(sj.scraper, "_get_json", side_effect=RuntimeError("unavailable")), \
         patch.object(sj, "microdata_jd", side_effect=AssertionError("unsafe fallback")):
        assert sj.detail_jd(url) == (url, "", "")


def test_native_greenhouse_does_not_turn_edit_date_into_posting_date():
    from unittest.mock import patch
    from scraper import score_jobs as sj
    url = "https://job-boards.greenhouse.io/acme/jobs/123"
    with patch.object(sj.scraper, "_get_json", return_value={
            "id": 123, "content": _HTML, "updated_at": "2026-09-21T12:00:00Z"}):
        assert sj.detail_jd(url) == (url, core.html_to_text(_HTML), "")
    assert sj._native_greenhouse_parts("https://job-boards.greenhouse.io.example.test/acme/jobs/123") is None


def test_block_boundaries_become_newlines():
    out = core.html_to_text(_HTML)
    assert "\n" in out, "structure was flattened: %r" % out
    lines = [l for l in out.split("\n") if l.strip()]
    assert "Responsibilities" in lines, lines
    assert "Minimum Qualifications" in lines, lines
    assert sum(1 for l in lines if l.startswith("• ")) == 3, lines


def test_inline_tags_stay_joined_by_a_space():
    """A <b> inside a sentence is not a line break. scripts/test_ibm_jd.py depends on this."""
    assert core.html_to_text("<p>Use <b>Power</b> <i>BI</i> daily</p>") == "Use Power BI daily"


def test_script_and_style_text_is_removed():
    out = core.html_to_text(
        "<div><style>.a{color:red}</style><p>Own the roadmap</p>"
        "<script>var x=1;</script></div>")
    assert "color:red" not in out and "var x" not in out, out
    assert "Own the roadmap" in out


def test_no_text_is_dropped():
    got = _alnum(core.html_to_text(_HTML))
    want = _alnum("Responsibilities Own the product roadmap Manage budgets "
                  "Minimum Qualifications 5+ years in program management "
                  "We are an equal opportunity employer.")
    assert got == want, "text lost or gained:\n  got  %s\n  want %s" % (got, want)


def test_already_plain_text_is_left_alone():
    for s in ("Just a plain sentence.", "Line one\nLine two", ""):
        out = core.html_to_text(s)
        assert _alnum(out) == _alnum(s), (s, out)


def test_plain_and_markdown_keep_their_readable_structure():
    for text in ("Required Qualifications\n- 2 years of experience\n\nPreferred Qualifications\n- 5 years of experience",
                 "## Responsibilities\n\n* Build APIs\n* Use <SQL> and Python"):
        assert core.html_to_text(text) == text
        assert core.html_to_text(core.html_to_text(text)) == text


def test_closing_blocks_separate_following_unwrapped_text():
    text = core.html_to_text("<h2>Required Qualifications</h2>2 years of experience"
                             "<h2>Preferred Qualifications</h2>5 years of experience")
    assert text.splitlines() == ["Required Qualifications", "2 years of experience",
                                "Preferred Qualifications", "5 years of experience"]
    assert core.experience_floors(text) == (2, 5)
    body, _about, jumps = jdrender.render_split(text)
    assert 'id="jdsec-req"' in body and 'id="jdsec-pref"' in body
    assert ("req", "Required Qualifications") in jumps


def test_microdata_keeps_lists_and_headings():
    from scraper import score_jobs as sj
    original = sj.scraper._safe_get
    class Response:
        status_code = 200
        text = '<section itemprop="description">' + _HTML + '<p>' + 'Build reliable services. ' * 12 + '</p></section>'
    sj.scraper._safe_get = lambda *a, **k: Response()
    try:
        text, _date = sj.microdata_jd("https://example.test/job")
    finally:
        sj.scraper._safe_get = original
    assert "\nMinimum Qualifications\n" in text
    assert "\n• Manage budgets" in text


def test_source_formatting_is_not_structure():
    """HTML is indented. A newline inside a text node is the author's wrapping, not a break."""
    out = core.html_to_text("<p>One sentence that\n   wrapped in the source</p>")
    assert out == "One sentence that wrapped in the source", repr(out)


def test_jd_nodes_recovers_the_structure_of_the_source():
    """The round trip that matters: what html_to_text emits, jd_nodes must be able to read.
    This is the whole reason for keeping the newline -- jd_nodes is a newline-driven parser."""
    nodes = jdrender.jd_nodes(core.html_to_text(_HTML))
    kinds = [k for k, _v in nodes]
    assert kinds.count("h") == 2, nodes
    uls = [v for k, v in nodes if k == "ul"]
    assert [len(v) for v in uls] == [2, 1], nodes
    assert uls[0] == ["Own the product roadmap", "Manage budgets"], uls


def test_text_halves_survives_a_metadata_header():
    """The kv node kind raised TypeError, and web._useful_terms swallowed it into an empty
    legal half -- silently switching the boilerplate filter off."""
    jd = core.html_to_text(
        "<p>Clearance Level: None</p><p>Category: Software Engineering</p>"
        "<h2>Responsibilities</h2><ul><li>Own the roadmap</li></ul>"
        "<p>All qualified applicants will receive consideration without regard to race.</p>")
    body, legal = jdrender.text_halves(jd)          # must not raise
    assert "roadmap" in body, body
    assert "consideration" in legal, legal


class _Resp(object):
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_fetch_jd_reads_the_posting_not_the_page_furniture():
    body = "Own the roadmap and manage budgets across teams. " * 10
    page = ("<html><body><nav>Skip to main content Home Jobs</nav>"
            "<main><h2>About the role</h2><p>" + body + "</p></main>"
            "<aside>Related jobs: Analyst, Manager</aside>"
            "<footer>Share this job on LinkedIn. Click the link below.</footer>"
            "</body></html>")
    real = core.requests.get
    core.requests.get = lambda *a, **k: _Resp(page)
    try:
        out = core.fetch_jd("https://example.com/job/1")
    finally:
        core.requests.get = real
    for junk in ("Skip to main content", "Related jobs", "Share this job",
                 "Click the link below"):
        assert junk not in out, "%r survived into the description" % junk
    assert "About the role" in out and "roadmap" in out, out


def test_fetch_jd_falls_back_to_the_whole_document():
    """No <main>, no itemprop: the old behaviour, so a plain page still yields its text."""
    body = "Own the roadmap and manage budgets across teams. " * 10
    real = core.requests.get
    core.requests.get = lambda *a, **k: _Resp("<html><body><p>%s</p></body></html>" % body)
    try:
        out = core.fetch_jd("https://example.com/job/2")
    finally:
        core.requests.get = real
    assert "roadmap" in out and len(out) > core._MAIN_MIN_CHARS, out


def _fetch_page(page, url="https://example.test/jobs/42", limit=None):
    from unittest.mock import patch
    with patch.object(core.requests, "get", return_value=_Resp(page)):
        return core.fetch_jd(url, limit=limit)


def _microdata_page(page, url="https://example.test/jobs/42"):
    from unittest.mock import patch
    from scraper import score_jobs as sj
    response = _Resp(page)
    response.status_code = 200
    with patch.object(sj.scraper, "_safe_get", return_value=response):
        return sj.microdata_jd(url)


def _ld(record):
    import json
    return '<script type="application/ld+json">' + json.dumps(record) + '</script>'


def test_html_entities_preserve_literal_skills_and_symbols():
    # Decoding real HTML before parsing used to swallow SQL as if it were a tag.
    raw = '<p>Use &lt;SQL&gt; and C&amp;C; evaluate a &lt; b and 5 &gt; 2.</p>'
    want = 'Use <SQL> and C&C; evaluate a < b and 5 > 2.'
    assert core.html_to_text(raw) == want
    import html
    assert core.html_to_text(html.escape(raw)) == want


def test_full_page_jd_tail_is_preserved_and_extractable():
    body = '<p>Develop reliable services and own release quality. </p>' * 220
    tail = '<h2>Required Qualifications</h2><p>7 years of experience in project management.</p>'
    text = _fetch_page('<main>' + body + tail + '</main>')
    assert len(text) > 8000
    assert text.endswith('7 years of experience in project management.')
    assert core.experience_floors(text)[0] == 7
    assert len(_fetch_page('<main>' + body + tail + '</main>', limit=100)) == 100


def test_application_form_does_not_erase_the_description():
    body = 'Manage the construction schedule and coordinate subcontractors. ' * 12
    text = _fetch_page('<form><div class="job-description"><h2>Duties</h2><p>' + body
                       + '</p></div><input value="noise"><button>Apply now</button></form>')
    assert body.strip() in text
    assert 'Apply now' not in text


def test_jsonld_matches_requested_job_and_keeps_its_own_date():
    wrong = {'@type': 'JobPosting', 'url': '/jobs/7', 'datePosted': '2026-09-01',
             'description': 'Construction superintendent coordinates concrete works. ' * 15}
    right = {'@type': ['Thing', 'https://schema.org/JobPosting'], 'url': '/jobs/42/',
             'datePosted': '2026-09-20', 'description': '<h2>Responsibilities</h2><p>'
             + 'Deliver software releases and manage cloud migrations. ' * 180 + '</p>'}
    page = ('<div itemscope itemtype="https://schema.org/Organization">'
            '<div itemprop="description">' + 'Company construction history. ' * 20 + '</div></div>'
            + _ld({'@graph': [{'@type': 'ItemList', 'itemListElement':
                    [{'@type': 'ListItem', 'item': wrong}, {'@type': 'ListItem', 'item': right}]}]}))
    text, date = _microdata_page(page, 'https://example.test/jobs/42?utm_source=test')
    assert 'cloud migrations' in text and len(text) > 8000
    assert 'concrete works' not in text and 'Company construction' not in text
    assert date == '2026-09-20', date
    assert _fetch_page(page) == text


def test_ambiguous_related_jobs_are_not_a_description_or_date():
    page = _ld([{'@type': 'JobPosting', 'url': '/jobs/' + str(i),
                 'datePosted': '2026-09-01', 'description': 'Unrelated posting. ' * 40}
                for i in (7, 8)]) + '<main>' + 'Unrelated posting. ' * 40 + '</main>'
    assert _microdata_page(page) == ('', '')
    assert _fetch_page(page) == ''


def test_query_job_identifier_is_not_discarded_as_tracking():
    page = _ld([{'@type': 'JobPosting', 'url': '/job?job=' + str(i),
                 'description': ('Correct job. ' if i == 42 else 'Unrelated job. ') * 40}
                for i in (7, 42)])
    text, _ = _microdata_page(page, 'https://example.test/job?utm_source=test&job=42')
    assert 'Correct job' in text and 'Unrelated job' not in text


def test_single_anonymous_jsonld_and_nested_date_remain_supported():
    record = {'@type': 'JobPosting', 'datePosted': '2026-09-20',
              'description': 'Manage subcontractors and construction schedules. ' * 20}
    page = _ld({'@type': 'WebPage', 'mainEntity': record})
    text, date = _microdata_page(page)
    assert 'subcontractors' in text and date == '2026-09-20'
    from scraper import score_jobs as sj
    assert sj.page_posted_date(core.BeautifulSoup(page, 'lxml')) == '2026-09-20'


def test_microdata_scopes_match_job_identity():
    page = ''
    for i in (7, 42):
        body = ('Correct job. ' if i == 42 else 'Unrelated job. ') * 40
        page += ('<article itemscope itemtype="https://schema.org/JobPosting" '
                 'itemid="/jobs/%s"><div itemprop="description">%s</div>'
                 '<meta itemprop="datePosted" content="2026-09-20"></article>' % (i, body))
    text, date = _microdata_page(page)
    assert 'Correct job' in text and 'Unrelated job' not in text
    assert date == '2026-09-20'
    assert _fetch_page(page) == text
    assert _microdata_page(page, 'https://example.test/jobs/999') == ('', '')
    assert _fetch_page(page, 'https://example.test/jobs/999') == ''


def test_jd_read_status_reports_limits_without_claiming_completeness():
    assert core.jd_read_status('')['status'] == 'missing'
    assert core.jd_read_status('Loading...')['status'] == 'unusable'
    text = ('Deliver software and coordinate cloud release schedules. ' * 300)
    assert core.jd_read_status(text[:8000])['status'] == 'suspected_truncated'
    assert core.jd_read_status(text[:12000])['status'] == 'suspected_truncated'
    assert core.jd_read_status(text)['status'] == 'readable'


def test_repair_requires_retaining_all_existing_job_text():
    from scripts.repair_clipped_jds import replacement_reason
    original = ('Manage construction programs and coordinate contractors. ' * 200)[:8000]
    assert replacement_reason(original, original + ' Manage the project budget and quality controls.') == ''
    assert replacement_reason(original, original) == 'source_unusable_or_still_clipped'
    assert replacement_reason(original, 'Unrelated software job description. ' * 400) == 'source_changed_requires_review'
    assert replacement_reason(original, '') == 'source_unavailable'
    assert replacement_reason(original, original[:500]) == 'not_longer'


def test_repair_rederives_requirements_from_recovered_tail():
    from scripts.repair_clipped_jds import repair_fields
    jd = ('Deliver software releases and coordinate cloud migrations. ' * 170
          + '\nRequired Qualifications\n7 years of experience in project management.')
    fields = repair_fields({'url': 'https://example.test/job', 'location': 'Boston, MA'}, jd, {})
    assert fields['exp_max_years'] == 7
    assert fields['facts_fp']
    assert fields['jd_terms']
    assert fields['loc_state'] == 'MA'


def test_repair_dry_run_is_bounded_and_does_not_write_database():
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch
    from scripts import repair_clipped_jds as repair
    old = ('Manage construction programs and coordinate contractors. ' * 200)[:8000]
    new = old + ' Manage the project budget and quality controls.'
    with tempfile.TemporaryDirectory() as folder:
        args = SimpleNamespace(state=str(Path(folder) / 'state.json'), report=str(Path(folder) / 'report.json'),
                               apply=False, url=[], host=[], retry_after_hours=24, limit=1,
                               batch_size=10, max_seconds=60, cache_only=True)
        with patch.object(repair, 'candidate_urls', return_value=['https://example.test/1', 'https://example.test/2']), \
             patch.object(repair, '_selected_cache', return_value={'https://example.test/1': new}), \
             patch.object(repair.db, 'load_jobs_by_urls', return_value=[{'url': 'https://example.test/1', 'jd': old}]), \
             patch.object(repair, 'persist_repairs') as write, \
             patch.object(repair.sj, 'detail_jd') as fetch:
            report = repair.run(args)
            assert report['eligible'] == 2 and report['selected'] == 1
            assert report['counts'] == {'would_recover': 1}
            write.assert_not_called()
            fetch.assert_not_called()


def test_repair_resumes_analysis_after_text_was_already_saved():
    import json
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch
    from scripts import repair_clipped_jds as repair
    url = 'https://example.test/42'
    full = 'Manage construction programs and coordinate contractors. ' * 200
    with tempfile.TemporaryDirectory() as folder:
        args = SimpleNamespace(state=str(Path(folder) / 'state.json'), report=str(Path(folder) / 'report.json'),
                               apply=True, url=[], host=[], retry_after_hours=24, limit=1,
                               batch_size=10, max_seconds=60, cache_only=True)
        Path(args.state).write_text(json.dumps({'apply': {url: {'status': 'pending', 'attempt_at': 0,
                                      'new_fp': repair.db.jd_fingerprint(full)}}}), encoding='utf-8')
        with patch.object(repair, 'candidate_urls', return_value=[]), \
             patch.object(repair, '_selected_cache', return_value={}), \
             patch.object(repair.core, 'load_idf', return_value={}), \
             patch.object(repair.db, 'load_jobs_by_urls', return_value=[{'url': url, 'jd': full}]), \
             patch.object(repair, 'persist_repairs') as write, \
             patch.object(repair.sj, 'detail_jd') as fetch:
            report = repair.run(args)
            assert report['counts'] == {'recovered': 1}
            assert write.call_args.args[1] == {url: full}
            fetch.assert_not_called()
            assert json.loads(Path(args.state).read_text())['apply'][url]['status'] == 'recovered'


def test_scheduled_retry_repairs_caps_without_replacing_better_text():
    from scraper import score_jobs as sj
    url = "https://example.test/jobs/42"
    old = ("Manage construction programs and coordinate contractors. " * 200)[:8000]
    good = old + " Manage the project budget and quality controls."
    assert sj._is_thin_jd(old)
    assert not sj._is_thin_jd(good)
    assert sj._accept_jd(url, good, {url: len(old)}, old)
    assert not sj._accept_jd(url, "Different job content. " * 500, {url: len(old)}, old)
    assert not sj._accept_jd(url, old, {url: len(old)}, old)


def test_long_jd_tail_retains_remote_pay_and_experience_facts():
    body = "Design reliable services and maintain deployment pipelines. " * 850
    assert len(body) > 40000
    text = body + "\nThis is a fully remote position.\nSalary range: $120,000 - $150,000 per year.\nRequired Qualifications\n7 years of experience in project management."
    assert core.parse_location("", text)["remote"] is True
    assert core.parse_salary(text) == {"min": 120000, "max": 150000, "period": "year"}
    assert core.experience_floors(text)[0] == 7


def test_repair_cache_stream_preserves_entries_and_large_unicode_text():
    import gzip
    import json
    import tempfile
    from pathlib import Path
    from unittest.mock import patch
    from scripts import repair_clipped_jds as repair
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "cache.json.gz"
        existing = {"https://example.test/a": "quotes \" braces {} accents é " * 5000,
                    "https://example.test/b": "keep me", "https://example.test/c": "old"}
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(existing, fh, ensure_ascii=False)
        assert dict(repair._cache_entries(path)) == existing
        with patch.object(repair.sj, "JD_CACHE_FILE", str(path)):
            assert repair._selected_cache({"https://example.test/b"}) == {"https://example.test/b": "keep me"}
            repair._save_cache({"https://example.test/c": "repaired", "https://example.test/new": "new"})
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            actual = json.load(fh)
        assert actual == dict(existing, **{"https://example.test/c": "repaired", "https://example.test/new": "new"})


def test_repair_accepts_missing_or_proven_nonposting_incumbents():
    from scripts.repair_clipped_jds import replacement_reason
    good = "Design software services, test code, and maintain cloud infrastructure. " * 12
    dead = "This job is no longer available. Search all careers and create an alert. " * 180
    assert core.clean_jd(dead)[1] == "not-a-posting"
    assert len(good) < len(dead)
    for old in (None, "", " \n\t", dead):
        assert replacement_reason(old, good) == ""
        assert replacement_reason(old, "Loading...") == "source_unusable_or_still_clipped"
    assert replacement_reason(good, "Different readable duties. " * 50) == "source_changed_requires_review"


def test_repair_compares_decoded_entities_without_losing_skill_text():
    import html
    from scripts.repair_clipped_jds import replacement_reason
    old = "Design&#xa;software&#xA0;with&#32;&lt;SQL&gt; and deliver releases. " * 40
    good = html.unescape(old) + " Required: seven years of engineering experience."
    assert len(good) < len(old), "the fixture must remove markup bytes while adding job content"
    assert core.jd_extends(old, good)
    assert replacement_reason(old, good) == ""
    assert not core.jd_extends(old, good.replace("<SQL>", ""))
    assert not core.jd_extends(old, html.unescape(old)), "formatting alone is not recovered content"


def test_repair_explicit_url_handles_missing_and_unusable_descriptions():
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import patch
    from scripts import repair_clipped_jds as repair
    url = "https://example.test/jobs/repair-gap"
    good = "Design software services, test code, and maintain cloud infrastructure. " * 12
    for old in ("", " \n", "This job is no longer available. " * 40):
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(state=str(Path(folder) / "state.json"),
                                   report=str(Path(folder) / "report.json"), apply=False,
                                   url=[url], host=[], retry_after_hours=24, limit=1,
                                   batch_size=1, max_seconds=60, cache_only=True)
            with patch.object(repair, "_selected_cache", return_value={url: good}), \
                 patch.object(repair.db, "load_jobs_by_urls", return_value=[{"url": url, "jd": old}]), \
                 patch.object(repair, "persist_repairs") as write:
                report = repair.run(args)
                assert report["counts"] == {"would_recover": 1}, report
                write.assert_not_called()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  - %s" % fn.__name__)
    print("\nAll %d extraction checks passed." % len(fns))
