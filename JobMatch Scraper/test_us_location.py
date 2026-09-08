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


def _check_csb(cases):
    """scraper._csb_is_us is a SECOND US test, with its own rules, and until 2026-09-06 it had
    no fixtures here at all -- which is how the bug below survived. It exists because the
    generic filter cannot be used on a SuccessFactors board: CSB writes an ISO country code in
    the same slot a US state code goes, so a German "DE" reads as Delaware and an Indian "IN"
    as Indiana. Both directions matter for the same reason the module docstring gives."""
    bad = ["%s -> %s (wanted %s)" % (loc, scraper._csb_is_us(loc), want)
           for loc, want in cases if scraper._csb_is_us(loc) is not want]
    assert not bad, "\n  ".join([""] + bad)


def test_csb_spelled_out_country_is_us():
    """The bug: CSB tenants do not "always carry an ISO country code", whatever the docstring
    said. jobs.oregontool.com writes "Oregon, IL, United States" -- and the fallback rule reads
    ANY two-letter alpha token as a country code, so IL (the state) was taken as the country,
    "UNITED STATES" matched neither of the two accepted spellings, and the row was dropped as
    foreign. All 13 postings on that board are in IL/MO/AZ/OR; all 13 were dropped on every run
    and the board sat in board_health's SILENT list looking like an employer with no openings."""
    _check_csb([("Oregon, IL, United States", True), ("Kansas City, MO, United States", True),
                ("Prescott, AZ, United States", True), ("Portland, OR, United States", True),
                ("Remote, United States", True), ("Dallas, TX, United States of America", True)])


def test_csb_iso_code_forms_still_work():
    """The forms that always worked. A literal US/USA token, and the bare "City, ST" shape."""
    _check_csb([("Austin, TX, US, 78704", True), ("Lincoln, NE, US", True),
                ("Boston, MA", True), ("Melbourne, FL, USA", True)])


def test_csb_foreign_forms_still_drop():
    """The regression that matters most. Widening the US side must not touch these -- the two
    at the top are the Capgemini case that cost 30% of that board's "US" rows: Casablanca and
    Buenos Aires are byte-for-byte the "City, ST" shape, so they are kept out by the named-city
    veto and by nothing else. Adding "UNITED STATES" must not have given them another door."""
    _check_csb([("Casablanca, MA", False), ("Buenos Aires, AR", False),
                ("Milano, IT, 20139", False), ("Walldorf, DE, 69190", False),
                ("Bangalore, KA, IN, 562149", False), ("Shanghai, SH, China", False),
                ("London, United Kingdom", False), ("Hart bei Graz, Steiermark, AT, 8075", False)])


def test_a_state_code_needs_no_comma():
    """THE 2026-09-08 FIX, and the reason this file's own header understates the stakes.

    _STATE_ABBR_RE required a comma before the code, so every US posting written any other way
    was dropped at ingest -- permanently, never retried, and unrecorded because dump_reject is a
    no-op unless DUMP_REJECTS is set. The 2026-09-06 sweep logged 33,704 rows in the "non-US
    location" bucket and these shapes were inside it. core.parse_location has read all of them
    since long before, which is what makes this a disagreement between two functions rather than
    a hard problem.
    """
    _check([("Boston MA", True), ("Chicago IL", True), ("Austin TX", True),
            ("New York NY", True), ("Seattle WA 98101", True), ("Denver CO 80202", True),
            # State FIRST with a dash. Standard on several tenants, and the shape docs/INDEX.md
            # named as the example loss ("WI - Appleton") without anyone fixing it.
            ("WI - Appleton", True), ("MA - Boston", True), ("TX - Dallas", True),
            ("CA - San Jose", True),
            # A bare code is a location. "MA" is Massachusetts, not an unknown.
            ("MA", True)])


def test_the_trailing_form_is_case_sensitive_on_purpose():
    """The one shape with no separator to lean on, so it gets the strictest rule -- the same one
    core._TRAIL_CODE_RE uses. Case-insensitive it fires on any string ending in a two-letter
    word, and "based in" would resolve to Indiana."""
    assert scraper._us_state_in("Boston MA") == "MA"
    assert scraper._us_state_in("boston ma") == ""
    assert scraper._us_state_in("we are based in") == ""
    # ...while a separator IS structure, so those two forms stay case-insensitive.
    assert scraper._us_state_in("boston, ma") == "MA"
    assert scraper._us_state_in("wi - appleton") == "WI"


def test_an_arrangement_in_the_location_field_is_not_a_foreign_country():
    """A board that writes the work arrangement where the place goes used to lose every row:
    none of these matched a state, a state name, "remote" or the country, so they fell through
    to the final `return False`. core.parse_location already lists them as tokens meaning "not a
    place"; unknown means KEEP, which is this function's own documented contract."""
    _check([("Hybrid", True), ("Onsite", True), ("On-Site", True), ("Anywhere", True),
            ("Various", True), ("Flexible", True), ("Nationwide", True), ("Virtual", True),
            ("Work From Home", True), ("TBD", True), ("N/A", True), ("Unspecified", True)])


def test_whitespace_only_is_blank():
    """"" was kept and "   " was dropped -- the same information, the opposite verdict,
    because `if not loc` is False for a truthy blank string."""
    _check([("", True), ("   ", True), ("\t", True), ("\n  ", True)])


def test_the_looser_state_test_is_still_behind_the_foreign_veto():
    """The whole safety argument for dropping the comma. _NON_US_RE runs FIRST and unchanged, so
    a foreign string whose last two letters happen to be a US state code still drops: "Berlin DE"
    resolves DE (Delaware) and is refused on "berlin". Measured over 33 foreign spellings, the
    only admission is "Ontario CA" -- and Ontario, California is a real city of 175,000 with its
    own international airport, so that one is an answer and not a leak. "Ontario, Canada" drops.
    """
    assert scraper._us_state_in("Berlin DE") == "DE", "the state test does resolve it"
    _check([("Berlin DE", False), ("London UK", False), ("Mumbai MH", False),
            ("Manila PH", False), ("Sao Paulo BR", False), ("Mexico City MX", False),
            ("Tel Aviv IL", False), ("Amsterdam NL", False), ("Krakow PL", False),
            ("Bengaluru, KA", False), ("Toronto, ON", False), ("Vancouver, BC", False),
            ("Ontario, Canada", False)])
    # Deliberate: the US city, not the Canadian province.
    _check([("Ontario CA", True)])


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d US-location checks passed." % len(fns))
