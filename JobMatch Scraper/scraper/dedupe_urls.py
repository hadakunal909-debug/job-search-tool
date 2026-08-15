"""
dedupe_urls.py — ONE-OFF migration: merge `jobs` rows that are the same posting stored
under two different URLs.

The `jobs` table is keyed on url alone, so before canonical_url() existed a posting served
under two URLs (notably Greenhouse's interchangeable boards.greenhouse.io /
job-boards.greenhouse.io hosts) became two rows. The scraper no longer creates these; this
cleans up the ones already there.

    python -m scraper.dedupe_urls              # DRY RUN — prints the plan, writes nothing
    python -m scraper.dedupe_urls --apply      # actually merge
    python -m scraper.dedupe_urls --apply --yes    # skip the confirmation prompt

Safety, in order of importance:
  * Grouping uses scraper.canonical_url(), which is deliberately conservative — see its
    docstring for the two normalizations that were rejected for merging distinct postings.
  * A group whose rows disagree on title or company is REFUSED, not merged. That is the
    signature of over-aggressive normalization, and a wrong merge is unrecoverable.
  * Per-user liked/applied/hidden state is moved to the surviving URL BEFORE the losing
    rows are deleted, so `db.all_flagged_urls()` state is never silently dropped. If both
    URLs carry a status for the same user, the survivor's is kept and the difference is
    reported rather than overwritten.
  * Merging is per-group and each step is idempotent, so an interrupted run can simply be
    re-run.

The `applications` tracker is intentionally left alone: its rows carry their own copy of
the title/company and are keyed per user, so a merged job URL costs nothing there.
"""
import collections
import sys

import db
import scraper


def _user_job_rows():
    """[(username, url, status)] across every user, both backends."""
    if db.using_supabase():
        return [(r.get("username"), r.get("url"), r.get("status"))
                for r in db._fetch_all(db.USERJOBS_TABLE,
                                       {"select": "username,url,status"})
                if r.get("url") and r.get("status")]
    out = []
    for user, per_user in (db._load_json(db.USER_JOBS_FILE) or {}).items():
        for url, st in (per_user or {}).items():
            if url and st:
                out.append((user, url, st))
    return out


def _score(row):
    """match_score as an int, or None. Supabase returns it as a number, the local CSV
    backend as a string — coerce so the merge behaves the same on both."""
    v = row.get("match_score")
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _pick_keeper(canon, rows):
    """The row that survives. Prefer the one already AT the canonical URL so no row has to
    be recreated; otherwise the richest row, which we then move to the canonical URL."""
    at_canon = [r for r in rows if r["url"] == canon]
    if at_canon:
        return at_canon[0]
    return max(rows, key=lambda r: (_score(r) or 0,
                                    len(r.get("found_date") or ""),
                                    len(r.get("title") or "")))


def _merge_fields(keeper, losers):
    """Fields to patch onto the keeper: take the best value found anywhere in the group.
    Only returns keys whose value actually improves on the keeper's."""
    patch = {}
    group = [keeper] + losers

    # match_score: the highest is the one scored against the fullest JD.
    scores = [s for s in (_score(r) for r in group) if s is not None]
    if scores and max(scores) != _score(keeper):
        patch["match_score"] = max(scores)

    # found_date is the real posting date -> the EARLIEST non-empty is the true one.
    dates = sorted(d for d in ((r.get("found_date") or "").strip() for r in group) if d)
    if dates and dates[0] != (keeper.get("found_date") or "").strip():
        patch["found_date"] = dates[0]

    # first_seen is when the posting entered OUR database, so the same rule applies: the
    # earliest sighting anywhere in the group is when we really first saw this job.
    seen = sorted(d for d in ((str(r.get("first_seen") or "")).strip() for r in group) if d)
    if seen and seen[0] != str(keeper.get("first_seen") or "").strip():
        patch["first_seen"] = seen[0]

    # Everything else: fill only what the keeper is missing.
    for field in ("title", "company", "location", "sponsors_h1b",
                  "posted_verified", "posted_confidence", "status"):
        if not (keeper.get(field) or ""):
            for r in losers:
                if r.get(field):
                    patch[field] = r[field]
                    break
    return patch


def main(argv):
    apply = "--apply" in argv
    assume_yes = "--yes" in argv

    # Unlike db.load_jobs() this names every column, so it 400s on a database where the newest
    # migration hasn't been run. Retry without first_seen rather than dying on the un-migrated
    # case — the merge below simply won't have a date to carry forward.
    try:
        fetched = db._fetch_all(db.TABLE, {"select": ",".join(db.FIELDS)})
    except Exception:
        cols = [c for c in db.FIELDS if c != "first_seen"]
        fetched = db._fetch_all(db.TABLE, {"select": ",".join(cols)})
    rows = [r for r in fetched if r.get("url")]
    with_jd = db.urls_with_jd()
    statuses = _user_job_rows()
    flagged_by_url = collections.defaultdict(list)
    for user, url, st in statuses:
        flagged_by_url[url].append((user, st))

    print("jobs rows: %d | rows with a JD: %d | user_jobs rows: %d"
          % (len(rows), len(with_jd), len(statuses)))
    print("backend: %s\n" % db.backend_name())

    groups = collections.defaultdict(list)
    for r in rows:
        groups[scraper.canonical_url(r["url"])].append(r)
    dupes = {k: v for k, v in groups.items() if len(v) > 1}

    if not dupes:
        print("No duplicate groups found — nothing to do.")
        return 0

    plan, refused = [], []
    for canon, members in sorted(dupes.items()):
        titles = {(m.get("title") or "").strip().lower() for m in members}
        companies = {(m.get("company") or "").strip().lower() for m in members}
        if len(titles) > 1 or len(companies) > 1:
            refused.append((canon, members))
            continue
        keeper = _pick_keeper(canon, members)
        losers = [m for m in members if m["url"] != keeper["url"]]
        plan.append((canon, keeper, losers))

    for canon, members in refused:
        print("REFUSED (rows disagree on title/company — NOT merging): %s" % canon)
        for m in members:
            print("    %-90s | %s" % (m["url"][:90], (m.get("title") or "")[:40]))
    if refused:
        print()

    moves = conflicts = 0
    for canon, keeper, losers in plan:
        print("GROUP %s" % canon)
        print("  KEEP   %s%s" % (keeper["url"],
                                 "" if keeper["url"] == canon else "   -> moves to canonical URL"))
        patch = _merge_fields(keeper, losers)
        if patch:
            print("  MERGE  %s" % patch)
        for l in losers:
            print("  DELETE %s" % l["url"])
            for user, st in flagged_by_url.get(l["url"], []):
                existing = dict(flagged_by_url.get(keeper["url"], [])).get(user)
                if existing and existing != st:
                    conflicts += 1
                    print("    CONFLICT user=%s keeps '%s' on the survivor (dropping '%s')"
                          % (user, existing, st))
                elif existing:
                    print("    status user=%s '%s' already on the survivor" % (user, st))
                else:
                    moves += 1
                    print("    MOVE STATUS user=%s '%s' -> the survivor" % (user, st))
            if l["url"] in with_jd and keeper["url"] not in with_jd:
                print("    CARRY JD from this row (survivor has none)")

    print("\n%d group(s) to merge, %d row(s) to delete, %d status move(s), "
          "%d conflict(s), %d group(s) refused."
          % (len(plan), sum(len(l) for _, _, l in plan), moves, conflicts, len(refused)))

    if not apply:
        print("\nDRY RUN — nothing was written. Re-run with --apply to perform the merge.")
        return 0
    if not plan:
        print("\nNothing to apply.")
        return 0
    if not assume_yes:
        try:
            if input("\nApply these changes? [y/N] ").strip().lower() not in ("y", "yes"):
                print("Aborted; nothing written.")
                return 1
        except EOFError:            # non-interactive shell: require the explicit flag
            print("Not a TTY — re-run with --yes to confirm non-interactively.")
            return 1

    done = 0
    for canon, keeper, losers in plan:
        try:
            target = canon
            patch = _merge_fields(keeper, losers)

            # 1. JD first: carry one over if the survivor has none.
            jd = ""
            if keeper["url"] not in with_jd:
                for l in losers + [keeper]:
                    if l["url"] in with_jd:
                        jd = db.get_job_jd(l["url"])
                        break

            # 2. Write the survivor at the canonical URL. update_job_fields upserts on url,
            #    so this patches an existing row or creates the renamed one.
            row = {"url": target}
            if target != keeper["url"]:              # recreating the row: carry everything
                for f in db.FIELDS:
                    if f != "url" and keeper.get(f) not in (None, ""):
                        row[f] = keeper[f]
            row.update(patch)
            if len(row) > 1:
                db.update_job_fields([row])
            if jd:
                db.update_jds({target: jd})

            # 3. Move per-user status BEFORE deleting anything.
            on_target = dict(flagged_by_url.get(target, []))
            for l in losers + ([keeper] if target != keeper["url"] else []):
                for user, st in flagged_by_url.get(l["url"], []):
                    if not on_target.get(user):
                        db.set_user_status(user, target, st)
                        on_target[user] = st
                    db.set_user_status(user, l["url"], "")      # clear the stale row

            # 4. Only now drop the losing job rows.
            drop = [l["url"] for l in losers]
            if target != keeper["url"]:
                drop.append(keeper["url"])
            db.delete_urls(drop)
            done += 1
            print("merged %s" % target)
        except Exception as e:
            print("FAILED on %s: %s" % (canon, str(e)[:200]))
            print("  (nothing else in this group was deleted; safe to re-run)")

    print("\nMerged %d of %d group(s)." % (done, len(plan)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
