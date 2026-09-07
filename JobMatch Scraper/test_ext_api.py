"""
test_ext_api.py — the extension and the app have to agree about what exists.

Run it directly
    python test_ext_api.py
or via pytest (functions are named test_*).

WHY THIS EXISTS, and what it is NOT. An audit on 2026-09-07 went looking for missing input
validation on these routes and found almost none missing: every route but /api/ext/version
checks a token (that one is unauthenticated on purpose, and its docstring says why), bulk_jobs
caps its list at 2,000 and rejects non-dict items, and Flask's MAX_CONTENT_LENGTH rejects a body
over 6 MB before any handler sees it. The first pass said otherwise and was wrong -- it grepped a
25-line window that only covered docstrings. This file therefore pins what is actually true,
because a wrong belief about validation is worse than none.

What the audit DID find is that nothing checked the CONTRACT. The extension is side-loaded and
ships separately from the app, so the two drift silently: a renamed route 404s in a popup nobody
is watching, and the failure looks like "the extension is broken" rather than "these two
disagree". That is the same coupling scripts/feed_parity.py exists for, and this is its twin.

Offline: reads source text and the URL map. No database, no network.
"""
import io
import json
import os
import re
import sys

APP = os.path.dirname(os.path.abspath(__file__))
EXT = os.path.join(APP, "extension")
FAILED = []


def _check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name +
          (("  -- " + detail) if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def _web():
    return io.open(os.path.join(APP, "web.py"), encoding="utf-8").read()


def _ext_sources():
    out = {}
    for fn in sorted(os.listdir(EXT)):
        if fn.endswith((".js", ".html")):
            out[fn] = io.open(os.path.join(EXT, fn), encoding="utf-8").read()
    return out


def _routes(web):
    return set(re.findall(r'@app\.route\("(/api/ext/[a-z_]+)"', web))


def _called(sources):
    hits = set()
    for text in sources.values():
        hits |= set(re.findall(r"/api/ext/[a-z_]+", text))
    return hits


def test_every_endpoint_the_extension_calls_exists():
    """THE ONE THAT MATTERS. The extension ships separately -- side-loaded unpacked, with no
    update_url, as EXT_MIN_VERSION's own comment explains -- so a route renamed in the app is a
    404 in a popup nobody is watching, and it reads as "the extension is broken"."""
    routes, called = _routes(_web()), _called(_ext_sources())
    missing = sorted(called - routes)
    _check("every /api/ext/* the extension calls is a real route",
           not missing, "MISSING: %s" % missing)
    # The other direction is information, not a failure: the app may serve a route the
    # extension has not started using yet.
    unused = sorted(routes - called)
    print("       (app serves %d, extension calls %d; unused by the extension: %s)"
          % (len(routes), len(called), unused or "none"))


def test_the_only_unauthenticated_route_is_the_one_that_must_be():
    """A stale extension may be stale precisely BECAUSE its token contract moved, so requiring a
    valid token to discover that would hide the message from the installs that most need it.
    That is the whole argument, and it applies to exactly one route."""
    web = _web()
    routes = sorted(_routes(web))
    lines = web.split("\n")
    idx = [(i, re.search(r'@app\.route\("(/api/ext/[a-z_]+)"', l).group(1))
           for i, l in enumerate(lines)
           if re.search(r'@app\.route\("/api/ext/[a-z_]+"', l)]
    open_routes = []
    for n, (i, name) in enumerate(idx):
        end = idx[n + 1][0] if n + 1 < len(idx) else len(lines)
        if "_ext_user(" not in "\n".join(lines[i:end]):
            open_routes.append(name)
    _check("exactly one ext route is unauthenticated",
           open_routes == ["/api/ext/version"], repr(open_routes))
    _check("...and %d routes exist in total" % len(routes), len(routes) >= 15, str(len(routes)))


def test_the_version_parser_survives_junk():
    """/api/ext/version is open, so _vtuple is the one ext parser that sees unfiltered input."""
    sys.path.insert(0, APP)
    os.environ.setdefault("EV_OFF", "1")
    os.environ.setdefault("APP_SECRET", "test-ext-api-not-a-session-key")
    import web
    for junk in ("", None, "abc", "...", "1.2.3.4.5.6", "-1", "1.-2", "v1.36.0",
                 "9" * 200, "1" * 50 + ".2"):
        try:
            got = web._vtuple(junk)
            ok = isinstance(got, tuple) and len(got) == 4
        except Exception as e:
            ok = False
            got = repr(e)
        _check("_vtuple(%r) returns a 4-tuple" % (junk if len(str(junk)) < 20 else "<long>",),
               ok, repr(got))
    _check("1.9.0 sorts BELOW 1.35.0 (string compare gets this backwards)",
           web._vtuple("1.9.0") < web._vtuple("1.35.0"))


def test_bulk_jobs_bounds_what_it_writes():
    """It writes into the SHARED jobs feed on nothing but a token, so its two bounds are the
    ones worth pinning: a cap on how many rows one call can carry, and a type check per row."""
    web = _web()
    i = web.index('@app.route("/api/ext/bulk_jobs"')
    j = web.index("@app.route", i + 10)
    body = web[i:j]
    _check("bulk_jobs caps the list it iterates", bool(re.search(r"jobs\[:\d+\]", body)),
           "no jobs[:N] slice")
    _check("...and rejects a non-dict item", "isinstance(j, dict)" in body)
    _check("...and refuses a payload that is not a list",
           "isinstance(jobs, list)" in body)


def test_the_manifest_and_the_app_agree_on_the_version():
    """The popup nags when its version is below EXT_MIN_VERSION. If the repo ships an extension
    OLDER than the app demands, every install nags on every open with no way to fix it."""
    man = json.load(io.open(os.path.join(EXT, "manifest.json"), encoding="utf-8"))
    m = re.search(r'EXT_MIN_VERSION = "([0-9.]+)"', _web())
    _check("EXT_MIN_VERSION is declared", bool(m))
    if m:
        sys.path.insert(0, APP)
        import web
        _check("the shipped manifest is not older than the app demands",
               web._vtuple(man["version"]) >= web._vtuple(m.group(1)),
               "manifest %s < required %s" % (man["version"], m.group(1)))


def test_the_manifest_declares_every_script_it_ships():
    """A file in extension/ that no manifest entry names is dead weight that still gets read by
    whoever comes next; a manifest entry with no file is a load error at install time."""
    man = json.load(io.open(os.path.join(EXT, "manifest.json"), encoding="utf-8"))
    named = set()
    named.add((man.get("action") or {}).get("default_popup"))
    named.add((man.get("background") or {}).get("service_worker"))
    for cs in man.get("content_scripts") or []:
        named |= set(cs.get("js") or [])
    for war in man.get("web_accessible_resources") or []:
        named |= set(war.get("resources") or [])
    named.discard(None)
    on_disk = {f for f in os.listdir(EXT) if f.endswith(".js")}
    # popup.js is loaded by popup.html, not by the manifest.
    html = io.open(os.path.join(EXT, "popup.html"), encoding="utf-8").read()
    named |= set(re.findall(r'src="([^"]+\.js)"', html))
    missing = sorted(f for f in named if f.endswith(".js") and f not in on_disk)
    orphan = sorted(on_disk - named)
    _check("every script the manifest names exists", not missing, repr(missing))
    _check("every .js on disk is reachable", not orphan, "unreferenced: %s" % orphan)


def main():
    print("extension <-> app contract")
    for fn in (test_every_endpoint_the_extension_calls_exists,
               test_the_only_unauthenticated_route_is_the_one_that_must_be,
               test_the_version_parser_survives_junk,
               test_bulk_jobs_bounds_what_it_writes,
               test_the_manifest_and_the_app_agree_on_the_version,
               test_the_manifest_declares_every_script_it_ships):
        fn()
    print("\n%s" % ("FAILED: " + ", ".join(FAILED) if FAILED else "all checks passed"))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
