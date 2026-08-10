"""
test_password_rule.py — one password rule, enforced everywhere an account can be made.

There are four ways to set a password: `manage_users.py add`, `manage_users.py passwd`, and the
admin UI's create + reset. The CLI pair enforced NOTHING — it would set a one-character password
on an account the web UI would have refused, and that account then logs in like any other. A
rule only one of two doors respects is not a rule.

Run it directly
    python test_password_rule.py
or via pytest.
"""
import inspect

import auth
import manage_users


def test_length_is_the_only_rule():
    """Length, not composition. A required digit or symbol reliably produces Password1! rather
    than something long, which is why NIST dropped composition rules in SP 800-63B."""
    assert auth.MIN_PASSWORD_LEN == 6
    assert auth.password_problem("a" * auth.MIN_PASSWORD_LEN) == ""
    # None of these would pass a composition rule, and all of them are fine here.
    for ok in ("correct horse battery staple", "aaaaaa", "123456", "      ",
               "ünïcodé", "passphrase with spaces"):
        assert auth.password_problem(ok) == "", ok


def test_too_short_is_refused_with_a_usable_message():
    for bad in ("", "a", "12345", "short"):
        msg = auth.password_problem(bad)
        assert msg, repr(bad)
        assert str(auth.MIN_PASSWORD_LEN) in msg, msg


def test_none_and_junk_do_not_raise():
    assert auth.password_problem(None)
    assert auth.password_problem(0 and "")          # falsy non-str


# --- the rule has to reach every door -------------------------------------------------
def test_the_cli_validates_both_of_its_paths():
    src = inspect.getsource(manage_users.main)
    # Both branches must check BEFORE hashing, or a short password still creates the account.
    assert src.count("auth.password_problem") >= 2, "add and passwd both need the check"
    add_i, hash_i = src.index("cmd == \"add\""), src.index("auth.hash_password")
    assert src.index("auth.password_problem", add_i) < hash_i, "checked before hashing"


def test_the_web_admin_shares_the_same_definition():
    import web
    assert web._MIN_PASSWORD == auth.MIN_PASSWORD_LEN, "must not drift from auth"
    for fn in (web.admin_user_create, web.admin_user_password):
        assert "auth.password_problem" in inspect.getsource(fn), fn.__name__


def test_the_signup_form_advertises_the_same_minimum():
    """The HTML minlength must match, or the browser blocks what the server would allow (or
    worse, lets through what it would reject)."""
    import os
    import web
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "templates", "admin_users.html"), encoding="utf-8").read()
    assert 'minlength="{{ min_password }}"' in src
    assert "min_password=_MIN_PASSWORD" in inspect.getsource(web.admin_users)


# --- and the hash itself still works --------------------------------------------------
def test_a_valid_password_round_trips():
    h = auth.hash_password("six123")
    assert auth.verify_password("six123", h)
    assert not auth.verify_password("six124", h)
    assert not auth.verify_password("", h)
    assert h.startswith("pbkdf2_sha256$")
    assert "six123" not in h                        # never stored in the clear


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("ok  -", fn.__name__)
    print("\nAll %d password-rule checks passed." % len(fns))
