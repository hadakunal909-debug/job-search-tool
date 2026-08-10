"""
auth.py — password hashing for the multi-user login.

No external dependencies: uses PBKDF2-HMAC-SHA256 from the standard library, so it
installs cleanly everywhere (incl. Python 3.14 / Render). Passwords are NEVER stored
in plain text — only a salted hash like:

    pbkdf2_sha256$200000$<salt-b64>$<hash-b64>

Verification is constant-time (hmac.compare_digest).
"""
import os
import hmac
import base64
import hashlib

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 200_000

# Minimum password length. LENGTH ONLY — no required digit, symbol or mixed case: composition
# rules reliably produce "Password1!" instead of something long, which is why NIST dropped them
# (SP 800-63B). Lowered from the 12 the admin UI used to enforce on its own.
#
# It lives HERE rather than in web.py because manage_users.py creates accounts too and could not
# see that constant — so the CLI enforced nothing at all and would happily set a one-character
# password on an account the web UI would have refused to create.
MIN_PASSWORD_LEN = 6


def password_problem(password):
    """Why this password can't be used, or "" if it's fine."""
    if len(password or "") < MIN_PASSWORD_LEN:
        return "Password must be at least %d characters." % MIN_PASSWORD_LEN
    return ""


def hash_password(password: str, iterations: int = _ITERATIONS) -> str:
    """Return a self-describing salted hash string for `password`."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "%s$%d$%s$%s" % (_ALGO, iterations,
                            base64.b64encode(salt).decode("ascii"),
                            base64.b64encode(dk).decode("ascii"))


def verify_password(password: str, stored: str) -> bool:
    """True if `password` matches the stored hash. Safe against bad/empty input."""
    try:
        algo, iters, salt_b64, hash_b64 = (stored or "").split("$")
        if algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False
