"""Saved roles must survive first paint and a subsequent experience refinement.

Exercises authentic Flask rendering and API filtering over an in-memory corpus, in
both inline and paged modes. No account, corpus, network or database is accessed.
"""
from contextlib import ExitStack
import datetime
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["EV_OFF"] = "1"
os.environ.setdefault("APP_SECRET", "ux-feed-regression-only")

from bs4 import BeautifulSoup
import core
import web


def main():
    today = datetime.date.today().isoformat()
    specs = [("ba", "Business Analyst", 1), ("ba", "Business Analyst", 7),
             ("coordinator", "Project Coordinator", 2), ("swe", "Software Engineer", 1)]
    facts = dict(strength="", strength_n=0, visa=(), agency=False, cap_exempt=False,
                 everify_named=False, logo="", logo_ar=1, logo_mono=False, initials="UX")
    with ExitStack() as stack:
        stack.enter_context(patch.object(web, "company_facts", return_value=facts))
        stack.enter_context(patch.object(web, "_repost_count", return_value=0))
        stack.enter_context(patch.object(web, "_host_jd_blocked", return_value=False))
        stack.enter_context(patch.object(web, "_row_pending", return_value=False))
        stack.enter_context(patch.object(web, "_visa_source_present", return_value=True))
        rows = []
        for i, (_role, title, years) in enumerate(specs):
            job = dict(url="https://example.test/ux/%d" % i, title=title, company="UX Fixture",
                       location="Boston, MA", found_date=today, first_seen=today,
                       is_active=True, exp_max_years=years, sponsor_jd="", sponsor_reason="")
            rows.append(web._build_row(job, 80))

    prefs = core.normalize_prefs(dict(roles="ba,coordinator", date="any", hideagency=False,
                                     min=0, min_scale=core.MIN_SCALE))
    with ExitStack() as stack:
        stack.enter_context(patch.object(web, "ranked_rows", return_value=rows))
        stack.enter_context(patch.object(web, "current_profile", return_value="Fictional analyst resume"))
        stack.enter_context(patch.object(web, "_needs_onboarding", return_value=False))
        stack.enter_context(patch.object(web, "_session_dead", return_value=""))
        stack.enter_context(patch.object(web, "user_statuses", return_value={}))
        stack.enter_context(patch.object(web, "_user_prefs", return_value=prefs))
        stack.enter_context(patch.object(web, "_profile_row", return_value={}))
        stack.enter_context(patch.object(web, "role_counts", return_value={}))
        client = web.app.test_client()
        with client.session_transaction() as session:
            session["user"] = "ux-filter-fictional"

        for threshold, paged in [(2, True), (100, False)]:
            with patch.object(web, "_FEED_INLINE_MAX", threshold):
                response = client.get("/")
                assert response.status_code == 200
                page = BeautifulSoup(response.data, "html.parser")
                roles = page.select_one("#roles")["value"]
                assert set(core.parse_roles_pref(roles)) == {"ba", "coordinator"}, roles
                checked = {el["value"] for el in page.select(".rolepick input:checked")}
                assert checked == {"ba", "coordinator"}, checked
                assert int(page.select_one("#count").text) == 3
                bootstrap = json.loads(page.select_one("#feeddata").text)
                assert len(bootstrap) == (3 if paged else 4)

                # Build the next request from the actual rendered roles, as the browser does.
                narrowed = client.get("/api/feed", query_string=dict(
                    roles=roles, exp="2", min="0", date="any", limit="60")).get_json()
                assert narrowed["total"] == 2, narrowed
                assert {r["url"] for r in narrowed["rows"]} == {
                    "https://example.test/ux/0", "https://example.test/ux/2"}
                assert all(set(r["roles"]) & checked for r in narrowed["rows"])
                print("PASS: %s feed retains selected role scope when experience narrows" %
                      ("paged" if paged else "inline"))

        # A deliberate all-role default must still work; fixing the binding must not
        # force the selected families when the user explicitly chooses none.
        with patch.object(web, "_user_prefs", return_value=dict(prefs, roles="")):
            page = BeautifulSoup(client.get("/").data, "html.parser")
            assert page.select_one("#roles")["value"] == ""
            assert not page.select(".rolepick input:checked")
            assert int(page.select_one("#count").text) == 4
    print("ALL UX FEED CHECKS PASS")


if __name__ == "__main__":
    main()
