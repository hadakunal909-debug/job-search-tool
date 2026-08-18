#!/usr/bin/env python3
"""probe_db_proxy.py — why is /api/db answering "bad signature"?

Every scrape run since the proxy went live has died on its first read with

    pgrest.PgRestError: HTTP 401: {"error":"bad signature"}

and "bad signature" has three causes that look identical from the client: the two sides hold
DIFFERENT SECRETS, or they hold the same one and something between them EDITED THE BODY before
the server hashed it, or the signature headers never arrived. Guessing between them by
re-pasting the GitHub secret and waiting for the next scheduled run is a 24-hour feedback loop
for a one-byte question. This asks the live endpoint directly.

WHAT MAKES THIS ANSWERABLE AT ALL is the order of checks in dbproxy.handle/verify. The server
never says "bad signature" for anything except a hash mismatch:

    server secret unset      -> 503 proxy not configured
    ts/sig headers absent    -> 401 missing signature
    HASH MISMATCH            -> 401 bad signature          <- the only one we are chasing
    hash OK, clock off       -> 401 stale request (Ns skew)
    hash OK, no PG_DSN       -> 503 proxy has no local database
    hash OK, table refused   -> 403 table not allowed

So ANY reply other than "bad signature"/"missing signature" is proof the secret just used is the
right one. That turns a yes/no into a bisection, and it is why the probes below deliberately try
to provoke the *later* errors.

Read-only by construction: every envelope it sends is a GET of one column of one row, and the
table allowlist plus pgrest.build would refuse anything else anyway. It writes nothing.

    python scripts/probe_db_proxy.py                  # full diagnosis against DB_PROXY_URL
    python scripts/probe_db_proxy.py --fingerprint    # print the secret's digest and stop
    python scripts/probe_db_proxy.py --url https://host/api/db --secret-env OTHER_VAR

THE SECRET IS NEVER PRINTED. Where the value itself would be useful it prints sha256[:12]
instead, which is what you compare between two machines that each believe they have the right
one. GitHub secrets are write-only, so to get the runner's digest add a temporary step to
scrape.yml — the last section of the output prints one ready to paste.
"""
import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import dbproxy

DEFAULT_URL = "https://stemjobs1.astrochakra.co/api/db"


def fp(secret):
    """A comparable, non-reversible stand-in for the secret."""
    if not secret:
        return "(empty)"
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def envelope(table="jobs", params=None):
    """The exact bytes dbproxy.Session would send. Compact separators matter: the signature is
    over these bytes, so anything that re-serializes the JSON in between breaks it."""
    return json.dumps({"method": "GET", "table": table,
                       "params": params or {"select": "url", "limit": "1"},
                       "prefer": "", "body": None},
                      separators=(",", ":")).encode("utf-8")


def post(url, raw, secret=None, ts=None, send_headers=True, timeout=30):
    """One signed (or deliberately unsigned) request. Returns (status, reason, seconds)."""
    import requests
    ts = ts if ts is not None else "%d" % int(time.time())
    headers = {"Content-Type": "application/json"}
    if send_headers:
        headers["X-DB-Ts"] = ts
        headers["X-DB-Sig"] = dbproxy.sign(secret or "", ts, raw)
    t0 = time.time()
    try:
        r = requests.post(url, data=raw, headers=headers, timeout=timeout)
    except Exception as e:
        return None, "request failed: %s" % str(e)[:200], time.time() - t0
    took = time.time() - t0
    try:
        payload = r.json()
        reason = payload.get("error") or payload.get("message") or json.dumps(payload)[:200]
    except Exception:
        reason = (r.text or "")[:200].replace("\n", " ")
    return r.status_code, reason, took


# Refusals handle() issues BEFORE it ever calls verify(). They say nothing about the secret, so
# reading them as "the signature was fine" would report a dead endpoint as a working one.
PRE_VERIFY = ("proxy not configured", "body too large")


def accepted(status, reason):
    """Did the SIGNATURE verify? Everything past verify() proves the secret matched — including
    the failures, which is the whole trick. See the table at the top of this file."""
    if status is None:
        return False
    if any(p in (reason or "") for p in PRE_VERIFY):
        return False
    return not (status == 401 and "signature" in (reason or ""))


def variants(secret):
    """Ways a correct secret gets stored wrong.

    db.py's _load_env_file strips whitespace and surrounding quotes off every .env value;
    GitHub Actions does NOT strip its secrets. So a value pasted into GitHub with a trailing
    newline signs WITH the newline while the cPanel side verifies without it, and the two
    disagree while looking identical on screen. That asymmetry is the likeliest cause here.
    """
    out, seen = [], set()
    for name, v in (("as read", secret),
                    ("whitespace stripped", secret.strip()),
                    ("trailing newline removed", secret.rstrip("\r\n")),
                    ("leading whitespace stripped", secret.lstrip()),
                    ("CR removed (CRLF paste)", secret.replace("\r", "")),
                    ("db.py .env normalisation", secret.strip().strip('"').strip("'"))):
        if v and v not in seen:
            seen.add(v)
            out.append((name, v))
    return out


CI_STEP = """
      - name: TEMP - fingerprint the proxy secret
        env:
          DB_PROXY_SECRET: ${{ secrets.DB_PROXY_SECRET }}
        run: python -c "import hashlib,os; s=os.environ['DB_PROXY_SECRET']; print('len', len(s), 'sha256[:12]', hashlib.sha256(s.encode()).hexdigest()[:12])"
"""


def read_secret(var):
    """RAW. Deliberately not through db._load_env_file, which strips: this script exists to
    find whitespace, and a loader that removed it first would hide the answer."""
    v = os.environ.get(var)
    if v is not None:
        return v, "environment (%s)" % var
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (".env", os.path.join(here, "..", ".env")):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip().startswith(var + "="):
                    return line.split("=", 1)[1].rstrip("\n"), "%s (raw line, unstripped)" % path
    return None, None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("DB_PROXY_URL") or DEFAULT_URL)
    ap.add_argument("--secret-env", default="DB_PROXY_SECRET",
                    help="environment variable holding the secret (default DB_PROXY_SECRET)")
    ap.add_argument("--fingerprint", action="store_true",
                    help="print the digest of the secret and exit; sends nothing")
    ap.add_argument("--timeout", type=float, default=30)
    args = ap.parse_args()

    secret, source = read_secret(args.secret_env)
    if not secret:
        print("No %s in the environment or .env. Set it to the value the cPanel app uses:" % args.secret_env)
        print("    export %s='...'    (or run this on the cPanel box, whose .env has it)" % args.secret_env)
        return 2

    print("secret source : %s" % source)
    print("length        : %d bytes" % len(secret))
    print("digest        : sha256[:12] = %s" % fp(secret))
    odd = []
    if secret != secret.strip():
        odd.append("SURROUNDING WHITESPACE (ends %r)" % secret[-3:])
    if "\r" in secret:
        odd.append("carriage return present")
    if secret.strip() != secret.strip().strip('"').strip("'"):
        odd.append("surrounding quotes")
    print("shape         : %s" % ("; ".join(odd) if odd else "clean — no stray whitespace or quotes"))
    if args.fingerprint:
        return 0
    print("endpoint      : %s" % args.url)
    print()

    # SELF-TEST FIRST. If sign/verify disagree here the fault is this script or a bad import,
    # and every result below would be noise.
    raw = envelope()
    ts = "%d" % int(time.time())
    ok, why = dbproxy.verify(secret, ts, raw, dbproxy.sign(secret, ts, raw))
    print("[0] local sign/verify round-trip ......... %s"
          % ("ok" if ok else "BROKEN (%s) — stop, the fault is local" % why))
    if not ok:
        return 1

    # 1. THE REAL REQUEST, through the client the scraper actually uses, so a client-side
    #    difference (header names, body shape) shows up as itself rather than as a mismatch.
    t0 = time.time()
    try:
        r = dbproxy.Session(args.url, secret).get(
            "proxy://x/rest/v1/jobs", params={"select": "url", "limit": "1"},
            headers={"Prefer": ""}, timeout=args.timeout)
        status, reason = r.status_code, (r.text or "")[:200]
    except Exception as e:
        status, reason = None, "client raised: %s" % str(e)[:200]
    print("[1] dbproxy.Session GET jobs ............. HTTP %s  %s  (%.1fs)"
          % (status, reason, time.time() - t0))
    live = accepted(status, reason)

    # 2. Did the signature headers survive the hop? A CDN or mod_security that drops unknown
    #    X- headers turns a perfectly signed request into "missing signature", not "bad".
    #    Only worth asking when something was refused; both answers below are decisive.
    if not live:
        st, rs, _ = post(args.url, raw, secret, timeout=args.timeout)
        st_bare, rs_bare, _ = post(args.url, raw, send_headers=False, timeout=args.timeout)
        print("[2] do the headers arrive ................ signed: HTTP %s %s | unsigned: HTTP %s %s"
              % (st, rs, st_bare, rs_bare))
        if st == 401 and "missing signature" in (rs or ""):
            print()
            print("VERDICT: we SENT X-DB-Ts and X-DB-Sig and the server saw neither. Something")
            print("         between the caller and Flask is stripping them. The secret is fine.")
            return 1
        if st_bare == 503 and "not configured" in (rs_bare or ""):
            print()
            print("VERDICT: the server has no DB_PROXY_SECRET set at all — it refuses every caller,")
            print("         correctly signed or not. Set it on the cPanel app first.")
            return 1

    # 3. THE SECRET VARIANTS. Each is the same value stored a slightly different way. Skipped
    #    when the value as read already works — there is nothing left to bisect.
    winner = None
    if not live:
        print("[3] secret variants:")
        for name, cand in variants(secret):
            st, rs, _ = post(args.url, raw, cand, timeout=args.timeout)
            good = accepted(st, rs)
            print("      %-30s %-8s (HTTP %s %s)"
                  % (name, "ACCEPTED" if good else "rejected", st, rs))
            if good and winner is None:
                winner = (name, cand)

    # 4. BODY INTEGRITY, and it runs even when probe [1] came back 200.
    #
    #    That early exit was this script's own first bug. Probe [1] sends a 93-byte GET, which
    #    is the smallest request the scraper ever makes; the ones that matter are PATCHes
    #    carrying job descriptions, orders of magnitude larger. A buffer or re-encoder that
    #    only touches bodies over some threshold would let probe [1] pass and still break every
    #    real run — so the script would have printed "the endpoint accepts this secret" about a
    #    proxy that cannot carry a single useful write. Four shapes, one secret: size and
    #    charset are varied, nothing else.
    good_secret = winner[1] if winner else secret
    print("[4] body integrity (one secret, different bytes):")
    shapes = [("minimal ASCII", envelope(params={"limit": "1"})),
              ("typical select", envelope()),
              # Long but VALID: pgrest.build runs after the signature check, so a body that
              # merely fails validation would report 400 and be scored as "signature ok" —
              # true, but it buries the size signal under an unrelated error message.
              ("long params", envelope(params={"select": "url", "limit": "1",
                                               "company": "eq." + "A" * 3000})),
              ("non-ASCII value", envelope(params={"select": "url", "limit": "1",
                                                   "company": "eq.Zürich Café"}))]
    passed, failed = [], []
    for name, body in shapes:
        st, rs, _ = post(args.url, body, good_secret, timeout=args.timeout)
        good = accepted(st, rs)
        (passed if good else failed).append("%s (%d bytes)" % (name, len(body)))
        print("      %-16s %6d bytes  %-8s (HTTP %s %s)"
              % (name, len(body), "ACCEPTED" if good else "rejected", st, rs))

    if passed and failed:
        print()
        print("VERDICT: one secret, accepted for %s and refused for %s." % (passed[0], failed[0]))
        print("         That is not an auth failure — the body is being altered in transit after")
        print("         we hashed it. Look at whatever sits in front of Flask: proxy buffering,")
        print("         gzip, charset rewriting. Note the scraper's real writes are the LARGE")
        print("         ones, so a small-body success proves nothing on its own.")
        return 1

    if winner:
        print()
        print("VERDICT: the secret is RIGHT but stored wrong — '%s' is the form the" % winner[0])
        print("         server accepts (digest %s). Set the GitHub secret to exactly" % fp(winner[1]))
        print("         that value. GitHub does not strip its secrets; db.py's .env loader does,")
        print("         and that asymmetry is the entire bug.")
        return 1

    if live:
        print()
        print("VERDICT: the endpoint ACCEPTS this secret, at every body size. CI is therefore")
        print("         sending a different value than this machine just used. Re-set the GitHub")
        print("         secret, and use the step below to confirm the digests match rather than")
        print("         waiting on a scheduled run:")
        print(CI_STEP)
        print("    This machine's: len %d  sha256[:12] %s" % (len(secret), fp(secret)))
        return 0

    # 5. Nothing above explained it: the two sides hold genuinely different values.
    print()
    print("VERDICT: every form of this secret is refused, for every body shape, and the headers")
    print("         arrive intact. The cPanel app and this machine hold DIFFERENT secrets.")
    print("         Compare digests, not values — add this step to scrape.yml, run the workflow")
    print("         once, then delete it:")
    print(CI_STEP)
    print("    This machine's: len %d  sha256[:12] %s" % (len(secret), fp(secret)))
    print("    Same digest but still failing means the value is fine and the bytes are not.")
    print("    A different digest means re-paste the GitHub secret. Neither form leaks it.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
