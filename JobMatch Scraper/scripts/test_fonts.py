"""The fonts are OURS, and this is what enforces it rather than merely recording it.

Until 2026-09-01 the type came from Google: a render-blocking <link> to fonts.googleapis.com
whose reply pointed at a SECOND host, fonts.gstatic.com, for the binaries. That is two extra DNS
lookups and two extra TLS handshakes standing between a cold visitor and the first pixel -- paid
on the first visit and never on a reload, which is exactly why the app felt fine the second time.

Six woff2 files live in static/fonts/ now and the CSP says font-src 'self'. That last part is the
point: this is the same move img-src 'self' data: already makes for the logos, where the POLICY is
what stops a well-meaning edit quietly reintroducing a third-party request. A CSP alone still
fails silently in one direction though -- it blocks a remote font at runtime, in a browser nobody
is watching, on a page that then renders in Times New Roman. So the checks below assert the whole
chain from both ends.

    python scripts/test_fonts.py

Offline: reads style.css, templates/ and web._CSP_TEMPLATE. No network, no database.
"""
import io
import os
import re
import sys

os.environ.setdefault("EV_OFF", "1")             # analytics reads this at import, once
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, APP)

import web                                       # noqa: E402

FAILS = []


def check(name, cond, extra=""):
    print("  %s %-58s %s" % ("ok " if cond else "FAIL", name, extra))
    if not cond:
        FAILS.append(name)


def read(rel):
    return io.open(os.path.join(APP, rel), encoding="utf-8").read()


CSS = read(os.path.join("static", "style.css"))
BASE = read(os.path.join("templates", "base.html"))

print("=" * 78)
print("self-hosted fonts")
print("=" * 78)

# ---- 1. every @font-face points at a file we actually ship -------------------------------
faces = re.findall(r"@font-face\s*\{(.*?)\}", CSS, re.S)
srcs = []
for body in faces:
    m = re.search(r"url\(\s*([^)\s]+?)\s*\)", body)
    if m:
        srcs.append(m.group(1).strip("'\""))

check("style.css declares @font-face at all", len(faces) >= 2, "%d blocks" % len(faces))

for src in srcs:
    rel = src.split("?")[0].lstrip("/")
    path = os.path.join(APP, rel.replace("/", os.sep))
    exists = os.path.isfile(path)
    magic = b""
    if exists:
        magic = io.open(path, "rb").read(4)
    # wOF2, not woff1 and not a 404 page saved with the right extension. The logo harvest's
    # lesson restated: judge the BYTES, not the name.
    check("ships %s" % rel.split("/")[-1], exists and magic == b"wOF2",
          "%d B" % os.path.getsize(path) if exists else "MISSING")

# ---- 2. the ?v= that earns the immutable Cache-Control -----------------------------------
# web.py grants "public, max-age=604800, immutable" only to /static/dist/** or a URL carrying
# ?v=. A bare /static/fonts/x.woff2 silently falls back to Flask's per-request revalidation.
check("every @font-face src carries ?v= for the immutable header",
      all("?v=" in s for s in srcs),
      "%d of %d" % (sum(1 for s in srcs if "?v=" in s), len(srcs)))

# ---- 3. a preload must match its @font-face src BYTE FOR BYTE ----------------------------
# A preload whose URL differs by even the ?v= downloads the file a SECOND time instead of
# matching the CSS request -- the opposite of the intent, and invisible without a network panel.
preloads = re.findall(r'<link\s+rel="preload"[^>]*>', BASE)
font_preloads = [p for p in preloads if 'as="font"' in p]
check("base.html preloads at least one font", len(font_preloads) >= 1,
      "%d" % len(font_preloads))
for p in font_preloads:
    href = (re.search(r'href="([^"]+)"', p) or [None, ""])[1]
    check("preload %s matches an @font-face src" % href.split("/")[-1], href in srcs,
          "" if href in srcs else "no @font-face uses this exact URL")
    # Fonts are fetched in CORS mode even same-origin; without crossorigin the preload is
    # discarded and the file is fetched again.
    check("preload %s is crossorigin" % href.split("/")[-1], "crossorigin" in p)

# ---- 4. nothing reaches for Google, in the CSP or in any template ------------------------
csp = web._CSP_TEMPLATE % "NONCE"
check("CSP grants font-src 'self'", "font-src 'self'" in csp)
for host in ("fonts.googleapis.com", "fonts.gstatic.com"):
    check("CSP names no %s" % host, host not in csp)

# COMMENTS STRIPPED FIRST, and this suite caught itself failing without it. base.html carries a
# Jinja comment explaining what the Google <link> was and why it went -- that prose is the RECORD
# of the decision and deleting it to satisfy a grep would be exactly backwards. It is the same
# defect scripts/test_card_meta.py fixed on 2026-08-31 (a comment describing a removed feature
# read as the feature) and the one the contrast suite hit when a token name appeared in prose.
def strip_comments(body):
    body = re.sub(r"\{#.*?#\}", "", body, flags=re.S)      # Jinja
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)     # HTML
    return body


tdir = os.path.join(APP, "templates")
offenders = []
for name in sorted(os.listdir(tdir)):
    if not name.endswith(".html"):
        continue
    body = strip_comments(io.open(os.path.join(tdir, name), encoding="utf-8").read())
    if "fonts.googleapis.com" in body or "fonts.gstatic.com" in body:
        offenders.append(name)
check("no template links a Google font host", not offenders, ", ".join(offenders) or "")

# ...and prove that check can still FAIL, so it is not passing merely because the regex ate
# everything. A trip-only assertion that cannot trip is worse than none.
_REAL_LINK = ('{# a comment naming fonts.gstatic.com #}' + chr(10) +
              '<link href="https://fonts.googleapis.com/css2" rel="stylesheet">')
check("that check would still catch a real <link>",
      "fonts.googleapis.com" in strip_comments(_REAL_LINK))
check("...and would ignore one named only inside a comment",
      "fonts.gstatic.com" not in strip_comments(
          '{# fonts.gstatic.com is where the binaries used to come from #}'))

# ---- 5. the fallback stack still names the families we ship ------------------------------
# A woff2 that loads but is never referenced by --font-sans / --font-data would render nothing.
families = set(re.findall(r"font-family:\s*'([^']+)'", "".join(faces)))
for fam in families:
    check("%s is referenced by a font token" % fam,
          re.search(r"--font-(?:sans|data|mono)\s*:[^;]*%s" % re.escape(fam), CSS) is not None)

# ---- 6. font-display, so text is never invisible while a face loads ---------------------
check("every @font-face sets font-display:swap",
      all("font-display" in b and "swap" in b for b in faces))

print()
if FAILS:
    print("%d FAILED" % len(FAILS))
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("ALL FONT CHECKS PASS")
