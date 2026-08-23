#!/usr/bin/env python3
"""The logo harvest: its identity rules, its acceptance test, and its markup contract.

    python scripts/test_logos.py

Offline. No network, no database. Everything the harvester decides is decided by a pure
function with its transport injected, which is what makes it testable at all.

WHY THIS SUITE EXISTS. Before 2026-08-22 no test anywhere asserted on logo markup, on `.logo`,
on the palette or on the initial, which is how 53% of tiles came to be something other than a
usable brand logo without anyone noticing. Every check below names the defect it froze.
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(APP)

from feed_parity import js_function            # reuse the lifter, do not re-type it  # noqa: E402
import core                                    # noqa: E402

sys.path.insert(0, os.path.join(APP, "scripts"))
import importlib.util                          # noqa: E402
_spec = importlib.util.spec_from_file_location("build_logos",
                                               os.path.join(APP, "scripts", "build_logos.py"))
bl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bl)

FAILS = []


def check(name, cond, extra=""):
    if not cond:
        FAILS.append(name)
    print("  %-4s%-62s %s" % ("ok" if cond else "FAIL", name, extra))


def head(t):
    print()
    print("=" * 92)
    print(t)
    print("=" * 92)


# ======================================================================== the twins
head("THE THREE TWINS (python <-> javascript)")

JS = io.open(os.path.join(APP, "static", "companies.js"), encoding="utf-8").read()
NAMES = [r[0] for r in json.load(io.open("companies.json", encoding="utf-8"))["rows"]]

# The corpus goes through a FILE, not argv: 2,695 names as one argument is past Windows'
# command-line limit and fails with WinError 206.
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
    json.dump(NAMES, fh, ensure_ascii=False)
    names_path = fh.name
driver = "\n".join([
    js_function(JS, "slug"),
    "var names = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));",
    "console.log(JSON.stringify(names.map(function (n) { return [slug(n)]; })));",
])
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
    fh.write(driver)
    path = fh.name
try:
    out = subprocess.run([("node.exe" if os.name == "nt" else "node"), path, names_path],
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    got = json.loads(out.stdout or "[]") if out.returncode == 0 else None
finally:
    os.unlink(path)
    os.unlink(names_path)

if got is None:
    check("node ran the lifted functions", False, (out.stderr or "")[:160])
else:
    # SLUG IS THE ASSET FILENAME. If the two sides ever disagree, the page asks for a file the
    # harvest did not write and every affected tile silently becomes a monogram. Frozen against
    # the WHOLE corpus on the day it was introduced rather than after it drifts, which is the
    # filter-triplet lesson in CLAUDE.md applied to a 40-character function.
    bad = [(n, g[0], bl.slugify(n)) for n, g in zip(NAMES, got) if g[0] != bl.slugify(n)]
    check("companies.js::slug matches build_logos.py::slugify for all %d names" % len(NAMES),
          not bad, "%d differ, e.g. %s" % (len(bad), bad[:2]) if bad else "")
    # THERE IS NO initials() TWIN IN JAVASCRIPT, and that is the assertion. The rule needs
    # core.norm_company to strip Technologies/Group/Labs, and a JS copy of that list disagreed on
    # 225 of 2,695 names, collapsing every "<X> Technologies" employer onto AT. So the server
    # computes the monograms and ships them index-parallel in cometa.
    check("companies.js does NOT reimplement initials()", "function initials" not in JS)
    check("and it reads the server-supplied monograms instead", "META.mono" in JS)

    import web
    bad = [(n, bl.initials(n), web.initials(n)) for n in NAMES
           if bl.initials(n) != web.initials(n)]
    check("web.py::initials matches build_logos.py::initials for all %d names" % len(NAMES),
          not bad, str(bad[:2]))

# The measured cases each rule exists for.
for name, want in (("Amazon.com Services LLC", "AS"), ("U.S. Bank", "US"),
                   ("Ernst & Young", "EY"), ("10x Genomics", "10"), ("Accenture", "AC")):
    check("initials(%r) is %s" % (name, want), bl.initials(name) == want, bl.initials(name))


# ======================================================================== markup contract
head("THE MARKUP CONTRACT")

lock = "\n".join([
    js_function(JS, "slug"),
    js_function(JS, "logoFor"),
    js_function(JS, "lockup"),
    "function esc(s){return String(s==null?'':s).replace(/[&<>\"']/g,function(c){"
    "return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c];});}",
    "var LOGOS = {'acme-inc': ['svg', 3.5, 0], 'mono-co': ['webp', 1.0, 1]};",
    "var LOGOV = 42;",
    "console.log(JSON.stringify({hit: lockup('Acme Inc', 'AI'), alias: lockup('Aliased Co', 'AC'),"
    " mono: lockup('Mono Co', 'MC'),"
    " miss: lockup('Nobody At All', 'NA'), digit: lockup('10x Genomics', '10')}));",
])
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
    fh.write(lock)
    path = fh.name
try:
    out = subprocess.run([("node.exe" if os.name == "nt" else "node"), path],
                         capture_output=True, text=True, encoding="utf-8", timeout=60)
    m = json.loads(out.stdout) if out.returncode == 0 else None
finally:
    os.unlink(path)

if m is None:
    check("node ran lockup()", False, (out.stderr or "")[:200])
else:
    # A HIT EMITS EXACTLY ONE <img> AND NO MONOGRAM. Asserted as a count rather than a substring
    # so reintroducing a second URL, a data-fallback or a layered monogram fails here.
    check("a manifest hit emits exactly one <img>", m["hit"].count("<img") == 1, m["hit"][:70])
    check("and it is same-origin under /static/logos/",
          "/static/logos/acme-inc.svg?v=42" in m["hit"])
    check("and no monogram is layered underneath it", "comono" not in m["hit"])
    check("and no fallback URL survives", "data-fallback" not in m["hit"])
    # THERE IS NO CLIENT-SIDE ALIAS STEP, and that is the assertion. companies.js used to read
    # ALIAS[slug(name)] while web.py keys that map on core.norm_company -- "1star networks"
    # against a lookup of "1star-networks-llc" -- so it could never hit for any name, and the
    # map was being shipped and ignored. A spelling the manifest does not hold is a MISS here,
    # exactly like an unknown employer, and the fix is a build_companies.py run rather than a
    # suffix list duplicated into JavaScript.
    check("a spelling the manifest does not hold is a miss, not an alias lookup",
          m["alias"].count("<img") == 0 and 'class="comono"' in m["alias"], m["alias"][:80])
    # data-mono ON THE IMAGE. Its absence was a real bug: the dark-mode rule
    # [data-theme="dark"] .colock img[data-mono="1"] never matched on /companies, so every
    # single-ink mark kept the light plate that commit 3df171d was written to remove -- while
    # the feed, whose markup lives in app.js, un-plated them correctly. logoFor() computed the
    # flag and lockup() dropped it.
    check("a single-ink mark carries data-mono for the dark-mode inversion",
          'data-mono="1"' in m["mono"], m["mono"][:90])
    check("and a full-colour mark does not, because inverting it would hue-shift the brand",
          "data-mono" not in m["hit"], m["hit"][:90])
    # A MISS EMITS ZERO <img>. The old markup emitted one anyway and let it 404, which is how a
    # blank-but-200 response came to paint an opaque white square over the letter tile.
    check("a manifest miss emits ZERO <img>", m["miss"].count("<img") == 0, m["miss"][:70])
    check("and renders a two-character monogram",
          re.search(r'class="comono"[^>]*>([A-Z0-9]{2})<', m["miss"]) is not None, m["miss"][:80])
    check("the monogram is passed through verbatim, not re-derived",
          ">10<" in m["digit"] and ">NA<" in m["miss"], m["digit"][:70])
    check("no inline background colour anywhere (the hash palette is retired)",
          "background:" not in (m["hit"] + m["miss"]))


# ======================================================================== the entity gate
head("THE ENTITY GATE, FROZEN OFFLINE")

# A slice of real Wikidata, captured 2026-08-22. `parents` is the P279 map the walk reads, and
# it is injected -- which is the whole reason the walk is testable without a network.
PARENTS = {
    "Q4830453": ["Q43229"],            # business -> organization
    "Q891723": ["Q4830453"],           # public company -> business
    "Q18388277": ["Q4830453"],         # technology company -> business
    "Q3918": ["Q2385804"],             # university -> educational institution
    "Q43229": [],
    "Q101352": [],                     # family name
    "Q16521": [],                      # taxon
    # A four-hop chain, to tell "walks the subclass tree" apart from "has a list of leaf classes"
    "Q_L1": ["Q_L2"], "Q_L2": ["Q_L3"], "Q_L3": ["Q_L4"], "Q_L4": ["Q43229"],
    # A cycle, which a naive walk hangs on
    "Q_C1": ["Q_C2"], "Q_C2": ["Q_C1"],
}


class FakeCache(object):
    def __init__(self):
        self.calls = 0

    def warm(self, qids):
        self.calls += 1

    def parents(self, qid):
        return PARENTS.get(qid, [])


def claims_of(*p31):
    return {"P31": [{"mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": q}}},
                     "rank": "normal"} for q in p31]}


c = FakeCache()
ok, _ = bl.is_org("Q1", claims_of("Q4830453"), c)
check("a business reaches the organisation root", ok)
ok, path = bl.is_org("Q1", claims_of("Q_L1"), c)
check("an organisation FOUR P279 hops up is still reached", ok, str(path))
ok, _ = bl.is_org("Q37484767", claims_of("Q101352"), c)
check("EY's family-name entity (Q37484767 -> Q101352) is rejected", not ok)
ok, _ = bl.is_org("Q216441", claims_of("Q16521"), c)
check("Arctic Wolf's taxon entity is rejected", not ok)
ok, _ = bl.is_org("Q1", claims_of("Q4830453", "Q101352"), c)
check("a hard-reject P31 wins even alongside an organisational one", not ok)
ok, _ = bl.is_org("Q1", {}, c)
check("NO P31 at all is a reject, not an accept-by-default", not ok)
ok, _ = bl.is_org("Q1", claims_of("Q_C1"), c)
check("a P279 cycle terminates instead of hanging", not ok)


# ======================================================================== identity rules
head("IDENTITY: THE RULES THAT KEPT A WRONG LOGO OFF A TILE")

# Wikidata's P856 for Qualcomm (Q544847) really is consumerrights.wiki. The entity is correct;
# the field is polluted. So a curated field still needs an independent sanity check.
check("a domain unrelated to the name is rejected (Qualcomm/consumerrights.wiki)",
      not bl.domain_agrees("Qualcomm", "consumerrights.wiki"))
check("a two-letter name matches its own domain (EY/ey.com)",
      bl.domain_agrees("EY", "ey.com"))
check("an acronym domain matches (Ernst & Young/ey.com)",
      bl.domain_agrees("Ernst & Young", "ey.com"))
check("a token inside the domain matches (HCL Technologies/hcltech.com)",
      bl.domain_agrees("HCL Technologies", "hcltech.com"))
check("a truncated domain matches (Citibank/citi.com)",
      bl.domain_agrees("Citibank", "citi.com"))

# Searching "LinkedIn" returns LinkedIn Learning, Ireland and News but not LinkedIn itself, and
# Learning carries a real logo -- so a symmetric subset test shipped the wrong brand.
check("a MORE SPECIFIC label is not a match (LinkedIn/LinkedIn Learning)",
      not bl.label_ok("LinkedIn", "LinkedIn Learning"))
check("a less specific label still is (Meta Platforms/Meta)",
      bl.label_ok("Meta Platforms", "Meta"))
check("an exact label is (Google/Google)", bl.label_ok("Google", "Google"))

# Products live on the company's domain, so a domain match alone cannot tell them apart.
check("Google Maps is too specific to be Google",
      bl.label_too_specific("Google", "Google Maps"))
check("Amazon Web Services is too specific to be Amazon",
      bl.label_too_specific("Amazon", "Amazon Web Services"))
check("Ernst & Young is NOT 'too specific' for EY (neither contains the other)",
      not bl.label_too_specific("EY", "Ernst & Young"))
# A CORPORATE FORM IS NOT A NARROWER THING. These four are measured: with Holding/L.P. left in,
# the rule skipped every candidate and ASML, Bloomberg, Amat and Barclays ended the run with no
# entity at all -- four of the top 143 employers by filing volume.
check("ASML Holding is not too specific for ASML",
      not bl.label_too_specific("ASML", "ASML Holding"))
check("Bloomberg L.P. is not too specific for Bloomberg",
      not bl.label_too_specific("Bloomberg", "Bloomberg L.P."))
check("Capgemini America is not too specific for Capgemini",
      not bl.label_too_specific("Capgemini", "Capgemini America"))
# The 10:1 and 9.6:1 wordmarks that an 8:1 aspect ceiling rejected.
check("a 10:1 wordmark is within the aspect ceiling", bl.MAX_AR >= 10.0, str(bl.MAX_AR))

# P154 can point at a historical logo, or at a photograph.
def p154(*files):
    return {"P154": [{"mainsnak": {"snaktype": "value", "datavalue": {"value": f}},
                      "rank": "normal"} for f in files]}


check("a year-range filename is skipped (Intel)",
      bl.pick_logo_file(p154("Intel logo (1968-2006).svg", "Intel logo 2023.svg"))
      == "Intel logo 2023.svg")
check("a JPEG is skipped: Cognizant's P154 is a photo of their office",
      bl.pick_logo_file(p154("Cognizant Technology Solutions - Kolkata 2011.JPG")) == "")
check("vector is preferred over raster, not the shortest name (Google)",
      bl.pick_logo_file(p154("Google.png", "Google 2026 logo.svg")) == "Google 2026 logo.svg")
check("an end-dated (P582) statement is dropped",
      bl.pick_logo_file({"P154": [
          {"mainsnak": {"snaktype": "value", "datavalue": {"value": "Old.svg"}},
           "rank": "normal", "qualifiers": {"P582": [{}]}},
          {"mainsnak": {"snaktype": "value", "datavalue": {"value": "New.svg"}},
           "rank": "normal"}]}) == "New.svg")
check("a deprecated statement is dropped",
      bl.claim_values({"P856": [
          {"mainsnak": {"snaktype": "value", "datavalue": {"value": "http://bad/"}},
           "rank": "deprecated"}]}, "P856") == ([], []))


# ======================================================================== the acceptance test
head("THE ACCEPTANCE TEST, ON GENERATED IMAGES")

try:
    from PIL import Image, ImageDraw
except Exception:
    check("Pillow is available", False)
    Image = None

if Image is not None:
    def png(im):
        b = io.BytesIO()
        im.save(b, "PNG")
        return b.getvalue()

    def solid(w, h, rgba):
        return png(Image.new("RGBA", (w, h), rgba))

    # A real-ish wordmark: black strokes on transparent, which is what most brand SVGs render as.
    wm = Image.new("RGBA", (250, 60), (0, 0, 0, 0))
    d = ImageDraw.Draw(wm)
    for i in range(8):
        d.rectangle([10 + i * 30, 12, 26 + i * 30, 48], fill=(17, 17, 17, 255))
        d.ellipse([12 + i * 30, 20, 24 + i * 30, 40], fill=(200, 40, 40, 255))
    good = png(wm)

    ok, why, meta = bl.judge(good)
    check("a black-on-transparent wordmark is ACCEPTED", ok, why or str(meta))
    check("and its aspect ratio is recorded", (meta.get("ar") or 0) > 3, str(meta.get("ar")))

    ok, why, _ = bl.judge(solid(200, 200, (222, 22, 43, 255)))
    check("a solid colour block is rejected (U.S. Bank's favicon class)",
          not ok and why == "solid-block", why)
    ok, why, _ = bl.judge(solid(200, 200, (0, 0, 0, 0)))
    check("a fully transparent image is rejected (Starkey's blank class)",
          not ok and why == "blank", why)
    ok, why, _ = bl.judge(solid(200, 200, (255, 255, 255, 255)))
    check("a knockout white mark reads as blank on the plate it renders on",
          not ok and why == "blank", why)
    small = wm.resize((32, 8))
    ok, why, _ = bl.judge(png(small))
    check("a 32px icon is rejected (Oracle/EY favicon class)",
          not ok and why == "undersized", why)

    # A photograph: rich in every metric that means "not degenerate", which is exactly why the
    # colour CEILING exists. Cognizant's P154 was a photo of their office wall.
    import random
    random.seed(4)
    ph = Image.new("RGBA", (200, 200))
    ph.putdata([(random.randrange(256), random.randrange(256), random.randrange(256), 255)
                for _ in range(200 * 200)])
    ok, why, _ = bl.judge(png(ph))
    check("a photograph is rejected by the colour ceiling", not ok and why == "photograph", why)

    banner = Image.new("RGBA", (2000, 100), (0, 0, 0, 0))
    ImageDraw.Draw(banner).rectangle([0, 40, 1900, 60], fill=(10, 10, 200, 255))
    ok, why, _ = bl.judge(png(banner))
    check("a 20:1 banner is rejected on aspect", not ok and why == "aspect", why)


# ======================================================================== svg hygiene
head("SVG SANITISATION: STRIP, THEN VERIFY")

HOSTILE = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
           b'<script>alert(1)</script>'
           b'<rect width="10" height="10" onload="alert(2)" fill="#123456"/>'
           b'<image href="https://evil.example/x.png"/>'
           b'<foreignObject><body>hi</body></foreignObject>'
           b'<metadata>junk</metadata><title>t</title>'
           b'</svg>')
out, why = bl.sanitise_svg(HOSTILE)
check("a hostile SVG survives sanitisation", bool(out), why)
if out:
    for token in (b"<script", b"onload", b"foreignObject", b"evil.example", b"<metadata"):
        check("stripped %s" % token.decode(), token.lower() not in out.lower())
    check("the artwork itself survives", b"rect" in out and b"#123456" in out)
out, why = bl.sanitise_svg(b'<?xml version="1.0"?><!DOCTYPE s [<!ENTITY x "y">]><svg/>')
check("an entity declaration is refused outright", not out, why)

# The scanner --check re-runs over the bytes that actually ship must agree with the sanitiser.
check("the shipped-bytes scanner catches a script tag",
      bl.SVG_UNSAFE_OUT.search(b"<svg><script>x</script></svg>") is not None)
check("and an on* handler", bl.SVG_UNSAFE_OUT.search(b'<svg><g onclick="x"/></svg>') is not None)
check("and an external reference",
      bl.SVG_EXTERNAL.search(b'<svg><image href="https://x/y"/></svg>') is not None)
check("and passes a clean one",
      bl.SVG_UNSAFE_OUT.search(b'<svg><rect fill="#fff"/></svg>') is None)


# ======================================================================== the gate can fail
head("STORE WRITES WHAT IT HASHES")

# THE DIGEST MUST DESCRIBE THE FILE ON DISK. The caller used to hash the bytes it downloaded
# while store() wrote a re-encoded WebP, so every raster asset disagreed with its own recorded
# hash: --check reported a mismatch for all of them and each harvest re-fetched 525 rows it had
# already done. Both paths are asserted because only one of them re-encodes.
import hashlib as _h                                                     # noqa: E402
_sealdir = tempfile.mkdtemp()
_cwd = os.getcwd()
os.chdir(_sealdir)
try:
    _svg = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 9 9">'
            b'<rect width="9" height="9" fill="#123456"/></svg>')
    fn, n, sha, why = bl.store("seal-svg", _svg, True)
    on_disk = io.open(os.path.join(bl.LOGO_DIR, fn), "rb").read()
    check("the SVG path hashes the bytes it wrote",
          bool(fn) and sha == _h.sha256(on_disk).hexdigest()[:16], why or fn)
    if Image is not None:
        # A real-ish mark, not a plain rectangle: store() re-judges its own output now, and a flat
        # filled box legitimately fails the gate (two colours, edges only at its border).
        _im = Image.new("RGBA", (240, 120), (0, 0, 0, 0))
        _d = ImageDraw.Draw(_im)
        for _i in range(7):
            _d.rectangle([12 + _i * 32, 24, 30 + _i * 32, 96], fill=(17, 17, 17, 255))
            _d.ellipse([14 + _i * 32, 40, 28 + _i * 32, 80], fill=(200, 40, 40, 255))
        _buf = io.BytesIO()
        _im.save(_buf, "PNG")
        fn, n, sha, why = bl.store("seal-raster", _buf.getvalue(), False)
        on_disk = io.open(os.path.join(bl.LOGO_DIR, fn), "rb").read() if fn else b""
        check("the raster path hashes the RE-ENCODED bytes, not the source",
              bool(fn) and sha == _h.sha256(on_disk).hexdigest()[:16], why or fn)
        check("and it really did re-encode to webp", fn.endswith(".webp"), fn)
        # JUDGE WHAT YOU SHIP. 36 assets were accepted on their source bytes and then rejected by
        # --check on the stored WebP, because lossy re-encoding adds enough colour noise to cross
        # the photograph ceiling. store() now re-judges every candidate encoding.
        check("and the stored bytes still pass the gate", bl.judge(on_disk)[0], str(bl.judge(on_disk)[1]))
        _flat = Image.new("RGBA", (200, 200), (222, 22, 43, 255))
        _b2 = io.BytesIO()
        _flat.save(_b2, "PNG")
        fn2, _n2, _s2, why2 = bl.store("seal-solid", _b2.getvalue(), False)
        check("store REFUSES an encoding that would fail its own gate",
              fn2 == "" and why2 == "reencode-failed-gate", "%s / %s" % (fn2, why2))
finally:
    os.chdir(_cwd)

head("THE GATE CAN ACTUALLY FAIL")

# docs/INDEX.md records two suites in this repo that printed "N FAILED" and exited 0 for months.
# A gate that cannot fail is worse than no gate, so each condition is forced.
import shutil                                                              # noqa: E402


class Args(object):
    pass


def run_check_in(tmp):
    """--check against a temporary tree, with its OUTPUT SWALLOWED.

    Not cosmetic. run_tests.py greps every suite's stdout for a FAILED line and marks the suite
    failed even on exit 0 -- a tripwire that exists because two suites in this repo printed
    "N FAILED" and exited 0 for months (docs/INDEX.md B3). The forced failures below are the
    assertion, so their own reports must not reach that grep or this suite fails while passing.
    """
    import contextlib
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            return bl.run_check(Args())
    finally:
        os.chdir(cwd)


base = tempfile.mkdtemp()
os.makedirs(os.path.join(base, "static", "logos"))
rows = [["Acme", 0, "", 0, "acme.com", 5000, 1, 3, "Acme"]]
json.dump({"rows": rows, "sectors": ["S"]},
          io.open(os.path.join(base, "companies.json"), "w", encoding="utf-8"))
svg = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="9" height="9" fill="#123"/></svg>'
io.open(os.path.join(base, "static", "logos", "acme.svg"), "wb").write(svg)
import hashlib                                                             # noqa: E402
json.dump({"v": 1, "ar": {"acme": ["svg", 2.0, 0]}, "alias": {}},
          io.open(os.path.join(base, "static", "logos", "index.json"), "w", encoding="utf-8"))
json.dump({"rows": {"acme": {"name": "Acme", "verdict": "accepted", "asset": "acme.svg",
                             "sha256": hashlib.sha256(svg).hexdigest()[:16]}}},
          io.open(os.path.join(base, "logo_harvest.json"), "w", encoding="utf-8"))

check("a clean tree passes --check", run_check_in(base) == 0)


def broken(mutate, label):
    tmp = tempfile.mkdtemp()
    shutil.copytree(base, tmp, dirs_exist_ok=True)
    mutate(tmp)
    check(label, run_check_in(tmp) == 1)


broken(lambda t: os.remove(os.path.join(t, "static", "logos", "acme.svg")),
       "--check fails on a manifest entry whose file is missing")
broken(lambda t: io.open(os.path.join(t, "static", "logos", "acme.svg"), "wb").write(b"<svg/>"),
       "--check fails when the bytes do not match the ledger sha256")
broken(lambda t: io.open(os.path.join(t, "static", "logos", "orphan.webp"), "wb").write(b"x"),
       "--check fails on an asset with no manifest entry")
broken(lambda t: io.open(os.path.join(t, "static", "logos", "acme.svg"), "wb").write(
           b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>'),
       "--check fails on a shipped SVG that still contains a script")
broken(lambda t: json.dump(
           {"v": 1, "ar": {}, "alias": {}},
           io.open(os.path.join(t, "static", "logos", "index.json"), "w", encoding="utf-8")),
       "--check fails when a 1000+ filing employer has no logo")


def two_on_one_domain(t):
    # Asserted against the MAP, not against companies.json's derived column: the map is what
    # --write-domains owns and the only one a laptop or CI can actually fix.
    json.dump({"domains": {"acme": "acme.com", "beta": "acme.com"}},
              io.open(os.path.join(t, "company_domains.json"), "w", encoding="utf-8"))


broken(two_on_one_domain,
       "--check fails when two employers share one domain (the ATS-host leak)")

print()
if FAILS:
    print("%d FAILED" % len(FAILS))
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("ALL LOGO CHECKS PASS")
