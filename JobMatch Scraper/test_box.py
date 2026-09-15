import unittest
from unittest.mock import patch
from scraper.box import BOARD, parse_page, scrape_box
from scraper.infosys import PartialScrapeError
import scraper
from test_infosys import response


HTML = '<p>Displaying <strong>1</strong> to <strong>1</strong> of <strong>2</strong> matching jobs</p><div class="card-job"><a class="js-view-job" href="/en/jobs/123/engineer/">Engineer</a><ul><li><svg class="map-marker"></svg>Redwood City, California, United States</li></ul></div><a rel="next nofollow" href="?page=2#results">Next</a>'


class BoxTests(unittest.TestCase):
    def test_card_location_and_real_next_link(self):
        rows, nxt, total = parse_page(HTML, BOARD)
        self.assertEqual(rows[0]["location"], "Redwood City, California, United States")
        self.assertEqual(nxt, BOARD + "?page=2")
        self.assertEqual(total, 2)

    def test_failed_second_page_keeps_first(self):
        first = response(HTML)
        first.url = BOARD
        with patch.object(scraper, "_safe_get", side_effect=[first, response("unavailable", 503)]):
            with self.assertRaises(PartialScrapeError) as raised:
                scrape_box(BOARD)
        self.assertEqual(len(raised.exception.rows), 1)

    def test_careers_url_uses_listing_adapter(self):
        self.assertEqual(scraper.detect_board("https://careers.box.com/en/")[:2], (BOARD, "box"))


if __name__ == "__main__":
    unittest.main()
