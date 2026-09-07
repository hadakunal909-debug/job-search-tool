"""
test_wishlist.py — a company we cannot scrape must not be silently discarded.

Run it directly
    python test_wishlist.py
or via pytest (functions are named test_*).

WHY THIS EXISTS. Both add-a-board paths dead-ended. The web page answered "That isn't a readable
job board, so it can only be listed as apply-direct — which needs a company name"; the extension
answered "No scrapeable board behind this site." Both are true, and both throw away the fact that
somebody went looking for an employer they wanted followed. Nobody learned which employers people
keep asking for, and the same site got re-investigated by hand every few weeks.

The wish list is the record. These tests pin the four things that make it worth having rather
than a second pile of links: it MERGES a repeat instead of duplicating, it COUNTS the repeats, it
cannot blank what an earlier request supplied, and it can never take down the route it hangs off.

Offline: db is stubbed.
"""
import io
import os
import sys

import db as real_db

APP = os.path.dirname(os.path.abspath(__file__))
FAILED = []


def _check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class _Stub(object):
    """Stands in for the wishlist table. Records what _upsert was asked to write."""

    def __init__(self, existing=None):
        self.rows = list(existing or [])
        self.writes = []

    def fetch(self, table, params=None, page=None):
        if table != real_db.WISHLIST_TABLE:
            return []
        return [dict(r) for r in self.rows]

    def upsert(self, rows, chunk=200, keys=None, table=None, pk="url"):
        self.writes.append({"rows": [dict(r) for r in rows], "keys": keys,
                            "table": table, "pk": pk})


def _run(fn, existing=None):
    stub = _Stub(existing)
    saved = (real_db._fetch_all, real_db._upsert, real_db.has_remote_db)
    real_db._fetch_all, real_db._upsert = stub.fetch, stub.upsert
    real_db.has_remote_db = lambda: True
    try:
        out = fn()
    finally:
        real_db._fetch_all, real_db._upsert, real_db.has_remote_db = saved
    return stub, out


def test_a_first_wish_is_recorded_with_its_reason():
    stub, ok = _run(lambda: real_db.add_wish(
        "https://acme.example/careers", "Acme", reason="no readable job board",
        source="addboard", user="kunal"))
    _check("add_wish reports success", ok is True)
    _check("it wrote to the wishlist table",
           stub.writes and stub.writes[0]["table"] == real_db.WISHLIST_TABLE,
           repr(stub.writes))
    row = stub.writes[0]["rows"][0] if stub.writes else {}
    _check("...keyed on url", stub.writes[0]["pk"] == "url")
    _check("...carrying the REASON it could not be taken",
           row.get("reason") == "no readable job board", repr(row))
    _check("...and who asked", row.get("added_by") == "kunal", repr(row))
    # The reason is the whole difference between a triageable list and a pile of links.


def test_a_repeat_wish_merges_and_counts():
    """The ranking signal. Without it the backlog is ordered by who asked FIRST, which is not
    a measure of anything; `requests` orders it by how many people actually wanted the site."""
    stub, ok = _run(lambda: real_db.add_wish("https://acme.example/careers", source="extension"),
                    existing=[{"url": "https://acme.example/careers", "requests": 3,
                               "status": "open"}])
    row = stub.writes[0]["rows"][0] if stub.writes else {}
    _check("a repeat increments requests", row.get("requests") == 4, repr(row))
    _check("...and does not create a second row", len(stub.writes[0]["rows"]) == 1)


def test_a_repeat_cannot_blank_what_the_first_request_supplied():
    """THE BUG THIS FILE CAUGHT. db._upsert sends the UNION of keys, so a None goes out as an
    explicit NULL. Somebody clicking the button without typing a company would otherwise erase
    the name the first requester took the trouble to enter -- the same shape as the derived-write
    erasure that the schema separation had to be audited for."""
    stub, _ = _run(lambda: real_db.add_wish("https://acme.example/careers"),   # no company
                   existing=[{"url": "https://acme.example/careers", "requests": 1,
                              "status": "open"}])
    row = stub.writes[0]["rows"][0] if stub.writes else {}
    _check("an empty company is OMITTED, not sent as null",
           "company" not in row, repr(row))
    for k in ("reason", "source", "added_by", "note"):
        _check("...same for %s" % k, k not in row, repr(row))


def test_asking_again_reopens_a_rejected_wish():
    """A second request is new information. Leaving it closed would swallow it silently."""
    stub, _ = _run(lambda: real_db.add_wish("https://acme.example/careers"),
                   existing=[{"url": "https://acme.example/careers", "requests": 1,
                              "status": "rejected"}])
    row = stub.writes[0]["rows"][0] if stub.writes else {}
    _check("a rejected wish goes back to open when asked for again",
           row.get("status") == "open", repr(row))


def test_it_never_takes_down_the_route_it_hangs_off():
    """add_wish is called from the FAILURE branch of two routes. An unmigrated table, or a
    transient proxy blip, must not turn 'we could not add your board' into a 500."""
    def boom(*a, **k):
        raise RuntimeError('relation "public.wishlist" does not exist')

    saved = (real_db._fetch_all, real_db._upsert, real_db.has_remote_db)
    real_db._fetch_all, real_db._upsert = boom, boom
    real_db.has_remote_db = lambda: True
    try:
        ok = real_db.add_wish("https://acme.example/careers")
        raised = False
    except Exception:
        ok, raised = None, True
    finally:
        real_db._fetch_all, real_db._upsert, real_db.has_remote_db = saved
    _check("a missing table does not raise", not raised)
    _check("...and it reports False so the caller can word itself honestly", ok is False)

    # list_wishes has the same duty: the page must render exactly as before.
    saved = (real_db._fetch_all, real_db.has_remote_db)
    real_db._fetch_all, real_db.has_remote_db = boom, lambda: True
    try:
        got = real_db.list_wishes("open")
    finally:
        real_db._fetch_all, real_db.has_remote_db = saved
    _check("list_wishes degrades to []", got == [], repr(got))


def test_status_is_validated_and_a_review_is_not_a_delete():
    stub, ok = _run(lambda: real_db.set_wish_status("https://acme.example/careers", "adopted"))
    _check("a valid status writes", ok is True)
    row = stub.writes[0]["rows"][0] if stub.writes else {}
    _check("...and stamps reviewed_at", bool(row.get("reviewed_at")), repr(row))
    for bad in ("deleted", "", "DROP TABLE", None):
        stub2, ok2 = _run(lambda b=bad: real_db.set_wish_status("https://x", b))
        _check("status %r is refused" % (bad,), ok2 is False and not stub2.writes)
    # Nothing here deletes. A rejected wish is the record that stops the same site being
    # investigated a third time, which is most of the value of keeping the list.


def test_the_two_dead_ends_now_record_a_wish():
    """Source-text checks, because both are branches inside long request handlers that need a
    live database and a session to reach. The BEHAVIOUR they call is unit-tested above."""
    web = io.open(os.path.join(APP, "web.py"), encoding="utf-8").read()
    _check("/add-board records a wish when no board is readable",
           'db.add_wish(url, reason="no readable job board"' in web)
    _check("/add-board records one when a board is found but unreadable",
           'reason="detected %s but read 0 postings" % ats' in web)
    _check("the extension's twin offers a wish instead of a shrug",
           '"can_wish": True' in web and 'source="extension"' in web)
    _check("...and the old dead-end wording is gone from the popup's not-found branch",
           web.count('"No scrapeable board behind this site."') == 0, "still there")

    popup = io.open(os.path.join(APP, "extension", "popup.js"), encoding="utf-8").read()
    _check("the popup has a wish second-click", "wishPending" in popup)
    _check("...separate from the ADD second-click, which posts different fields",
           popup.index("if (wishPending)") < popup.index("if (boardFound)"),
           "the wish branch must come first or an add would file a wish")


def test_the_table_is_reachable_from_off_the_box():
    """dbproxy's allowlist is deployed CODE. Until the release lands, every laptop and Actions
    call answers 403 -- which reads exactly like an auth failure, not an allowlist one. The
    cPanel app talks to Postgres directly and works without it, so this fails nowhere else."""
    import dbproxy
    _check("wishlist is allowlisted on the proxy",
           "wishlist" in dbproxy.ALLOWED_TABLES)


def test_the_migration_matches_what_the_code_writes():
    sql = io.open(os.path.join(APP, "MIGRATION_wishlist.sql"), encoding="utf-8").read()
    _check("the migration creates the table",
           "create table if not exists public.wishlist" in sql)
    _check("...keyed on url, so db._upsert works unchanged",
           "url          text primary key" in sql)
    for col in ("company", "reason", "source", "added_by", "requests", "status", "note",
                "reviewed_at"):
        _check("...with %s" % col, col in sql)
    # db.py:262 marks this LAST -- without it PostgREST answers from its cached schema.
    _check("...and ends with the schema reload",
           sql.strip().endswith("notify pgrst, 'reload schema';"))
    _check("NO foreign key: a wish is a company we do NOT have",
           "references public.jobs" not in sql and "references public.boards" not in sql)


def main():
    print("wishlist")
    for fn in (test_a_first_wish_is_recorded_with_its_reason,
               test_a_repeat_wish_merges_and_counts,
               test_a_repeat_cannot_blank_what_the_first_request_supplied,
               test_asking_again_reopens_a_rejected_wish,
               test_it_never_takes_down_the_route_it_hangs_off,
               test_status_is_validated_and_a_review_is_not_a_delete,
               test_the_two_dead_ends_now_record_a_wish,
               test_the_table_is_reachable_from_off_the_box,
               test_the_migration_matches_what_the_code_writes):
        fn()
    print("\n%s" % ("FAILED: " + ", ".join(FAILED) if FAILED else "all checks passed"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
