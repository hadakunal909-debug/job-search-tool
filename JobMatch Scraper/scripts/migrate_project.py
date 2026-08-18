#!/usr/bin/env python3
"""Move this app's data into a DIFFERENT Supabase project, reading the old one as little as
possible.

WHY IT IS BUILT THIS WAY. The obvious migration — read every table out of the old project and
write it to the new one — costs ~150 MB of egress on the project you are leaving, which is
normally the exact resource that ran out and made you leave. It is also unnecessary here: the
`jobs` table is 99% of the volume, and this machine already holds all of it.

    jobs_snapshot.json.gz   22,320 rows, every column except `jd`   (web.py's shared feed cache)
    jd_cache.json.gz        20,570 descriptions keyed by url        (score_jobs' JD corpus)

Merge those two and you have the table, complete with first_seen, for ZERO reads of the source.
Measured coverage on 2026-08-14: 18,351 of 22,320 rows get their description from disk. The
remainder are rows whose JD this machine never cached, and --fill-jds asks the source for
exactly those and nothing else, after first subtracting the ones that have no stored JD at all
(db.urls_missing_jd, ~0.5 MB) so it does not pay for rows that would come back empty.

WHAT THIS DOES NOT DO: schema. PostgREST cannot create tables, and this repo has no single
create-everything script — SUPABASE_RUN_ALL.sql assumes jobs/users/applications already exist,
and the rest is spread over SUPABASE_PENDING_MIGRATION.sql, MIGRATION_jd_columns.sql and
db.JOBS_DERIVED_SQL. Getting that set wrong fails SILENTLY: db.load_jobs drops optional columns
one at a time until a select succeeds, so a missing column just quietly vanishes from the feed.
Dump the schema instead, which is exact and costs a few KB:

    pg_dump --schema-only --no-owner --no-privileges -d "$OLD_DB_URL" -f schema.sql
    psql -d "$NEW_DB_URL" -f schema.sql

Use the SESSION POOLER connection string from Project Settings -> Database (the direct one is
IPv6-only on the free tier), and a pg_dump at least as new as the server. Then --check here
confirms every table landed before you move a row.

USAGE
    set TARGET_SUPABASE_URL / TARGET_SUPABASE_KEY to the NEW project (source credentials come
    from db.py as usual — .env or .streamlit/secrets.toml).

    python scripts/migrate_project.py --check                 # preflight, reads nothing
    python scripts/migrate_project.py --jobs        --apply   # local files -> new project
    python scripts/migrate_project.py --fill-jds    --apply   # the JDs disk didn't have
    python scripts/migrate_project.py --tables      --apply   # users, boards, applications, ...
    python scripts/migrate_project.py --verify                # counts on both sides

Every mode is a DRY RUN until --apply. Nothing here ever writes to the source.
"""
import argparse
import gzip
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db

SNAPSHOT = "jobs_snapshot.json.gz"
JD_CACHE = "jd_cache.json.gz"
PROGRESS = "migrate_progress.json"

# The small tables, in dependency order — users before anything that references a username, so a
# restore against a schema carrying the admin-panel foreign keys does not trip over itself.
CORE_TABLES = ["users", "profiles", "user_jobs", "applications", "resumes", "boards",
               "blocked_companies", "brain_companies", "learned_answers", "scrape_status",
               "admin_audit"]
# Opt-in. events/events_daily are analytics HISTORY and tailored_cache is a cache that rebuilds
# itself; none of them changes how the app behaves, and events is usually the second-biggest
# table in the database.
EXTRA_TABLES = ["events", "events_daily", "tailored_cache"]

# The conflict key per table, READ OUT OF db.py's own on_conflict= params rather than guessed.
# Real PostgREST infers the key from the primary key when you omit it, so db.py can be sloppy in
# places and still work; pgrest.py refuses to guess, because inferring the wrong key on a bulk
# merge is how you silently collapse rows. A table that isn't listed is loaded with
# ignore-duplicates instead, which needs no key and makes a re-run safe either way.
CONFLICT_KEYS = {"jobs": "url", "user_jobs": "username,url", "profiles": "username",
                 "boards": "url", "blocked_companies": "name_key", "applications": "id",
                 "resumes": "id", "brain_companies": "domain", "tailored_cache": "id",
                 "learned_answers": "username,key", "scrape_status": "id",
                 # users is the one db.py never spells out (create_user INSERTs rather than
                 # upserting), but username is its primary key and user_jobs/profiles/resumes
                 # all carry a foreign key to it -- which is also why it must load first.
                 "users": "username"}

# Stable column to page by, for tables with no entry above. Guessed conflict keys would be
# dangerous (a wrong one silently merges rows); a wrong sort column only changes row order.
ORDER_KEYS = {"admin_audit": "id", "events": "id", "events_daily": "day"}

_target = {"url": None, "key": None, "sess": None}


def session():
    """The thing that talks to the TARGET.

    Either a Supabase project (db's requests session) or a Postgres server (pgrest.Session,
    which implements the same five verbs over psycopg). Both answer .head/.post identically, so
    every push below is written once and works for both destinations.

    The Postgres path is what makes a cPanel migration cheap: run this script ON the cPanel box
    with TARGET_PG_DSN pointed at localhost, and the rows never cross the internet at all.
    """
    if _target["sess"] is None:
        dsn = os.environ.get("TARGET_PG_DSN") or ""
        if dsn:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import pgrest
            _target["sess"] = pgrest.Session(dsn)
        else:
            _target["sess"] = db._http
    return _target["sess"]


def target_creds():
    dsn = os.environ.get("TARGET_PG_DSN") or ""
    if dsn:
        # pgrest only reads the table out of the path, so the host half is a placeholder — the
        # DSN is what actually locates the database.
        return "pg://local", ""
    url = (os.environ.get("TARGET_SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("TARGET_SUPABASE_KEY") or ""
    if not (url and key):
        sys.exit("Set TARGET_SUPABASE_URL + TARGET_SUPABASE_KEY (a Supabase project), or "
                 "TARGET_PG_DSN (a Postgres server) first.")
    src, _ = db._creds()
    # The one mistake this script must make impossible. Pointing the target at the source turns
    # every "migration" below into a no-op upsert over the live table -- which would look like a
    # complete success while moving nothing, and would spend the egress you are trying to save.
    if src and url.rstrip("/") == src.rstrip("/"):
        sys.exit("TARGET is the same project as the SOURCE. Refusing.")
    return url, key


def theaders(extra=None):
    h = {"apikey": _target["key"], "Authorization": "Bearer %s" % _target["key"],
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def trest(path=""):
    return "%s/rest/v1/%s" % (_target["url"], path)


def tcount(table):
    """Row count on the TARGET, via a HEAD — no body, so it costs nothing on either side."""
    try:
        r = session().head(trest(table), headers=theaders({"Prefer": "count=exact"}),
                           params={"select": "*"}, timeout=30)
        if r.status_code >= 400:
            return None
        rng = r.headers.get("Content-Range") or ""
        tail = rng.rsplit("/", 1)[-1] if "/" in rng else ""
        return int(tail) if tail.isdigit() else None
    except Exception:
        return None


def push(table, rows, chunk=100, apply=False, label=""):
    """Upsert `rows` into the target, in chunks, resumable.

    chunk=100 rather than db._upsert's 200 because these rows carry the jd column: at ~6.6 KB a
    description, 200 rows is a 1.3 MB request body, and a single connection blip then costs the
    whole batch. Progress is written after every chunk so a killed run resumes instead of
    restarting -- 22k rows is ~223 requests and the first attempt is the one most likely to hit
    a laptop going to sleep.

    resolution=merge-duplicates makes it idempotent on the url primary key, so re-running after
    a failure is always safe. return=minimal keeps PostgREST from echoing every row back, which
    would be egress on the NEW project for data we just sent it.
    """
    if not rows:
        print("  nothing to send.")
        return 0
    key = "%s:%s" % (table, label or "all")
    done = 0
    if apply and os.path.exists(PROGRESS):
        try:
            done = int((json.load(open(PROGRESS, encoding="utf-8")) or {}).get(key) or 0)
        except Exception:
            done = 0
        if done:
            print("  resuming after %d rows already sent." % done)
    sent, t0 = done, time.time()
    while sent < len(rows):
        batch = rows[sent:sent + chunk]
        if not apply:
            sent += len(batch)
            continue
        # A known key merges (re-running overwrites with the same values, which is correct —
        # the local snapshot IS the source of truth here). An unknown one falls back to
        # ON CONFLICT DO NOTHING, so a resumed run still cannot duplicate a row.
        conflict = CONFLICT_KEYS.get(table)
        r = session().post(trest(table), headers=theaders(
            {"Prefer": ("resolution=merge-duplicates,return=minimal" if conflict
                        else "resolution=ignore-duplicates,return=minimal")}),
            params=({"on_conflict": conflict} if conflict else {}),
            data=json.dumps(batch).encode("utf-8"), timeout=90)
        if r.status_code >= 400:
            print("  FAILED at row %d: HTTP %s %s" % (sent, r.status_code, (r.text or "")[:300]))
            print("  fix the cause and re-run the same command; it resumes from here.")
            sys.exit(1)
        sent += len(batch)
        try:
            state = json.load(open(PROGRESS, encoding="utf-8")) if os.path.exists(PROGRESS) else {}
        except Exception:
            state = {}
        state[key] = sent
        json.dump(state, open(PROGRESS, "w", encoding="utf-8"))
        if sent % 2000 < chunk or sent >= len(rows):
            rate = (sent - done) / max(0.1, time.time() - t0)
            print("    %6d / %d   (%.0f rows/s)" % (sent, len(rows), rate))
    return sent


def local_jobs():
    """The jobs table, rebuilt from this machine. Returns (rows, n_with_jd, n_without)."""
    try:
        snap = json.load(gzip.open(SNAPSHOT, "rt", encoding="utf-8"))
        rows = snap.get("rows") or []
    except Exception as e:
        sys.exit("Cannot read %s (%s). It is written by the web app; load the feed once."
                 % (SNAPSHOT, str(e)[:80]))
    try:
        jds = json.load(gzip.open(JD_CACHE, "rt", encoding="utf-8"))
    except Exception:
        jds = {}
        print("  (no %s — descriptions will all come from --fill-jds)" % JD_CACHE)

    # One uniform key set across every row. PostgREST takes a chunk's columns from the union of
    # its rows' keys, so rows with different shapes in one request make the missing ones send an
    # explicit null anyway; deciding it here means the nulls are ours and are intended. Every
    # value is the snapshot's, which IS the source of truth for this migration, so a re-run
    # overwriting with the same values is correct rather than lossy.
    cols = set()
    for r in rows:
        cols |= set(r)
    cols.add("jd")
    cols = sorted(cols)

    out, have, missing = [], 0, 0
    for r in rows:
        u = r.get("url")
        if not u:
            continue
        jd = jds.get(u) or ""
        have, missing = (have + 1, missing) if jd else (have, missing + 1)
        row = {c: r.get(c) for c in cols}
        row["url"] = u
        row["jd"] = jd or None
        # Empty strings are not the same as absent for the date columns: found_date "" would be
        # rejected by a date type, while null is the honest "we never knew".
        for c in ("found_date", "first_seen", "last_seen", "posted_verified"):
            if c in row and not (row.get(c) or ""):
                row[c] = None
        out.append(row)
    return out, have, missing


def cmd_check():
    print("=" * 74)
    print("PREFLIGHT")
    print("=" * 74)
    print("  source : %s" % (db._creds()[0] or "(none)"))
    print("  target : %s" % _target["url"])
    missing = []
    for t in CORE_TABLES + EXTRA_TABLES + ["jobs"]:
        n = tcount(t)
        print("    %-20s %s" % (t, "MISSING" if n is None else "%d rows" % n))
        if n is None:
            missing.append(t)
    if missing:
        print("\n  %d table(s) absent on the target. Load the schema first (see the module\n"
              "  docstring: pg_dump --schema-only). Migrating into a partial schema is the one\n"
              "  failure that does not announce itself." % len(missing))
    rows, have, without = local_jobs()
    print("\n  local files can rebuild %d job rows: %d with a description, %d without."
          % (len(rows), have, without))
    print("  first_seen present on %d of them."
          % sum(1 for r in rows if r.get("first_seen")))
    return 1 if missing else 0


def cmd_jobs(apply):
    rows, have, without = local_jobs()
    size = sum(len(r.get("jd") or "") for r in rows)
    print("=" * 74)
    print("JOBS  (from local files — the source project is not read at all)")
    print("=" * 74)
    print("  %d rows, %d with a cached description, %d without." % (len(rows), have, without))
    print("  ~%.0f MB of description text to upload." % (size / 1048576.0))
    if not apply:
        print("\n  DRY RUN. Re-run with --apply to send.")
        return 0
    push("jobs", rows, chunk=100, apply=True, label="local")
    print("  target now holds %s rows." % tcount("jobs"))
    return 0


def cmd_fill_jds(apply):
    """Ask the SOURCE only for descriptions this machine does not have.

    Two subtractions before a single row is fetched, because this is the only step that spends
    egress on the project you are leaving:
      * rows already carrying a JD from jd_cache.json.gz — most of the corpus;
      * rows the source says have NO stored JD (db.urls_missing_jd, a urls-only select) — asking
        for those would pay full price for empty strings.
    """
    rows, _, _ = local_jobs()
    need = {r["url"] for r in rows if not (r.get("jd") or "")}
    print("=" * 74)
    print("FILL JDS  (the only step that reads the source)")
    print("=" * 74)
    print("  %d rows have no description from disk." % len(need))
    try:
        blank = db.urls_missing_jd()
    except Exception as e:
        print("  could not ask which rows lack a JD (%s) — assuming all of them have one."
              % str(e)[:70])
        blank = set()
    need -= blank
    print("  %d of those have no stored JD either, so %d will be fetched (~%.1f MB)."
          % (len(blank & {r["url"] for r in rows}), len(need), len(need) * 6664 / 1048576.0))
    if not need:
        return 0
    if not apply:
        print("\n  DRY RUN. Re-run with --apply to fetch and send.")
        return 0
    got = db.load_jobs_by_urls(sorted(need))
    payload = [{"url": r["url"], "jd": r.get("jd") or None} for r in got if r.get("url")]
    print("  source returned %d rows; patching them into the target." % len(payload))
    push("jobs", payload, chunk=100, apply=True, label="jds")
    return 0


def cmd_tables(apply, extras):
    names = CORE_TABLES + (EXTRA_TABLES if extras else [])
    print("=" * 74)
    print("SMALL TABLES  (read from the source — a few MB)")
    print("=" * 74)
    total = 0
    for t in names:
        # ORDER EXPLICITLY. db._fetch_all pages with `order=url` unless told otherwise, and
        # `url` exists on only three of these tables -- asking Supabase to order `users` by it
        # returns 400, which then cascaded: users never loaded, so user_jobs failed its
        # username foreign key at row 0. Order by the table's own key instead; it has to be a
        # stable column or the paged walk can repeat or skip rows between requests.
        order = ((CONFLICT_KEYS.get(t) or ORDER_KEYS.get(t) or "").split(",")[0]) or None
        try:
            rows = db._fetch_all(t, {"select": "*", "order": order} if order
                                 else {"select": "*"})
        except Exception as e:
            print("  %-20s skipped (%s)" % (t, str(e)[:60]))
            continue
        b = len(json.dumps(rows))
        total += b
        print("  %-20s %6d rows  ~%.2f MB" % (t, len(rows), b / 1048576.0))
        if rows and apply:
            push(t, rows, chunk=200, apply=True, label="copy")
    print("\n  ~%.1f MB read from the source in total." % (total / 1048576.0))
    if not apply:
        print("  DRY RUN. Re-run with --apply to write them.")
        return 0
    # SEQUENCES, LAST. Every row above was copied WITH its id, and inserting an explicit id does
    # not advance the sequence that column defaults from -- so events_id_seq sat at 1 while the
    # table already held ids up to 13,481. Every insert the live app made afterwards collided
    # with events_pkey and was swallowed by db.insert_events, which is how analytics went silent
    # for three days while this script reported a clean copy. Here is the only place that knows
    # a bulk copy just happened.
    sess = session()
    if hasattr(sess, "repair_sequences"):
        try:
            fixed = sess.repair_sequences()
            print("  sequences: %s" % (", ".join("%s.%s->%s" % f for f in fixed) or "none found"))
        except Exception as e:
            print("  sequences: REPAIR FAILED (%s) -- inserts into copied tables will collide"
                  % str(e)[:80])
    else:
        print("  sequences: target is not a direct Postgres session. Run by hand:"
              " SELECT setval('events_id_seq', (SELECT MAX(id) FROM events));")
    return 0


def cmd_verify():
    print("=" * 74)
    print("VERIFY")
    print("=" * 74)
    src_n = db.table_count(db.TABLE)
    print("  jobs   source %-8s target %s" % (src_n, tcount("jobs")))
    for t in CORE_TABLES + EXTRA_TABLES:
        print("  %-6s source %-8s target %s"
              % (t[:6], db.table_count(t), tcount(t)))
    print("\n  Both counts ride HEAD requests, so this costs nothing on either side.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="preflight: schema + what disk holds")
    ap.add_argument("--jobs", action="store_true", help="load jobs from local files")
    ap.add_argument("--fill-jds", action="store_true", help="fetch the JDs disk lacks")
    ap.add_argument("--tables", action="store_true", help="copy the small tables")
    ap.add_argument("--with-analytics", action="store_true",
                    help="include events/events_daily/tailored_cache in --tables")
    ap.add_argument("--verify", action="store_true", help="row counts, both sides")
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    a = ap.parse_args()
    if not any([a.check, a.jobs, a.fill_jds, a.tables, a.verify]):
        ap.print_help()
        return 2
    _target["url"], _target["key"] = target_creds()
    rc = 0
    if a.check:
        rc |= cmd_check()
    if a.jobs:
        rc |= cmd_jobs(a.apply)
    if a.fill_jds:
        rc |= cmd_fill_jds(a.apply)
    if a.tables:
        rc |= cmd_tables(a.apply, a.with_analytics)
    if a.verify:
        rc |= cmd_verify()
    return rc


if __name__ == "__main__":
    sys.exit(main())
