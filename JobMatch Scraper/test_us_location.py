"""
test_us_location.py — the fixture set for scraper.is_us_location and adopt's foreign veto.

No external test deps, no network: run it directly
    python test_us_location.py
or via pytest if you have it (functions are named test_*).

This function had no suite at all until 2026-09-02, which is how two bugs lived in it. It gates
every row that enters the corpus (scraper.main), both US-only filters in web.py, and — through
_NON_US_RE — adopt_everify_boards' impostor veto. The asymmetry is the same one test_liveness
names: a location wrongly read as foreign silently deletes a job the user could have applied to,
and nobody ever finds out. A location wrongly read as US shows one row from Bengaluru.

THE HARD CASES ARE ALL THE SAME SHAPE: a two-letter code that means one thing in the US and
another abroad. Measured over 8,958 real location strings, the foreign-name veto and a valid US
state code collide on exactly ten, and five of those are genuinely foreign — so no positional
rule ("the state code comes after the city") can separate them. Both directions are frozen here.
"""
import scraper
from scraper.adopt_everify_boards import _names_non_us


def _check(cases):
    bad = ["%s -> %s (wanted %s)" % (loc, scraper.is_us_location(loc), want)
           for loc, want in cases if scraper.is_us_location(loc) is not want]
    assert not bad, "\n  ".join([""] + bad)


def test_plain_us_forms_still_pass():
    """The baseline. If any of these break, the rest of the file is noise."""
    _check([("Boston, MA", True), ("San Francisco, CA", True), ("Remote, US", True),
            ("United States", True), ("Austin, Texas", True), ("Remote", True),
            ("", True), ("3 Locations", True)])


def test_plain_foreign_forms_still_drop():
    _check([("Bengaluru, Karnataka, India", False), ("Toronto, Ontario, Canada", False),
            ("London, UK", False), ("Paris, France", False), ("Dublin, Ireland", False),
            ("Chennai, TN, India", False), ("Hyderabad, Telangana", False),
            ("Coimbatore", False), ("Amsterdam", False), ("London", False)])


def test_us_namesake_city_with_a_state_code_is_us():
    """BUG 1. The veto runs before the state check, so a US city sharing a foreign city's name
    was dropped. Every one of these is a real US place that appears in our own data or would."""
    _check([("Lima, OH", True), ("LONDON, OH", True), ("LONDON, OH; GROVEPORT, OH", True),
            ("AMSTERDAM, NY", True), ("Dublin, OH", True), ("Paris, TX", True),
            ("Warsaw, IN", True), ("Vancouver, WA", True), ("Melbourne, FL", True),
            ("Berlin, CT", True), ("Panama City, FL", True)])


def test_the_namesake_exemption_does_not_leak():
    """...and the five real strings that prove position cannot decide it. In every one the
    two-letter code FOLLOWS the foreign name, exactly as it does in "Lima, OH" — but here IN is
    India, OR is Odisha and DE is Germany, not Indiana, Oregon and Delaware."""
    _check([("Ahmedabad, Gujarat, IN", False), ("Indore, IN", False),
            ("Anywhere in Tamilnadu, Tamil Nadu, IN", False), ("Bhubaneswar, OR", False),
            ("Germany - Remote, DE", False),
            # An exempt name is not enough on its own: something else in the string is foreign,
            # or there is no US state code to say which namesake this is.
            ("Vancouver, BC, Canada", False), ("Vancouver, BC", False),
            ("Mexico City, MX", False), ("London, England", False), ("Paris, Île-de-France", False)])


def test_bare_us_token():
    """BUG 2. "united states" and "usa" were accepted and a bare "US" was not, so a board that
    writes the country the short way lost every row. All four are real strings from the corpus."""
    _check([("US", True), ("Home Based, US", True), ("Quincy MA US", True),
            ("Las Vegas NV US", True), ("Chantilly, US, 20151", True),
            ("Rancho Dominguez, US, 90221", True)])
    # \bus\b must not fire inside a word — these have no other US signal, so a leak shows up here.
    _check([("Aarhus", False), ("Belarus", False), ("Vilnius, Lithuania", False)])


def test_naming_the_country_outranks_the_veto():
    """"Melbourne, FL, USA" says USA and was still dropped, because the veto ran first."""
    _check([("Melbourne, FL, USA", True), ("United States and Canada", True),
            ("Canada - Remote; United States - Remote", True)])


def test_adopt_foreign_veto_agrees():
    """_names_non_us is a VETO and deliberately not is_us_location inverted — it must answer
    False for anything it cannot place, not just for things abroad. But a US namesake is not
    "somewhere abroad", and a board whose every posting sits in Lima OH must not read as 100%
    foreign and lose a real US employer."""
    for loc, want in [("Lima, OH", False), ("London, OH", False), ("Boston, MA", False),
                      ("Tiernan Hall", False), ("UNLV1-Main Campus, Las Vegas", False),
                      ("", False),
                      ("Chennai, TN, India", True), ("Bhubaneswar, OR", True),
                      ("Dublin, Ireland", True), ("Vancouver, BC, Canada", True)]:
        assert _names_non_us(loc) is want, "%s -> %s (wanted %s)" % (loc, _names_non_us(loc), want)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d US-location checks passed." % len(fns))
