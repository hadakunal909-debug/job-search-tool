"""The job description renderer, ported to TypeScript, proved EQUIVALENT to the original.

Not a snapshot test. A snapshot would only prove the port is self-consistent; this runs the
ORIGINAL implementation (lifted out of static/app.js) and the PORT (web/src/feed/jd.ts) over
the same 20 real descriptions and diffs the output byte for byte.

Why that matters here more than anywhere else in the migration: jd.ts is ~180 lines of
heuristics whose every threshold was chosen from a measurement on the live corpus, and a drift
would not throw. It would just render every job description as mush, and be discovered by
reading rather than by a test failing.

The fixture covers all five shapes the corpus actually contains, and is weighted the way the
corpus is: measured over 16,344 stored descriptions, 13,339 of them (82%) arrive as one
unbroken run with zero newlines, because Workday, iCIMS and Amazon hand over the whole posting
as a single ~4,000 character string.

Needs node (already a hard dependency of feed_parity.py and test_filter_memory.py). Node 24
strips TypeScript types natively, so jd.ts runs without a build step.

    python scripts/test_jd_render.py        # exit 0 = the two implementations agree
"""
import io
import json
import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_JS = os.path.join(ROOT, "static", "app.js")
PORT_TS = os.path.join(ROOT, "web", "src", "feed", "jd.ts")
FIXTURE = os.path.join(HERE, "fixtures", "jd_samples.json")

# The original's esc() builds a DOM node, which cannot run under node. Both sides are given
# THIS implementation so the comparison isolates the rendering logic. The escaping itself is
# verified separately against a real browser, and jd.ts documents the four rules it must match.
PURE_ESC = """
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/\\u00a0/g, "&nbsp;");
}
"""


def lift_original():
    """The JD block out of app.js: from JD_BULLET to the end of jdHTML, brace counted.

    Sliced as one contiguous region rather than function by function, so nothing between the
    declarations can be silently dropped. A marker that no longer exists is a hard failure,
    not a silent skip: the whole point is to notice when the original moves.
    """
    src = io.open(APP_JS, encoding="utf-8").read()
    start = src.find("var JD_BULLET")
    if start < 0:
        raise SystemExit("test_jd_render: can't find JD_BULLET in static/app.js")
    fn = src.find("function jdHTML", start)
    if fn < 0:
        raise SystemExit("test_jd_render: can't find jdHTML() in static/app.js")
    i = src.index("{", fn)
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
        j += 1
    raise SystemExit("test_jd_render: unbalanced braces reading jdHTML() from app.js")


def run(script, label):
    with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False,
                                     encoding="utf-8", dir=ROOT) as fh:
        fh.write(script)
        path = fh.name
    try:
        p = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8")
        if p.returncode != 0:
            print("node failed for %s:\n%s" % (label, (p.stderr or "")[:1500]))
            raise SystemExit(1)
        return json.loads(p.stdout)
    finally:
        os.unlink(path)


samples = json.load(io.open(FIXTURE, encoding="utf-8"))
fixture_json = json.dumps([s["jd"] for s in samples])

original = run(
    PURE_ESC + lift_original() + "\nconst JDS = " + fixture_json + ";\n"
    "console.log(JSON.stringify(JDS.map(jdHTML)));\n", "original")

ts_url = "file:///" + PORT_TS.replace("\\", "/")
port = run(
    'import { jdHTML } from "%s";\n' % ts_url
    + "const JDS = " + fixture_json + ";\n"
    "console.log(JSON.stringify(JDS.map(jdHTML)));\n", "port")

print("=" * 78)
print("%d real descriptions, %s bytes of fixture" % (len(samples), format(os.path.getsize(FIXTURE), ",")))
print("=" * 78)

fails = 0
by_shape = {}
for s, a, b in zip(samples, original, port):
    ok = a == b
    by_shape.setdefault(s["shape"], [0, 0])
    by_shape[s["shape"]][1] += 1
    if ok:
        by_shape[s["shape"]][0] += 1
    else:
        fails += 1
        print("\nMISMATCH  %s  %s" % (s["shape"], (s["title"] or "")[:50]))
        for k in range(min(len(a), len(b))):
            if a[k] != b[k]:
                lo = max(0, k - 60)
                print("  first difference at char %d" % k)
                print("  original: ...%s" % a[lo:k + 60].replace("\n", "\\n"))
                print("  port    : ...%s" % b[lo:k + 60].replace("\n", "\\n"))
                break
        else:
            print("  identical up to %d chars, lengths %d vs %d" % (min(len(a), len(b)), len(a), len(b)))

print()
print("%-24s %s" % ("shape", "agree"))
print("-" * 40)
for shape in sorted(by_shape):
    got, tot = by_shape[shape]
    print("%-24s %d/%d" % (shape, got, tot))

# A test that renders nothing proves nothing. The original produced real markup for every
# sample, or the agreement above is agreement on emptiness.
empty = [i for i, a in enumerate(original) if len(a) < 40]
print()
print("every sample produced real markup:", not empty, "" if not empty else "empty at %s" % empty)
tags = sum(a.count("<li>") for a in original), sum(a.count("<h4") for a in original), \
    sum(a.count("<p>") for a in original)
print("markup produced: %d <li>, %d <h4>, %d <p>" % tags)
if empty or sum(tags) < 50:
    print("\nVACUOUS: the fixture is not exercising the renderer.")
    raise SystemExit(1)

print()
if fails:
    print("THE PORT DIFFERS FROM THE ORIGINAL in %d of %d descriptions." % (fails, len(samples)))
    raise SystemExit(1)
print("PORT IS BYTE-IDENTICAL TO THE ORIGINAL across all %d descriptions." % len(samples))
