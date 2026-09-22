"""Category decisions remain identical on cards, browser filters, and digests.

All rows are synthetic; no live account, database, or posting is read or written.
``fixture_jobs`` and ``fixture_rows`` are reusable for an isolated browser preview.
"""
import collections
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["EV_OFF"] = "1"
os.environ.setdefault("APP_SECRET", "category-test-only")

import core
import job_categories
import web
from bs4 import BeautifulSoup
from feed_parity import js_function, run_js
from jinja2 import ChoiceLoader, DictLoader
from test_job_categories import CONSTRUCTION_DUTIES, IT_DUTIES


def fixture_jobs():
    specs = [
        ("IT Project Manager", CONSTRUCTION_DUTIES, "Software & Internet", "construction"),
        ("Construction Project Manager", IT_DUTIES, "Engineering, Construction & Real Estate", "it"),
        ("IT Project Manager", "", "Engineering, Construction & Real Estate", "it"),
        ("Project Manager", "", "Engineering, Construction & Real Estate", "construction"),
        ("Project Manager", "", "Unsorted", "other"),
        ("Data Analyst", "Responsibilities: Build data pipelines and statistical models. "
         "Own data visualization and business intelligence.", "Software & Internet", "data"),
    ]
    return [dict(title=title, jd=jd, company="Category Fixture %d" % i,
                 company_type=sector, url="https://example.test/category/%d" % i,
                 location="Boston, MA", is_active=True, expected_category=category)
            for i, (title, jd, sector, category) in enumerate(specs)]


def fixture_rows(jobs=None):
    jobs = fixture_jobs() if jobs is None else jobs
    facts = dict(strength="", strength_n=0, visa=(), agency=False, cap_exempt=False,
                 everify_named=False, logo="", logo_ar=1, logo_mono=False, initials="CF")
    with ExitStack() as stack:
        stack.enter_context(patch.object(web, "company_facts", return_value=facts))
        stack.enter_context(patch.object(web, "_repost_count", return_value=0))
        stack.enter_context(patch.object(web, "_host_jd_blocked", return_value=False))
        stack.enter_context(patch.object(web, "_row_pending", return_value=False))
        stack.enter_context(patch.object(web, "_jd_fields", return_value=(None, "", "", "")))
        stack.enter_context(patch.object(web, "_visa_source_present", return_value=True))
        return [web._build_row(job, 80) for job in jobs]


def card_html(rows):
    source = (ROOT / "static" / "app.js").read_text(encoding="utf8")
    names = ("H", "esc", "parseRowDate", "hasClock", "daysAgo", "scoreRing", "scoreCell",
             "companyLink", "companyMark", "factCell", "plainReason", "matchLabel", "cardHTML")
    preamble = """
const HAS_RESUME = true, COMPANY = '', SEP = '<span> / </span>';
const document = {createElement: function () {return {textContent: '', get innerHTML() {
  return String(this.textContent).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}};}};
"""
    driver = preamble + "\n".join(js_function(source, n) for n in names)
    driver += "\nprocess.stdout.write(JSON.stringify(" + json.dumps(rows) + ".map(cardHTML)));"
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "category-cards.cjs"
        path.write_text(driver, encoding="utf8")
        result = subprocess.run(["node", str(path)], capture_output=True, text=True, encoding="utf8", check=True)
    return json.loads(result.stdout)


def render_job(row, status):
    # Render the real detail template with only the unrelated outer layout replaced.
    env = web.app.jinja_env.overlay(loader=ChoiceLoader([
        DictLoader({"base.html": "{% block content %}{% endblock %}"}), web.app.jinja_loader]))
    detail = dict(row, jd_read_status=status)
    asks = collections.defaultdict(lambda: None, verdict="posting", exp_evidence=[], edu_req="", edu_pref="")
    with web.app.test_request_context("/job"):
        return env.get_template("job.html").render(
            row=detail, asks=asks, csrf_token=lambda: "fixture", gone=False,
            route="none", has_resume=False, have=[], missing=[], has_jd=True,
            had_text=True, jd_html="<p>Readable description fixture.</p>", jd_jumps=[],
            about=collections.defaultdict(lambda: None, n_open=0),
            match_breakdown=[], chip_label="No sponsorship information", live_score=None)


def main():
    jobs = fixture_jobs()
    rows = fixture_rows(jobs)
    digests = [core.digest_row(j, 80) for j in jobs]
    for job, row, digest in zip(jobs, rows, digests):
        assert row["category"] == job["expected_category"], row
        assert {k: row[k] for k in job_categories.CATEGORY_FIELDS} == {
            k: digest[k] for k in job_categories.CATEGORY_FIELDS}, (row, digest)

    # A light feed row must preserve its already-read JD verdict even when its title
    # and employer point elsewhere; the request path must not fetch a description.
    stored = {**jobs[0], **job_categories.classify_job(jobs[0]["title"], jobs[0]["jd"])}
    stored.pop("jd")
    assert fixture_rows([stored])[0]["category"] == "construction"
    assert core.digest_row(stored, 80)["category"] == "construction"

    cases = [(key, {"category": key, "date": "any", "min": "0"})
             for key in ("any", *job_categories.CATEGORY_LABELS)]
    cases.append(("it-pm", {"category": "it", "roles": "pm", "date": "any", "min": "0"}))
    with tempfile.TemporaryDirectory() as scratch:
        browser = run_js([(r, "") for r in rows], cases, {name: "" for name, _ in cases}, scratch)
    for name, params in cases:
        prefs = core.normalize_prefs(dict(params, hideagency=False, verifiedonly=False))
        assert web._prefs_as_params(prefs)["category"] == params["category"]
        feed = {r["url"] for r, _ in web._filter_rows(rows, {}, params)}
        mail = {r["url"] for r in digests if core.prefs_match(r, prefs)}
        assert feed == mail == set(browser[name]["urls"]), (name, feed, mail, browser[name])
        want = {r["url"] for r in rows if params["category"] == "any" or r["category"] == params["category"]}
        if name != "it-pm":
            assert feed == want, (name, feed, want)
    assert core.normalize_prefs({"category": "invalid"})["category"] == "any"

    for row, html in zip(rows, card_html(rows)):
        tag = BeautifulSoup(html, "html.parser").select_one(".ccategory")
        assert tag and row["category_label"] in tag.get_text(), (row, html)
        assert tag["title"] == row["category_tip"]
        assert ("Inferred" in tag.get_text()) == (row["category_source"] in ("title", "company"))
        assert ("Needs review" in tag.get_text()) == (row["category_source"] == "unknown")

    unsafe = dict(rows[0], category_label='<img src=x onerror="bad()">', category_tip='" onclick="bad()')
    unsafe_html = BeautifulSoup(card_html([unsafe])[0], "html.parser").select_one(".ccategory")
    assert unsafe_html.get_text() == unsafe["category_label"] and unsafe_html.find("img") is None
    assert unsafe_html["title"] == unsafe["category_tip"] and "onclick" not in unsafe_html.attrs

    with web.app.test_request_context("/feed"):
        form = web.app.jinja_env.get_template("_filterbar.html").render(
            prefs=core.normalize_prefs({"category": "construction"}),
            category_options=job_categories.CATEGORY_LABELS.items(), role_groups=[], visa_options=[])
    select = BeautifulSoup(form, "html.parser").select_one("select#category")
    assert select and {o["value"] for o in select.select("option")} == {"any", *job_categories.CATEGORY_LABELS}
    assert select.select_one("option[selected]")["value"] == "construction"

    statuses = [dict(status="readable", label="Description captured", chars=12000),
                dict(status="incomplete", label="Description may be incomplete", chars=8000)]
    for row, status in zip((rows[0], rows[3]), statuses):
        page = BeautifulSoup(render_job(row, status), "html.parser")
        assert row["category_label"] in page.select_one(".jobcategory").get_text()
        assert row["category_tip"] in page.select_one(".category-evidence").get_text()
        state = page.select_one("[data-read-status]")
        assert state["data-read-status"] == status["status"] and status["label"] in state.get_text()
    print("Passed 6 category fixtures through server, browser and email; cards, controls and detail evidence verified.")


if __name__ == "__main__":
    main()
