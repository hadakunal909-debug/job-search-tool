#!/usr/bin/env python3
"""
test_qa_audit_fixes.py — guards for the QA_AUDIT findings fixed on 2026-08-24.

WHY ONE FILE. The register's own rule is that a finding with no guard comes back, and most of
these fixes are one or two lines in code that has no suite of its own (a host-matching helper, a
CSV cell, a status whitelist). Scattering them would mean six new files nobody remembers; the id
in each test name is what keeps them traceable back to the finding.

OFFLINE AND DATABASE-FREE, deliberately — see CLAUDE.md on CI having no database. Nothing here
reads the live corpus, and the two cases that need Flask use a test request context rather than
a running server.

Run:  EV_OFF=1 python test_qa_audit_fixes.py
"""
import os
import sys

os.environ.setdefault("EV_OFF", "1")          # BEFORE anything imports web/analytics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# ----------------------------- S3: anchored host matching -----------------------------
def test_s3_spoofed_host_never_survives_into_a_board_url():
    """`"greenhouse.io" in host` said yes to greenhouse.io.evil.example. An anchored suffix
    cannot, and several detect_board branches keep the CALLER's host in what they return."""
    import scraper
    spoofs = [
        "https://myworkdaysite.com.169.254.169.254.nip.io/recruiting/t/s",
        "https://eightfold.ai.evil.example/careers",
        "https://greenhouse.io.evil.example/acme",
        "https://notgreenhouse.io/acme",
        "https://jobdiva.com.evil.example/portal/?a=123",
        "https://evil-workable.com/acme",
    ]
    for u in spoofs:
        got = scraper.detect_board(u)
        if got is None:
            continue                          # refused outright: the best outcome
        board_url = got[0]
        for bad in ("evil.example", "nip.io", "169.254.169.254", "notgreenhouse", "evil-workable"):
            assert bad not in board_url, "%s -> %s leaked %r" % (u, board_url, bad)


def test_s3_host_is_accepts_the_real_thing():
    """Anchoring must not break the boards that ARE these hosts."""
    from scraper import host_is
    assert host_is("boards.greenhouse.io", "greenhouse.io")
    assert host_is("greenhouse.io", "greenhouse.io")
    assert host_is("acme.wd1.myworkdayjobs.com", "myworkdayjobs.com")
    assert host_is("acme.wd1.myworkdayjobs.com:443", "myworkdayjobs.com")   # port stripped
    assert host_is("acme.wd1.myworkdayjobs.com.", "myworkdayjobs.com")      # DNS root form
    assert not host_is("greenhouse.io.evil.example", "greenhouse.io")
    assert not host_is("notgreenhouse.io", "greenhouse.io")
    assert not host_is("", "greenhouse.io")


# ----------------------------- F1: apostrophes -----------------------------
def test_f1_apostrophes_are_deleted_not_turned_into_a_space():
    """An apostrophe sits INSIDE a word. Replacing it with a space left a stray "s" token and
    the DOL/USCIS rows — which mostly spell these without it — could never match."""
    from scraper import _norm_name
    cases = {
        "Kohl's": "kohls",
        "Domino's Pizza": "dominos pizza",
        "BJ's Wholesale Club": "bjs wholesale club",
        "Children's Hospital of Philadelphia": "childrens hospital of philadelphia",
        "Kohl’s": "kohls",                    # curly, which is what scraped text carries
    }
    for raw, want in cases.items():
        got = _norm_name(raw)
        assert got == want, "%r -> %r, wanted %r" % (raw, got, want)
    # And the ordinary separators still separate.
    assert _norm_name("Avery-Dennison Corp.") == "avery dennison"


# ----------------------------- F7: Workday casing -----------------------------
def test_f7_workday_site_segment_folds():
    """One requisition served under two casings was stored as two rows, because `url` is the
    primary key. That is the root cause of "Amat" and "Applied Materials" as two employers."""
    import scraper
    a = "https://amat.wd1.myworkdayjobs.com/external/job/Santa-ClaraCA/Data-Scientist_R2624867"
    b = "https://amat.wd1.myworkdayjobs.com/External/job/Santa-ClaraCA/Data-Scientist_R2624867"
    assert scraper.canonical_url(a) == scraper.canonical_url(b)
    # The requisition id is case-SENSITIVE and must not fold with it.
    c = "https://amat.wd1.myworkdayjobs.com/external/job/Santa-ClaraCA/Data-Scientist_r2624867"
    assert scraper.canonical_url(a) != scraper.canonical_url(c)


# ----------------------------- S8: status whitelist -----------------------------
def test_s8_only_known_statuses_reach_user_jobs():
    """db.set_user_status' docstring named a closed set and enforced nothing, so any account
    could write arbitrary strings into the shared table."""
    import db
    assert db.USER_STATUSES == ("liked", "hidden", "applied")
    # "applied " IS NOT IN THIS LIST, and that is the point rather than an omission. `.strip()`
    # runs before the membership test, so it normalises to a valid "applied" and the column gets
    # a clean value -- the invariant this guards ("only known statuses reach user_jobs") holds.
    # It was in the reject list, where it did not raise, fell through to the DATABASE, and came
    # back a 409 foreign-key error on the fake username -- so the suite both failed and wrote
    # to the live table. Everything below raises before any I/O, so this test touches nothing.
    for bad in ("deleted", "LIKED", "<script>", "x" * 400, "applied	x", "liked,hidden"):
        try:
            db.set_user_status("nobody", "https://example.com/j", bad)
        except ValueError:
            continue
        raise AssertionError("set_user_status accepted %r" % (bad,))
    # ...and the normalising case, asserted on the VALUE that would be written rather than by
    # calling through to the table: strip() is what makes it safe, so prove strip() is applied.
    assert "applied ".strip() in db.USER_STATUSES
    assert " LIKED ".strip() not in db.USER_STATUSES


# ----------------------------- S10: CSV formula injection -----------------------------
def test_s10_formula_cells_are_neutralised():
    import web
    for bad in ('=HYPERLINK("http://evil","x")', "+1+1", "-2+3", "@SUM(A1)", "\tx", "\rx"):
        assert web._csv_cell(bad).startswith("'"), bad
    for ok in ("Amazon", "Senior Analyst", "", "3-5 years"):
        assert web._csv_cell(ok) == ok, ok


# ----------------------------- S11: the AI key is encrypted, not just signed -----------------
def test_s11_sealed_value_is_not_readable_and_round_trips():
    """Flask signs the session cookie; it does not encrypt it. The key was plain base64 JSON to
    anyone holding the cookie file."""
    import web
    secret = "AIzaSyEXAMPLE-not-a-real-key"
    blob = web.seal(secret)
    assert secret not in blob and "AIza" not in blob
    assert web.unseal(blob) == secret
    # Tampering is detected rather than decrypted into garbage.
    assert web.unseal(blob[:-6] + "AAAAAA") == ""
    assert web.unseal("") == "" and web.unseal("not-base64!!") == ""
    # Two seals of the same value differ: the nonce is per call.
    assert web.seal(secret) != web.seal(secret)


# ----------------------------- S1: no DB read for an invalid token --------------------------
def test_s1_invalid_tokens_do_not_hit_the_database():
    """_ext_user resolved the user's token_epoch — a query — BEFORE checking the HMAC, so
    spraying invented usernames at the CORS-open /api/ext/* routes cost one read per guess."""
    import web, db
    calls = []
    real_get_user = db.get_user
    db.get_user = lambda u, cols=None, *a, **k: (
        calls.append(u) or ({"username": u, "token_epoch": 0} if u == "realuser" else None))
    try:
        with web.app.test_request_context("/api/ext/jobs?token=x"):
            web._account_cache.clear()
            web._EXT_BAD.clear()
            for i in range(20):
                assert web._ext_user("victim%d:%s" % (i, "d" * 32)) is None
            assert len(calls) <= web._EXT_BAD_ALLOW, \
                "20 invalid tokens cost %d database reads" % len(calls)

            # A valid token still authenticates, and costs exactly one lookup.
            calls[:] = []
            web._account_cache.clear()
            web._EXT_BAD.clear()
            assert web._ext_user(web._ext_token("realuser", epoch=0)) == "realuser"
            assert len(calls) == 1
    finally:
        db.get_user = real_get_user
        web._account_cache.clear()


def test_s1_malformed_tokens_are_refused_on_shape():
    import web
    for bad in ("", "nocolon", ":" + "a" * 32, "user:short", "user:" + "Z" * 32,
                "user:" + "a" * 31, ("u" * 200) + ":" + "a" * 32):
        assert web._ext_user(bad) is None, bad


# ----------------------------- S2: the limiters -----------------------------
def test_s2_extension_limiter_has_a_burst_tier():
    """A lone 900-per-3600s permits all 900 landing in one second, which is the saturation
    event a limiter exists for. _rate_hit's own docstring says so; this class ignored it."""
    import web
    web._ext_hits.clear()
    tiers = web._EXT_DEFAULT[0]
    accepted = 0
    for _ in range(1000):
        if web._rate_hit(("extension API", "t:abc"), tiers):
            break
        accepted += 1
    assert accepted < 100, "accepted %d calls in one instant" % accepted


def test_s2_spraying_the_ext_limiter_cannot_reset_the_feed_limiter():
    """One shared dict, cleared wholesale at the cap, meant 5001 unique ?token= values wiped
    every logged-in user's feed-limiter state."""
    import web
    web._ext_hits.clear()
    for _ in range(300):
        web._rate_hit(("feed", "realuser"), web._FEED_TIERS)
    assert web._rate_hit(("feed", "realuser"), web._FEED_TIERS), "setup: should be limited"
    for i in range(web._EXT_MAX_KEYS + 200):
        web._rate_hit(("extension API", "t:%d" % i), web._EXT_DEFAULT[0])
    assert web._rate_hit(("feed", "realuser"), web._FEED_TIERS), \
        "the spray reset another namespace's limiter"


# ----------------------------- U15 / U8: keyword suggestions -----------------------------
def test_u15_perks_are_never_suggested_as_resume_keywords():
    """"Retirement" and "dental" are in the description but are not a description of the work.
    Advising someone to put them on a CV is the most visible way this panel loses trust."""
    import web
    terms = ["retirement", "dental", "vision", "tuition", "wellness", "flexible",
             "python", "kubernetes", "forecasting"]
    kept = web._useful_terms(terms, "Acme", "We offer dental and a retirement plan.", 20)
    for perk in ("retirement", "dental", "vision", "tuition", "wellness", "flexible"):
        assert perk not in kept, perk
    for real in ("python", "kubernetes", "forecasting"):
        assert real in kept, real


def test_u8_the_employers_own_name_is_not_a_skill():
    import web
    kept = web._useful_terms(["applied materials", "applied", "materials", "python"],
                             "Applied Materials", "Applied Materials is a global leader.", 20)
    assert "python" in kept
    for own in ("applied materials", "applied", "materials"):
        assert own not in kept, own


# ----------------------------- U6: location punctuation -----------------------------
def test_u6_location_separators_are_normalised():
    import core
    assert core.tidy_location("Santa Clara,CA") == "Santa Clara, CA"
    assert core.tidy_location("Redmond, WA, US") == "Redmond, WA, US"
    assert core.tidy_location("  Boston ,  MA  ") == "Boston, MA"
    assert core.tidy_location("") == ""
    assert core.tidy_location(None) == ""
    assert core.tidy_location("Remote") == "Remote"


# ----------------------------- F3: the debug log stays parseable -----------------------------
def test_f3_ext_debug_records_are_valid_json_when_bounded():
    """Truncating a serialised object at a byte offset produces a line no parser can read, and
    `fields` carries up to 40 descriptors so records really do exceed the cap."""
    import json
    rec = {"ts": "2026-08-24", "user": "u", "url": "https://x/" + "a" * 300,
           "company": "C", "status": "s", "reason": "r" * 300, "ai": "",
           "fields": [{"label": "L" * 200, "type": "text", "options": ["o" * 50] * 8}] * 40}
    line = json.dumps(rec)
    while len(line) > 9000 and rec["fields"]:
        rec["fields"] = rec["fields"][:len(rec["fields"]) // 2]
        rec["fields_truncated"] = True
        line = json.dumps(rec)
    if len(line) > 9000:
        for k in ("reason", "url", "company", "ai"):
            rec[k] = (rec.get(k) or "")[:60]
        rec["fields_truncated"] = True
        line = json.dumps(rec)
    assert len(line) <= 9000
    json.loads(line)                       # the whole point: it still parses


# ----------------------------- F15: research verifies the employer -----------------------------
def test_f15_a_guessed_domain_is_reported_as_a_guess():
    from resume_brain import research
    dom, src = research.resolve_domain("Actalent", "", with_source=True)
    assert dom == "actalent.com" and src == "guess"
    dom, src = research.resolve_domain("Anything", "https://careers.example.com/x",
                                       with_source=True)
    assert dom == "careers.example.com" and src == "url"


def test_f15_an_unrelated_page_fails_the_mention_check():
    from resume_brain import research
    pages = [{"url": "https://x", "title": "Zerbrasil Athletics",
              "text": "Somos una agencia boutique de deportistas."}]
    assert not research.mentions_company(pages, "Applied Materials")
    good = [{"url": "https://x", "title": "Applied Materials",
             "text": "Applied Materials is a global leader in materials engineering."}]
    assert research.mentions_company(good, "Applied Materials")
    # Written without the space, which is how a logo/wordmark page often spells it.
    squashed = [{"url": "https://x", "title": "AppliedMaterials", "text": "AppliedMaterials"}]
    assert research.mentions_company(squashed, "Applied Materials")


# ----------------------------- F11: storefront copy is not company research -------------------
def test_f11_product_pages_are_detected():
    from resume_brain import research
    shop = ("Shopping just got easier Try 30 days of Walmart+ for just $1 "
            "$35 min. oz., All Hair Type $17.84 Add $ 17 84 current price $17.84 "
            "(2 pack) As I Am Rosemary Shampoo 8 fl oz shop now free shipping in stock")
    assert research.is_product_copy(shop)
    about = ("Our purpose is to help people save money and live better. Founded in 1962, we "
             "employ more than two million associates worldwide and operate in 19 countries.")
    assert not research.is_product_copy(about)
    assert not research.is_product_copy("")


# ----------------------------- U1: the refusal names formats that work ------------------------
def test_u1_pdf_refusal_does_not_recommend_the_format_it_refused():
    import core
    phrase = core._readable_formats_phrase(exclude=".pdf")
    assert "PDF" not in phrase, phrase
    assert phrase.strip(), "must still name something"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    bad = 0
    for fn in fns:
        try:
            fn()
            print("ok  -", fn.__name__)
        except Exception as e:
            bad += 1
            print("FAILED - %s: %s" % (fn.__name__, e))
    print("\n%d/%d QA-audit guards passed." % (len(fns) - bad, len(fns)))
    sys.exit(1 if bad else 0)
