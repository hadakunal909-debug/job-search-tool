"""
test_liveness.py — the fixture set for scraper.liveness.classify.

No external test deps, no network: run it directly
    python test_liveness.py
or via pytest if you have it (functions are named test_*).

The asymmetry is the whole point. Marking a dead posting live costs a wasted click. Marking a
LIVE posting closed removes a job the user could have applied to and nobody ever finds out. So
the bot-wall cases below are the ones that matter most: every one of them is a page that returned
successfully, contains no posting, and would have been read as "gone" by the classifier this
replaced.
"""
import scraper.liveness as lv

# --- real interstitials, trimmed. All of these are HTTP 200 or 403 with a full-size body. ---
CLOUDFLARE = """<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>
<div class="cf-wrapper"><h1>Checking your browser before accessing careers.example.com</h1>
<p>Please enable JavaScript and cookies to continue.</p></div>
""" + ("<!-- padding -->" * 200) + "</body></html>"

TURNSTILE = ("<html><body><h2>careers.example.com</h2><p>Verifying you are human. This may take a "
             "few seconds.</p><p>performing security verification</p>" + ("x" * 900) + "</body></html>")

INCAPSULA = ("<html><body>Request unsuccessful. Incapsula incident ID: 0-1234567890"
             + ("y" * 800) + "</body></html>")

# --- genuinely dead postings ---
GONE_PAGE = ("<html><body><h1>Software Engineer</h1><p>This job is no longer available. "
             "Browse our other openings.</p>" + ("z" * 900) + "</body></html>")

FILLED_PAGE = ("<html><body><p>The position has been filled. Thank you for your interest.</p>"
               + ("z" * 900) + "</body></html>")

# --- a live posting ---
LIVE_PAGE = ("<html><body><h1>Senior Data Engineer</h1><p>We are looking for an engineer to own "
             "our ingestion pipelines. Requirements: 5 years SQL, Python, Airflow. Apply below.</p>"
             "<form><button>Apply now</button></form>" + ("w" * 2000) + "</body></html>")

EMPTY_SHELL = "<html><head></head><body><div id=\"root\"></div></body></html>"

JOB_URL = "https://boards.example.com/careers/job/1098234"
LISTING_URL = "https://boards.example.com/careers/search"
UUID_URL = "https://jobs.example.com/postings/3f2504e0-4f89-11d3-9a0c-0305e82c3301"


# ------------------------------------------------------------------ the guards
def test_a_cloudflare_interstitial_is_never_gone():
    """The single most important case. HTTP 200, a full-size body, no posting -- the old rule
    (status 200 and body < 500) missed it, and any rule that keyed on status alone would call
    every Cloudflare-fronted host dead at once."""
    for status in ("200", "403", "503"):
        v, why = lv.classify(status, CLOUDFLARE, JOB_URL, JOB_URL)
        assert v == "blocked", (status, v, why)
        assert "interstitial" in why, why


def test_other_vendors_walls_are_caught_too():
    for body in (TURNSTILE, INCAPSULA):
        v, _why = lv.classify("200", body, JOB_URL, JOB_URL)
        assert v == "blocked", v


def test_a_bot_wall_beats_even_a_404():
    """Guards run first on purpose. A wall served under a 404 is still a wall, and concluding
    'gone' from it would close every job on that host."""
    v, why = lv.classify("404", CLOUDFLARE, JOB_URL, JOB_URL)
    assert v == "blocked", (v, why)


def test_rate_limits_and_unavailable_are_blocked_not_gone():
    for status in ("429", "503", "401", "403", "405", "451"):
        v, _why = lv.classify(status, LIVE_PAGE, JOB_URL, JOB_URL)
        assert v == "blocked", (status, v)


def test_server_errors_are_transient_and_say_nothing():
    for status in ("500", "502", "504"):
        v, why = lv.classify(status, "", JOB_URL, JOB_URL)
        assert v == "transient", (status, v)
        assert "says nothing" in why


def test_a_connection_error_is_blocked_not_gone():
    v, _why = lv.classify("ERR:ConnectionError", "", JOB_URL, JOB_URL)
    assert v == "blocked", v


# ------------------------------------------------------------------ hard evidence
def test_404_and_410_are_the_only_hard_dead_codes():
    for status in ("404", "410"):
        v, why = lv.classify(status, "", JOB_URL, JOB_URL)
        assert v == "gone", (status, v)
        assert why == "HTTP " + status
    # and nothing else is
    for status in ("200", "301", "418", "429", "500"):
        v, _why = lv.classify(status, LIVE_PAGE, JOB_URL, JOB_URL)
        assert v != "gone", (status, v)


def test_a_page_that_says_it_is_closed_is_gone():
    for body in (GONE_PAGE, FILLED_PAGE):
        v, why = lv.classify("200", body, JOB_URL, JOB_URL)
        assert v == "gone", (v, why)
        assert "page says" in why


def test_a_gone_phrase_inside_a_non_2xx_body_is_not_trusted():
    """A phrase found in an error template is the template's text, not the posting's."""
    v, _why = lv.classify("500", GONE_PAGE, JOB_URL, JOB_URL)
    assert v == "transient", v


def test_localised_dead_pages_are_caught():
    for phrase in ("offre expirée", "esta oferta ya no está disponible"):
        body = "<html><body><p>%s</p>%s</body></html>" % (phrase, "q" * 900)
        v, _why = lv.classify("200", body, JOB_URL, JOB_URL)
        assert v == "gone", (phrase, v)


# ------------------------------------------------------------------ redirects
def test_a_redirect_that_drops_the_job_id_is_uncertain_not_gone():
    v, why = lv.classify("200", LIVE_PAGE, JOB_URL, LISTING_URL)
    assert v == "uncertain", (v, why)
    assert "listing page" in why


def test_the_same_holds_for_a_uuid_job_id():
    v, _why = lv.classify("200", LIVE_PAGE, UUID_URL, "https://jobs.example.com/postings")
    assert v == "uncertain", v


def test_a_redirect_that_keeps_the_job_id_is_fine():
    moved = "https://boards.example.com/en-us/careers/job/1098234"
    v, _why = lv.classify("200", LIVE_PAGE, JOB_URL, moved)
    assert v not in ("gone", "uncertain"), v


def test_a_url_with_no_job_id_cannot_lose_one():
    """Otherwise every redirect from a slug-based board would read as uncertain."""
    slug = "https://jobs.lever.co/acme/senior-data-engineer"
    assert not lv.lost_the_job_id(slug, "https://jobs.lever.co/acme")
    v, _why = lv.classify("200", LIVE_PAGE, slug, "https://jobs.lever.co/acme")
    assert v != "uncertain", v


def test_a_short_number_is_not_a_job_id():
    """4 digits is a year or a page number. Treating it as an id would make ?page=2023 -> ?page=1
    look like a lost posting."""
    assert lv.job_ids("https://x.com/jobs?page=2024") == set()
    assert lv.job_ids("https://x.com/jobs/98234") == {"98234"}


# ------------------------------------------------------------------ the empty-body rule
def test_an_empty_body_on_a_200_is_still_gone():
    """The one rule kept from the old classifier -- but now only reachable when no wall matched."""
    v, why = lv.classify("200", "", JOB_URL, JOB_URL)
    assert v == "gone", (v, why)
    assert "byte body" in why


def test_a_js_shell_counts_as_empty():
    v, _why = lv.classify("200", EMPTY_SHELL, JOB_URL, JOB_URL)
    assert v == "gone", v


def test_a_live_posting_is_never_gone_or_blocked():
    v, _why = lv.classify("200", LIVE_PAGE, JOB_URL, JOB_URL)
    assert v == "unknown", v            # reached it, just could not extract -- a parser problem
    assert v not in lv.CLOSES_THE_POSTING


# ------------------------------------------------------------------ contract
def test_only_gone_may_close_a_posting():
    assert lv.CLOSES_THE_POSTING == frozenset(("gone",))


def test_every_verdict_has_a_note():
    seen = set()
    cases = [("404", ""), ("403", ""), ("500", ""), ("200", LIVE_PAGE),
             ("200", LIVE_PAGE), ("", "")]
    for status, body in cases:
        seen.add(lv.classify(status, body, JOB_URL, JOB_URL)[0])
    seen.add(lv.classify("200", LIVE_PAGE, JOB_URL, LISTING_URL)[0])
    seen.add("readable")                # produced by the caller, not by classify
    for v in seen:
        assert lv.VERDICT_NOTES.get(v), v
    assert {"gone", "blocked", "transient", "uncertain", "unknown", "readable"} <= set(
        lv.VERDICT_NOTES), sorted(lv.VERDICT_NOTES)


def test_classify_never_raises_on_junk():
    for status in (None, "", 404, "weird", b"404"):
        for body in (None, "", "x" * 60000):
            for a, b in ((None, None), ("", JOB_URL), (JOB_URL, None)):
                v, why = lv.classify(status, body, a, b)
                assert isinstance(v, str) and isinstance(why, str)


def test_the_wall_scan_is_bounded():
    """bot_walled must not scan a multi-megabyte page. The phrase is always at the top; a wall
    hiding at byte 500k is not a wall we need to catch, and every probe pays for this."""
    body = ("k" * 30000) + "just a moment"
    assert lv.bot_walled(body) == ""
    assert lv.bot_walled("just a moment" + "k" * 30000) == "just a moment"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d liveness checks passed." % len(fns))
