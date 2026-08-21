#!/usr/bin/env python3
"""Undo the applications that were logged by opening a posting rather than by applying to one.

Until 2026-08-21 the feed's Apply link called /api/action with status="applied" from its click
handler (static/app.js, "clicking Apply auto-logs it"). So OPENING a job counted as applying to
it, and the tracker reported 129 applications that had never been made. The click now parks the
job and static/applyask.js asks on return; nothing is written unless the answer is yes.

That fixes new clicks. This removes the ones already on record, in both places they landed:

    applications   the tracker row _autolog_application wrote
    user_jobs      the status="applied" flag that triggered it

WHICH ROWS. _autolog_application (web.py) writes applied_date == the creation day and no notes.
A row the user typed on /applications almost always differs on one of those -- they backdate the
date, or write a note, or both -- so that pair is the signature, and it is the same test
/admin/usage already uses for its "auto-logged from the feed" count. Rows that fail it are left
alone, which is the safe direction: a real application wrongly deleted cannot be recovered, while
a click-through wrongly kept is one row someone can delete by hand.

    python scripts/reset_autologged_applies.py                # dry run, every user
    python scripts/reset_autologged_applies.py --user kunal   # dry run, one user
    python scripts/reset_autologged_applies.py --apply        # actually delete
    python scripts/reset_autologged_applies.py --keep-flags   # tracker only, leave user_jobs

Saved and Hidden are never touched. Neither is any application that carries a note, a
non-default status (interview, offer, rejected...), or a hand-set date.
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import db


def looks_autologged(a):
    """The _autolog_application signature: applied on its creation day, with nothing typed.

    Status is checked too. _autolog_application writes "applied" and only "applied", so a row
    that has since moved to interview or offer is one the user has been maintaining by hand --
    deleting that would throw away the most valuable data in the table.
    """
    created = str(a.get("created_at") or "")
    applied = str(a.get("applied_date") or "")
    if not created or not applied or applied[:10] != created[:10]:
        return False
    if (a.get("notes") or "").strip():
        return False
    return (a.get("status") or "applied").strip() in ("", "applied")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="just this account (default: every account)")
    ap.add_argument("--apply", action="store_true", help="actually write; default is a dry run")
    ap.add_argument("--keep-flags", action="store_true",
                    help="delete the tracker rows but leave user_jobs.status='applied' alone")
    args = ap.parse_args()

    print("database: %s" % db.backend_name())

    users = [args.user] if args.user else [
        u.get("username") for u in (db.list_users() or []) if u.get("username")]
    if not users:
        print("no accounts found -- nothing to do.")
        return 0

    total_apps = total_flags = 0
    kept_reasons = collections.Counter()

    for user in users:
        try:
            apps = db.list_applications(user) or []
        except Exception as e:
            print("  %s: could not read applications (%s)" % (user, str(e)[:80]))
            continue

        doomed = [a for a in apps if looks_autologged(a)]
        for a in apps:
            if a in doomed:
                continue
            if (a.get("notes") or "").strip():
                kept_reasons["has a note"] += 1
            elif (a.get("status") or "applied").strip() not in ("", "applied"):
                kept_reasons["status moved on"] += 1
            else:
                kept_reasons["date was set by hand"] += 1

        print("\n%s: %d application(s), %d look auto-logged" % (user, len(apps), len(doomed)))
        for a in doomed[:10]:
            print("    - %-28s %s" % ((a.get("company") or "?")[:28],
                                      (a.get("title") or "")[:44]))
        if len(doomed) > 10:
            print("    ...and %d more" % (len(doomed) - 10))

        urls = {(a.get("url") or "").strip() for a in doomed}
        urls.discard("")
        # Only flags whose tracker row is going away, and only where the flag actually says
        # applied: a job the user later Saved or Hid has a different flag and keeps it.
        try:
            statuses = db.get_user_statuses(user) or {}
        except Exception:
            statuses = {}
        flags = [u for u in urls if statuses.get(u) == "applied"]
        print("    and %d user_jobs row(s) still flagged applied" % len(flags))

        total_apps += len(doomed)
        total_flags += len(flags)

        if not args.apply:
            continue
        for a in doomed:
            try:
                db.delete_application(user, a.get("id"))
            except Exception as e:
                print("    FAILED to delete %s: %s" % (a.get("id"), str(e)[:80]))
        if not args.keep_flags:
            for u in flags:
                try:
                    db.set_user_status(user, u, "")     # "" DELETEs the row, per db.py
                except Exception as e:
                    print("    FAILED to clear %s: %s" % (u[:50], str(e)[:80]))

    print("\n%s" % ("-" * 66))
    print("%s %d tracker row(s) and %d applied flag(s)"
          % ("DELETED" if args.apply else "WOULD DELETE", total_apps, total_flags))
    if kept_reasons:
        print("kept: %s" % ", ".join("%d %s" % (n, k) for k, n in kept_reasons.most_common()))
    if not args.apply:
        print("\nDry run. Re-run with --apply to write.")
    else:
        # The feed's Applied badge is served from a 60-second cache of user_jobs, and the admin
        # panel from a 5-minute one, so the numbers on screen lag this by that much.
        print("\nDone. /admin/usage and the feed's Applied count refresh within ~5 minutes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
