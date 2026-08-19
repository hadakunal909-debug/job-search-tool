"""
test_resume_store.py — guards the résumé-store unification, i.e. the bug where the feed scored
every job against an empty string while a full résumé sat one table away.

No external test deps, and NO database: run it directly
    python test_resume_store.py
or via pytest if you have it (functions are named test_*).

Background, because the fix looks like plumbing and is not. There are two stores:
`users.resume` (one text column, and the ONLY value the feed's match % scores against) and the
`resumes` table (the library Resume Brain manages). The sync between them ran one way and only
when the library was completely empty. Measured on the live database, that left two of four
accounts with a résumé in the library and '' in users.resume, and a third with two different
documents in the two places.

Covers:
  * set_active_resume clears the previous flag, sets the new one, and mirrors text into the cache
  * a mirror-write failure must not roll back the activation (the library stays the truth)
  * get_active_resume falls back to the newest row, so a library predating the column still answers
  * _ensure_resume_migrated repairs all three directions, and each is idempotent
  * adding `active` to RESUME_FIELDS is what makes it persist at all — assert it is listed
  * a résumé save that predates the migration must not send `active` and must not raise
"""
import re

import db


# --------------------------------------------------------------------------- in-memory fake store
class Fake(object):
    """Stands in for the three db calls set_active_resume orchestrates. Records the write order,
    because clear-then-set is the part that has to be right."""

    def __init__(self, rows, legacy="", mirror_raises=False):
        self.rows = [dict(r) for r in rows]
        self.legacy = legacy
        self.mirror_raises = mirror_raises
        self.writes = []

    def list_resumes(self, _user):
        return [dict(r) for r in self.rows]

    def save_resume(self, _user, rec):
        self.writes.append(dict(rec))
        for r in self.rows:
            if r.get("id") == rec.get("id"):
                r.update(rec)
                return True, r["id"]
        self.rows.append(dict(rec))
        return True, rec.get("id")

    def set_user_resume(self, _user, text):
        if self.mirror_raises:
            raise RuntimeError("PostgREST 400: column does not exist")
        self.legacy = text
        self.writes.append({"_mirror": text})

    def get_user(self, _user, _cols=None):
        return {"resume": self.legacy}


def _install(fake):
    """Point db's own helpers at the fake and return a restore callable."""
    names = ("list_resumes", "save_resume", "set_user_resume", "get_user")
    saved = {n: getattr(db, n) for n in names}
    for n in names:
        setattr(db, n, getattr(fake, n))
    return lambda: [setattr(db, n, f) for n, f in saved.items()]


ROWS = [
    {"id": "a", "name": "old", "content": "AAA resume text", "active": True},
    {"id": "b", "name": "new", "content": "BBB resume text", "active": False},
]


# ------------------------------------------------------------------------------------------ tests
def test_active_flag_is_a_persisted_field():
    """save_resume filters every payload to RESUME_FIELDS and drops unknown keys SILENTLY, so
    omitting `active` there would make activation a no-op that reports success."""
    assert "active" in db.RESUME_FIELDS, db.RESUME_FIELDS


def test_set_active_clears_the_previous_row_and_mirrors():
    fake = Fake(ROWS, legacy="AAA resume text")
    restore = _install(fake)
    try:
        got = db.set_active_resume("u", "b")
    finally:
        restore()
    assert got and got["id"] == "b" and got["active"] is True
    assert [r["id"] for r in fake.rows if r.get("active")] == ["b"], fake.rows
    # The old row must be explicitly cleared, not just left behind.
    cleared = [w for w in fake.writes if w.get("id") == "a"]
    assert cleared and cleared[0]["active"] is False, fake.writes
    # And the cache the feed reads must now hold the NEW text.
    assert fake.legacy == "BBB resume text"


def test_mirror_failure_does_not_undo_the_activation():
    """The library is the truth. If writing the derived cache fails — which is exactly what
    happens on a database where the migration has not been pasted yet — the activation must
    still stand, because the next successful save repairs the cache."""
    fake = Fake(ROWS, legacy="AAA resume text", mirror_raises=True)
    restore = _install(fake)
    try:
        got = db.set_active_resume("u", "b")
    finally:
        restore()
    assert got and got["id"] == "b"
    assert [r["id"] for r in fake.rows if r.get("active")] == ["b"]
    assert fake.legacy == "AAA resume text"        # unchanged, but not fatal


def test_activating_does_not_reset_created_at():
    """save_resume stamps created_at whenever it is absent, and a flag flip is a PARTIAL update —
    so omitting it would silently re-date the résumé on every switch. created_at is what the
    newest-row fallback and the library ordering both sort on, so that would scramble both."""
    rows = [{"id": "a", "content": "AAA", "active": True, "created_at": "2026-01-01T00:00:00Z"},
            {"id": "b", "content": "BBB", "active": False, "created_at": "2026-02-02T00:00:00Z"}]
    fake = Fake(rows, legacy="AAA")
    restore = _install(fake)
    try:
        db.set_active_resume("u", "b")
    finally:
        restore()
    sent = {w["id"]: w for w in fake.writes if "id" in w}
    assert sent["a"]["created_at"] == "2026-01-01T00:00:00Z", sent["a"]
    assert sent["b"]["created_at"] == "2026-02-02T00:00:00Z", sent["b"]


def test_set_active_on_a_missing_id_is_a_no_op():
    fake = Fake(ROWS, legacy="AAA resume text")
    restore = _install(fake)
    try:
        assert db.set_active_resume("u", "nope") is None
    finally:
        restore()
    assert fake.writes == []
    assert [r["id"] for r in fake.rows if r.get("active")] == ["a"]


def test_get_active_falls_back_to_newest():
    """A library written before the column existed has no flag anywhere. Returning None there
    would read as "no résumé" and score the feed against nothing — the very bug being fixed."""
    unflagged = [{"id": "a", "content": "AAA"}, {"id": "b", "content": "BBB"}]
    fake = Fake(unflagged)
    restore = _install(fake)
    try:
        assert db.get_active_resume("u")["id"] == "b"
        assert db.get_active_resume("u") is not None
    finally:
        restore()

    empty = Fake([])
    restore = _install(empty)
    try:
        assert db.get_active_resume("u") is None
    finally:
        restore()


def test_get_active_prefers_the_flag_over_recency():
    fake = Fake(ROWS)
    restore = _install(fake)
    try:
        assert db.get_active_resume("u")["id"] == "a"      # flagged, though not newest
    finally:
        restore()


# ---- the three repair directions, driven through web._ensure_resume_migrated ----
def _repair(rows, legacy):
    import web
    fake = Fake(rows, legacy=legacy)
    restore = _install(fake)
    try:
        web._resume_repair_tried.discard("u")
        web._ensure_resume_migrated("u")
    finally:
        restore()
    return fake


def test_repair_adopts_a_legacy_resume_into_an_empty_library():
    fake = _repair([], "LEGACY text long enough to matter")
    assert len(fake.rows) == 1
    assert fake.rows[0]["content"] == "LEGACY text long enough to matter"
    assert fake.rows[0]["active"] is True          # adopted AND selected, not just adopted


def test_repair_flags_an_active_row_when_none_is_flagged():
    """Prefer the row the feed has actually been scoring, so repairing cannot silently change
    someone's match percentages."""
    rows = [{"id": "a", "content": "AAA"}, {"id": "b", "content": "BBB"}]
    fake = _repair(rows, "AAA")
    assert [r["id"] for r in fake.rows if r.get("active")] == ["a"], fake.rows


def test_repair_seeds_an_empty_legacy_column_from_the_library():
    """THE production bug: résumé in the library, '' in users.resume, whole feed scored against
    an empty string."""
    rows = [{"id": "a", "content": "LIBRARY ONLY text", "active": True}]
    fake = _repair(rows, "")
    assert fake.legacy == "LIBRARY ONLY text"


def test_repair_is_idempotent_and_quiet_when_nothing_is_wrong():
    rows = [{"id": "a", "content": "SAME", "active": True}]
    fake = _repair(rows, "SAME")
    assert fake.writes == [], fake.writes          # a healthy account is not rewritten
    assert fake.legacy == "SAME"


def test_repair_does_nothing_for_a_user_with_no_resume_anywhere():
    fake = _repair([], "")
    assert fake.rows == [] and fake.writes == [] and fake.legacy == ""


def test_repair_never_raises():
    """It runs on the hot read path, so a transport error must not take a page down."""
    import web

    class Broken(object):
        def list_resumes(self, _u):
            raise RuntimeError("no such column: active")

        def get_user(self, _u, _c=None):
            raise RuntimeError("down")

        def save_resume(self, *a, **k):
            raise RuntimeError("down")

        def set_user_resume(self, *a, **k):
            raise RuntimeError("down")

    restore = _install(Broken())
    try:
        web._resume_repair_tried.discard("u")
        web._ensure_resume_migrated("u")           # must not raise
    finally:
        restore()


def test_repair_runs_at_most_once_per_user_per_process():
    """current_resume() calls this on every cache miss where the column is empty. Without the
    gate, a user who genuinely has no résumé pays two queries a minute forever."""
    import web
    web._resume_repair_tried.discard("u")
    calls = []
    saved = web._ensure_resume_migrated
    web._ensure_resume_migrated = lambda u: calls.append(u)
    fake = Fake([], legacy="")
    restore = _install(fake)
    try:
        # Drive current_resume twice with the cache defeated between calls.
        import flask
        with web.app.test_request_context("/"):
            flask.session["user"] = "u"
            web._resume_cache.pop("u", None)
            web.current_resume()
            web._resume_cache.pop("u", None)
            web.current_resume()
    finally:
        restore()
        web._ensure_resume_migrated = saved
        web._resume_cache.pop("u", None)
        web._resume_repair_tried.discard("u")
    assert calls == ["u"], calls


def test_resume_files_are_registered_everywhere_they_must_be():
    """A new table is a 403 from the proxy transport, an uncounted row in the admin panel, and an
    un-cleaned orphan on account deletion until it is listed in each of these."""
    import dbproxy
    import web
    assert "resume_files" in dbproxy.ALLOWED_TABLES
    assert "resume_files" in web._COUNTED_TABLES
    assert "resume_files" in web._USER_SCOPED_TABLES
    # Children before parents: resume_files has an FK to resumes, so it must be deleted first.
    order = db._user_child_tables()
    assert order.index("resume_files") < order.index("resumes"), order


def test_every_uploadable_format_is_also_storable():
    """These two lists drifted: .md could be uploaded and scored but was not a storable kind, so a
    Markdown upload showed an empty Original File tab with nothing explaining why."""
    import core
    uploadable = set(e.lstrip(".") for e in core.RESUME_UPLOAD_EXTS)
    assert uploadable <= set(db.RESUME_FILE_KINDS), \
        "uploadable but not storable: %s" % sorted(uploadable - set(db.RESUME_FILE_KINDS))


def test_the_upload_box_offers_what_the_parser_accepts():
    """A format missing from the accept attribute is a format users never discover."""
    import core
    import io as _io
    exts = set(e.lstrip(".") for e in core.RESUME_UPLOAD_EXTS)
    for tpl in ("templates/brain_home.html", "templates/brain_teach.html", "templates/welcome.html"):
        html = _io.open(tpl, encoding="utf-8").read()
        m = re.search(r'accept="([^"]*)"', html)
        assert m, "%s has an upload form with no accept attribute" % tpl
        offered = set(x.strip().lstrip(".") for x in m.group(1).split(","))
        assert exts <= offered, "%s omits %s" % (tpl, sorted(exts - offered))


def test_resume_file_listings_never_carry_the_payload():
    """The whole reason this is a sibling table: db.list_resumes selects *, and profile_text walks
    every row on nearly every request. A blob that can ride along, will."""
    assert "b64" in db.RESUME_FILE_FIELDS
    assert "b64" not in db.RESUME_FILE_META


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d résumé-store checks passed." % len(fns))
