#!/usr/bin/env python3
"""pgrest.py's translation layer, asserted statement by statement.

WHY THESE ASSERTIONS ARE ON THE SQL TEXT and not on query results: a storage shim that gets a
filter subtly wrong does not raise, it returns the wrong rows. `jd=not.is.null` translated as
`IS NULL` would hand score_jobs an empty backlog and read as "nothing to do"; a dropped WHERE on
a DELETE empties a table. Those are silent, and they are silent in production. Checking the
generated statement catches them here instead, needs no database, and so runs in CI where there
are no credentials at all.

Every pattern below was taken from a real db.py call site, not invented — the query language it
emits is exactly what pgrest is required to support and nothing more.

    python scripts/test_pgrest.py
"""
import datetime
import decimal
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pgrest

fails = []
ran = []


def check(label, ok, got=None):
    ran.append(label)
    print("  %s  %s" % ("ok " if ok else "FAIL", label))
    if not ok:
        fails.append(label)
        if got is not None:
            print("        got: %s" % (got,))


def sql_of(method, table, params=None, body=None, prefer=""):
    s, a, _ = pgrest.build(method, table, params or {}, body, prefer)
    return " ".join(s.split()), a


def raises(fn):
    try:
        fn()
        return False
    except pgrest.PgRestError:
        return True


print("=" * 74)
print("SELECT — the feed read, db.load_jobs")
print("=" * 74)
s, a = sql_of("GET", "jobs", {"select": "url,title,company", "order": "url",
                              "limit": 1000, "offset": 2000})
check("columns, order, limit and offset",
      s == 'SELECT "url", "title", "company" FROM "jobs" ORDER BY "url" LIMIT %s OFFSET %s'
      and a == [1000, 2000], s)

s, _ = sql_of("GET", "jobs", {"select": "*"})
check("select=* stays a star", s == 'SELECT * FROM "jobs"', s)

s, _ = sql_of("GET", "jobs", {"select": "first_seen", "order": "first_seen.desc.nullslast",
                              "limit": 1})
check("order=first_seen.desc.nullslast (jobs_fingerprint)",
      s == 'SELECT "first_seen" FROM "jobs" ORDER BY "first_seen" DESC NULLS LAST LIMIT %s', s)

print()
print("=" * 74)
print("FILTERS — every operator db.py emits")
print("=" * 74)
s, a = sql_of("GET", "jobs", {"select": "url", "jd": "not.is.null"})
check("urls_with_jd: jd=not.is.null",
      s == 'SELECT "url" FROM "jobs" WHERE "jd" IS NOT NULL' and a == [], s)

s, a = sql_of("GET", "jobs", {"select": "url", "or": "(jd.is.null,jd.eq.)"})
check("urls_missing_jd: or=(jd.is.null,jd.eq.)",
      s == 'SELECT "url" FROM "jobs" WHERE ("jd" IS NULL OR "jd" = %s)' and a == [""], s)

s, a = sql_of("GET", "jobs", {"select": "*", "url": 'in.("a","b")'})
check("load_jobs_by_urls: url=in.(...) becomes = ANY",
      s == 'SELECT * FROM "jobs" WHERE "url" = ANY(%s)' and a == [["a", "b"]], (s, a))

# db._in_list escapes quotes and backslashes so a URL carrying a comma cannot break the list.
tricky = 'in.("http://x.com/a,b","he said \\"hi\\"")'
_, a = sql_of("DELETE", "jobs", {"url": tricky})
check("in.() survives a comma and an escaped quote inside a value",
      a == [["http://x.com/a,b", 'he said "hi"']], a)

s, a = sql_of("GET", "events", {"select": "*", "at": "gte.2026-08-01"})
check("gte on a timestamp", s == 'SELECT * FROM "events" WHERE "at" >= %s'
      and a == ["2026-08-01"], (s, a))

s, a = sql_of("GET", "users", {"select": "*", "username": "eq.a.b@c.com"})
check("eq operand keeps its dots (split on the FIRST one only)",
      a == ["a.b@c.com"], a)

s, _ = sql_of("GET", "jobs", {"select": "url", "company": "neq.__none__"})
check("neq", s == 'SELECT "url" FROM "jobs" WHERE "company" <> %s', s)

print()
print("=" * 74)
print("COUNT — table_count's HEAD, which must return no rows")
print("=" * 74)
s, a, _ = pgrest.build("HEAD", "jobs", {"select": "url"}, None, "count=exact")
check("HEAD becomes count(*)", " ".join(s.split()) == 'SELECT count(*) AS n FROM "jobs"', s)

print()
print("=" * 74)
print("UPSERT — db._upsert, the write the whole scraper depends on")
print("=" * 74)
rows = [{"url": "u1", "title": "A", "company": "X"}, {"url": "u2", "title": "B", "company": "Y"}]
s, a = sql_of("POST", "jobs", {"on_conflict": "url"}, rows,
              "resolution=merge-duplicates,return=minimal")
check("ON CONFLICT names the key and updates the rest",
      s == ('INSERT INTO "jobs" ("url", "title", "company") VALUES (%s, %s, %s), (%s, %s, %s) '
            'ON CONFLICT ("url") DO UPDATE SET "title" = EXCLUDED."title", '
            '"company" = EXCLUDED."company"'), s)
check("...the conflict column is NOT in the SET list", '"url" = EXCLUDED' not in s)
check("...args are flattened row-major", a == ["u1", "A", "X", "u2", "B", "Y"], a)
check("...return=minimal suppresses RETURNING", "RETURNING" not in s)

s, _ = sql_of("POST", "jobs", {"on_conflict": "url"}, [{"url": "u1"}],
              "resolution=merge-duplicates,return=minimal")
check("a row that is ONLY the conflict key degrades to DO NOTHING",
      s.endswith('ON CONFLICT ("url") DO NOTHING'), s)

s, _ = sql_of("POST", "user_jobs", {}, [{"username": "a", "url": "u"}], "")
check("a plain insert returns its rows", s.endswith("RETURNING *"), s)

check("bulk rows with different keys are refused",
      raises(lambda: sql_of("POST", "jobs", {"on_conflict": "url"},
                            [{"url": "a", "t": 1}, {"url": "b"}], "resolution=merge-duplicates")))
check("merge-duplicates without on_conflict is refused",
      raises(lambda: sql_of("POST", "jobs", {}, rows, "resolution=merge-duplicates")))

print()
print("=" * 74)
print("PATCH / DELETE")
print("=" * 74)
s, a = sql_of("PATCH", "jobs", {"url": "eq.u1"}, {"jd": "text", "match_score": 70},
              "return=minimal")
check("update sets then filters, args in that order",
      s == 'UPDATE "jobs" SET "jd" = %s, "match_score" = %s WHERE "url" = %s'
      and a == ["text", 70, "u1"], (s, a))

s, _ = sql_of("DELETE", "jobs", {"url": 'in.("a")'}, None, "return=minimal")
check("filtered delete", s == 'DELETE FROM "jobs" WHERE "url" = ANY(%s)', s)

check("an UNFILTERED delete is refused outright",
      raises(lambda: sql_of("DELETE", "jobs", {}, None, "")))

print()
print("=" * 74)
print("REFUSALS — anything outside the supported subset must raise, not guess")
print("=" * 74)
check("unknown operator", raises(lambda: sql_of("GET", "jobs", {"title": "cs.{a}"})))
check("is. with a non-null operand", raises(lambda: sql_of("GET", "jobs", {"jd": "is.true"})))
check("an identifier that isn't one",
      raises(lambda: sql_of("GET", "jobs", {"select": "url; drop table jobs"})))
check("a table name that isn't one", raises(lambda: sql_of("GET", "jobs; --", {})))
check("malformed or= group", raises(lambda: sql_of("GET", "jobs", {"or": "jd.is.null"})))

print()
print("=" * 74)
print("VALUE SHAPES — psycopg returns objects where PostgREST returns strings")
print("=" * 74)
check("date -> ISO string", pgrest.jsonify(datetime.date(2026, 8, 14)) == "2026-08-14")
check("datetime -> ISO string",
      pgrest.jsonify(datetime.datetime(2026, 8, 14, 9, 30)).startswith("2026-08-14T09:30"))
check("Decimal -> float", pgrest.jsonify(decimal.Decimal("120000.50")) == 120000.5)
check("nested dict/list are converted too",
      pgrest.jsonify({"a": [datetime.date(2026, 1, 2)]}) == {"a": ["2026-01-02"]})
check("a jsonb value passes through unchanged",
      pgrest.jsonify({"w": {"excel": 12.46}}) == {"w": {"excel": 12.46}})

print()
if fails:
    print("FAILED (%d): %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("all good - %d checks" % len(ran))
