"""Experience eligibility through JD parsing, feed rows, browser and email. Offline."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["EV_OFF"] = "1"
os.environ.setdefault("APP_SECRET", "experience-test-only")

import core
import web
from feed_parity import run_js


def main():
    fixtures = [
        ("Associate Product Manager", "0 years of experience required", True),
        ("Project Coordinator", "1 year of experience required", True),
        ("Business Analyst", "2 years of experience required", True),
        ("Associate Product Manager", "3 years of experience required", True),
        ("Project Manager II", "3 years of experience required", True),
        ("Project Manager", "3-5 years of experience required", True),
        ("Project Manager", "4 years of experience required", False),
        ("Associate Product Manager", "5 years of experience required", False),
        ("Project Coordinator", "six years of experience required", False),
        ("Business Analyst", "Minimum Qualifications: 7+ years in analytics", False),
        ("Program Manager", "8 years of experience and 2 years of SQL experience", False),
        ("Project Coordinator", "Required: 2 years of experience. Preferred: 7 years of experience.", True),
        ("Senior Product Manager", "2 years of experience required", False),
        ("Associate Director", "2 years of experience required", False),
        ("Project Manager", "We value collaboration and communication.", False),
        ("Associate Product Manager", "We value collaboration and communication.", False),
        ("Program Manager Intern", "6 years of experience required", False),
        ("Program Manager Intern", "No prior experience required", True),
        ("Project Coordinator", "No experience necessary. Preferred: 5 years of experience.", True),
        ("Project Coordinator", "No experience necessary. Required: 5 years of experience.", False),
        ("Project Coordinator", "36 months of relevant experience required", True),
        ("Project Coordinator", "37 months of relevant experience required", False),
        ("Project Coordinator", "47 months of relevant experience required", False),
    ]
    rows, expected = [], set()
    for i, (title, requirement, keep) in enumerate(fixtures):
        text = "Responsibilities\n" + "Coordinate projects and communicate progress to stakeholders. " * 12
        text += "\nQualifications\n" + requirement
        job = dict(url="https://example.test/experience/%d" % i, title=title,
                   company="Example", location="Boston, MA", jd=text, is_active=True,
                   exp_max_years=core.experience_years(core.clean_jd(text)[0]))
        row = web._build_row(job, 80)
        digest = core.digest_row(job, 80)
        prefs = dict(core.DEFAULT_PREFS, exp="entry", hideagency=False)
        assert core.prefs_match(digest, prefs) == keep, (title, requirement, digest)
        assert core.experience_matches(row, "entry") == keep, (title, requirement, row)
        rows.append(row)
        if keep:
            expected.add(row["url"])

    cases = [("entry", {"exp": "entry", "date": "any"}),
             ("entry search", {"exp": "entry", "q": "example", "date": "any"}),
             ("two", {"exp": "2", "date": "any"}),
             ("three", {"exp": "3", "date": "any"}),
             ("any", {"exp": "any", "date": "any"})]
    with tempfile.TemporaryDirectory() as scratch:
        js = run_js([(r, "") for r in rows], cases, {n: "" for n, _ in cases}, scratch)
    for name, params in cases:
        actual = {r["url"] for r, _ in web._filter_rows(rows, {}, params)}
        assert set(js[name]["urls"]) == actual, name
        if name.startswith("entry"):
            assert actual == expected, (name, actual ^ expected)
        if name == "any":
            assert len(actual) == len(rows)
        if name in ("two", "three"):
            cap = int(params["exp"])
            assert all(r["exp_src"] == "stated" and 0 <= r["exp_eff"] <= cap
                       for r in rows if r["url"] in actual)

    for invalid in (None, "", " ", "garbage", True, -1, 3.1, float("nan"), float("inf")):
        assert not core.experience_matches(dict(exp_src="stated", exp_eff=invalid, level="entry"), "entry")
    assert core.normalize_prefs({"exp": "3"})["exp"] == "3"
    with patch.dict(web._jdmeta, {"stale": {"exp_years": 2}}):
        assert web._jd_fields({"url": "stale", "exp_max_years": None})[0] is None
        assert web._jd_fields({"url": "stale"})[0] == 2
    print("Passed %d JD fixtures through feed, browser and email; strict ceilings and search verified." % len(fixtures))


if __name__ == "__main__":
    main()
