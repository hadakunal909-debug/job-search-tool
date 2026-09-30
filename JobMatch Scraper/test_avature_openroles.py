"""Avature OpenRoles routes must use their public listing throughout pagination."""
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import scraper


BOARD = "https://careers.example.com/careers/OpenRoles"


def page(job_id):
    return ('<article class="article article--result"><h3>'
            '<a href="/careers/JobDetail/New-York-United-States-Engineer/%s">'
            'Engineer %s</a></h3><span class="list-item-location">New York, NY</span>'
            '</article>' % (job_id, job_id))


class AvatureOpenRolesTests(unittest.TestCase):
    def test_metadata_prefers_same_host_explicit_openroles(self):
        metadata = ('<meta name="avature.portal.urlPath" content="careers">'
                    '<meta name="avature.portal.lang" content="en_US">')
        links = '<a href="https://other.example.com/careers/OpenRoles">Other</a>'
        self.assertEqual(scraper.avature_from_html(metadata + links, BOARD),
                         ('https://careers.example.com/en_US/careers/SearchJobs', 'avature', ''))
        links += '<a href="/careers/OpenRoles?jobOffset=10">Jobs</a>'
        self.assertEqual(scraper.avature_from_html(metadata + links, BOARD),
                         (BOARD, 'avature', ''))

    def test_openroles_location_keeps_non_us_country(self):
        rows = []
        markup = ('<article class="article article--result"><h3>'
                  '<a href="/careers/JobDetail/London-United-Kingdom-Engineer/1">Engineer</a>'
                  '</h3><div class="article__header__content__text">'
                  '<span class="paragraph_inner-span">United Kingdom - UK London</span>'
                  '<div><span class="paragraph_inner-span">Engineering</span></div>'
                  '</div></article>')
        scraper._avature_cards(markup, BOARD, set(), rows)
        self.assertEqual(rows[0]["location"], "United Kingdom - UK London")
        self.assertFalse(scraper.is_us_location(rows[0]["location"]))

    def test_route_preserves_openroles_and_existing_searchjobs(self):
        self.assertEqual(scraper._avature_base(BOARD + "/?jobOffset=10"), BOARD)
        self.assertEqual(scraper._avature_base(
            "https://example.avature.net/careers/JobDetail/title/123"),
            "https://example.avature.net/careers/SearchJobs")

    def test_entire_listing_pages_on_openroles(self):
        requested = []

        def get(url, **kwargs):
            requested.append(url)
            offset = int(url.split("jobOffset=")[-1])
            return SimpleNamespace(status_code=200, text=page(offset + 1) if offset < 3 else "")

        with patch.object(scraper.SESSION, "get", side_effect=get), patch.object(scraper.time, "sleep"):
            rows = scraper.scrape_avature(BOARD)
        self.assertEqual([r["title"] for r in rows], ["Engineer 1", "Engineer 2", "Engineer 3"])
        self.assertEqual(requested, [BOARD + "/?jobOffset=%d" % n for n in range(4)])


if __name__ == "__main__":
    unittest.main()
