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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  - %s" % fn.__name__)
    print("\nAll %d extraction checks passed." % len(fns))
