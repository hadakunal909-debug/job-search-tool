#!/usr/bin/env python3
"""
manage_users.py — create/manage the people who can log into the app.

Accounts are admin-created (no public sign-up), so you run this yourself. It writes
to the same storage the app uses (Supabase if configured, else local users.json).

    python manage_users.py add    <username> <password>     # create a user
    python manage_users.py list                             # list users
    python manage_users.py passwd <username> <newpassword>  # reset a password
    python manage_users.py remove <username>                # delete a user (+ their saved jobs)
    python manage_users.py resume <username> <file.txt>     # set a user's resume from a text file

First-time setup: run the SQL in USERS_SETUP.md in your Supabase SQL editor once
(creates the `users` + `user_jobs` tables and the `jobs.jd` column).
"""
import sys

# Force UTF-8 stdout (Windows cp1252 consoles crash on accents in names).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import auth
import db


def _usage():
    print(__doc__)
    sys.exit(1)


def main(argv):
    if not argv:
        _usage()
    cmd = argv[0].lower()

    if cmd == "add" and len(argv) == 3:
        username, password = argv[1], argv[2]
        if db.get_user(username):
            print("User '%s' already exists. Use 'passwd' to change the password." % username)
            return
        ok, msg = db.create_user(username, auth.hash_password(password))
        print("Created user '%s'. They can now log in." % username if ok else msg)

    elif cmd == "passwd" and len(argv) == 3:
        username, password = argv[1], argv[2]
        if not db.get_user(username):
            print("No such user: %s" % username)
            return
        db.set_user_password(username, auth.hash_password(password))
        print("Password updated for '%s'." % username)

    elif cmd == "remove" and len(argv) == 2:
        username = argv[1]
        removed = db.delete_user(username)
        print("Removed user '%s'. Rows deleted: %s"
              % (username, ", ".join("%s=%d" % kv for kv in sorted(removed.items())) or "none"))

    elif cmd == "resume" and len(argv) == 3:
        username, path = argv[1], argv[2]
        if not db.get_user(username):
            print("No such user: %s" % username)
            return
        with open(path, encoding="utf-8") as f:
            db.set_user_resume(username, f.read())
        print("Set resume for '%s' from %s." % (username, path))

    elif cmd == "list":
        users = db.list_users()
        backend = "Supabase" if db.using_supabase() else "local users.json"
        print("%d user(s) [%s]:" % (len(users), backend))
        for u in users:
            print("  - %s   (created %s)" % (u.get("username", "?"), u.get("created_at", "")))

    else:
        _usage()


if __name__ == "__main__":
    main(sys.argv[1:])
