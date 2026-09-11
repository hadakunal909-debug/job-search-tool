"""The mmap'd idf sidecar must answer EXACTLY what json.load answers, or every match % moves.

core.load_idf() stopped returning a 1,015,658-entry dict and started returning a lookup into
an on-disk hash table (core.py::_IdfIndex). Measured on the real idf.json: 858 ms and +113 MB
of resident heap became 76 ms and a file-backed mapping -- but idf weights feed
core.analyze_jd, which feeds every score on every card, so a table that drops a key or
returns a neighbour's value would not crash anything. It would quietly restate the product.

WHAT EACH CHECK IS GUARDING, since none of them is decorative:

  round-trip      every key, not a sample -- an open-addressed table loses keys at the point
                  where the probe chain is wrong, which is data-dependent by construction.
  misses          a probe chain has to STOP. If it stops one slot early a present term reads
                  as absent; if it never stops a missing term hangs.
  cross-process   the table is built by /warm in one worker and read by all the others.
                  Python's hash() for str is salted per process by PYTHONHASHSEED, so a table
                  keyed on it would read as half-empty in every OTHER worker -- silently, and
                  only for some terms. This is the reason _idx_slot exists, and the subprocess
                  below is the only check that can see it.
  staleness       content changed -> REFUSED (never serve weights from a different corpus);
                  mtime-only change -> still used, because a deploy is a zip extract that
                  rewrites every file without changing a byte. Both directions, both bugs.
  no write        nothing on a request path may build the sidecar; the build is ~3.5 s.
  damage          a truncated or garbage sidecar must fall back to json.load, not raise.

Runs fully offline on synthetic tables; when the real idf.json is present it repeats the
round-trip over all million terms, which is the only place the probe distribution is real.

    python scripts/test_idf_index.py
"""
import os
import sys
import json
import shutil
import struct
import random
import tempfile
import subprocess

os.environ.setdefault("EV_OFF", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core

FAILS = []


def want(name, cond, extra=""):
    print("  %s %-52s %s" % ("ok " if cond else "FAIL", name, extra))
    if not cond:
        FAILS.append(name)


def _synthetic(n=4000, seed=11):
    """A table with the shapes that break naive implementations: unicode, spaces, an empty
    key, one very long term, near-duplicates, and values that are not round numbers."""
    rnd = random.Random(seed)
    alpha = "abcdefghijklmnopqrstuvwxyz0123456789-./+# "
    idf = {}
    for i in range(n):
        k = "".join(rnd.choice(alpha) for _ in range(rnd.randint(1, 30))).strip()
        if k:
            idf[k] = round(rnd.uniform(0.5, 11.0932), 4)
    idf.update({
        "": 1.0, "python": 3.25, "python ": 3.5, " python": 3.75, "Python": 4.0,
        "machine learning": 7.5, "c++": 9.0, "naïve bayes": 8.25, "日本語": 6.0,
        "x" * 3000: 2.5, "0": 11.0932, "sql": 1.0,
    })
    return idf


def _write(tmp, idf, name="idf.json"):
    p = os.path.join(tmp, name)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(idf, fh, sort_keys=True)
    return p


def round_trip():
    print("=" * 74)
    print("every term round-trips, and a miss is a miss")
    print("=" * 74)
    tmp = tempfile.mkdtemp(prefix="idfidx_")
    try:
        idf = _synthetic()
        p = _write(tmp, idf)
        want("build_idf_index writes a sidecar", core.build_idf_index(p) is True)
        want("a second build is a no-op", core.build_idf_index(p) is False)

        idx = core.load_idf(p)
        want("load_idf returns the index, not a dict",
             type(idx).__name__ == "_IdfIndex", type(idx).__name__)
        want("len() matches", len(idx) == len(idf), "%d vs %d" % (len(idx), len(idf)))
        want("bool() is True for a non-empty table", bool(idx) is True)

        bad = [k for k, v in idf.items() if idx.get(k) != v]
        want("all %d terms round-trip" % len(idf), not bad, "%d wrong: %r" % (len(bad), bad[:3]))

        absent = ["no_such_term_%d" % i for i in range(3000)]
        want("a miss returns the default",
             all(idx.get(k, 7.77) == 7.77 for k in absent))
        want("a miss is not `in`", all(k not in idx for k in absent))
        want("a hit is `in`", all(k in idx for k in ("python", "c++", "日本語")))
        want("[] raises KeyError on a miss", _raises_keyerror(idx, "no_such_term_0"))
        want("[] returns the value on a hit", idx["python"] == 3.25)

        # get() must not blow up on a non-str key the way dict.get would not.
        want("a non-str key returns the default", idx.get(17, "dflt") == "dflt")

        # Nothing in the app iterates, so this is the check that keeps it honest if anything
        # ever starts: an empty generator would be a silently wrong answer, not an error.
        want("iteration round-trips the whole table", dict(idx.items()) == idf)
        want("keys() matches", sorted(idx.keys()) == sorted(idf))
        idx.close()
    finally:
        core._reset_idf_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def _raises_keyerror(idx, k):
    try:
        idx[k]
        return False
    except KeyError:
        return True


def cross_process():
    print("=" * 74)
    print("the table is readable from a DIFFERENT process (the PYTHONHASHSEED trap)")
    print("=" * 74)
    tmp = tempfile.mkdtemp(prefix="idfidx_")
    try:
        idf = _synthetic(n=2000, seed=5)
        p = _write(tmp, idf)
        core.build_idf_index(p)
        app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        reader = os.path.join(tmp, "reader.py")
        with open(reader, "w", encoding="utf-8") as fh:
            fh.write(
                "import sys, json\n"
                "sys.path.insert(0, %r)\n"
                "import core\n"
                "idf = json.load(open(%r, encoding='utf-8'))\n"
                "idx = core.load_idf(%r)\n"
                "assert type(idx).__name__ == '_IdfIndex', type(idx).__name__\n"
                "bad = [k for k, v in idf.items() if idx.get(k) != v]\n"
                "print(len(bad))\n" % (app, p, p))

        # Two runs under DIFFERENT hash seeds. A table keyed on Python's own hash() passes at
        # seed 0 (hashing disabled) and fails at any other -- so one seed proves nothing.
        for seed in ("0", "1", "4242"):
            env = dict(os.environ, PYTHONHASHSEED=seed, EV_OFF="1")
            r = subprocess.run([sys.executable, reader], capture_output=True, text=True, env=env)
            out = (r.stdout or "").strip().splitlines()
            want("PYTHONHASHSEED=%-5s reads every term" % seed,
                 r.returncode == 0 and out and out[-1] == "0",
                 (r.stderr or "")[-160:] if r.returncode else "%s wrong" % (out[-1] if out else "?"))
    finally:
        core._reset_idf_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def staleness():
    print("=" * 74)
    print("stale content is REFUSED; a deploy's rewrite is not a content change")
    print("=" * 74)
    tmp = tempfile.mkdtemp(prefix="idfidx_")
    try:
        idf = _synthetic(n=500, seed=3)
        p = _write(tmp, idf)
        core.build_idf_index(p)
        core._reset_idf_cache()
        want("fresh sidecar is used", type(core.load_idf(p)).__name__ == "_IdfIndex")

        # A DEPLOY: the file is rewritten, every byte identical, mtime moves. The row_cache
        # learned this one the expensive way -- on mtime it invalidated 40k rows and charged
        # the first visitor ~7 s for a release that changed nothing.
        raw = open(p, "rb").read()
        with open(p, "wb") as fh:
            fh.write(raw)
        os.utime(p, (0, 0))
        core._reset_idf_cache()
        want("mtime moved, content identical -> still used",
             type(core.load_idf(p)).__name__ == "_IdfIndex")

        # A REBUILD: one weight changes. Serving the old table here would restate every score
        # that touches that term, with nothing anywhere reporting an error.
        idf2 = dict(idf)
        idf2["python"] = 99.0
        _write(tmp, idf2)
        core._reset_idf_cache()
        got = core.load_idf(p)
        want("content changed -> sidecar refused, falls back to the dict",
             isinstance(got, dict), type(got).__name__)
        want("...and the fallback has the NEW value", got.get("python") == 99.0, repr(got.get("python")))

        # DAMAGE: half a file, and a file that is not one of ours at all.
        idx_path = p + ".idx"
        core.build_idf_index(p, force=True)
        good = open(idx_path, "rb").read()
        for label, blob in (("truncated", good[:len(good) // 2]),
                            ("garbage", b"not an index at all" * 50),
                            ("empty", b"")):
            core._reset_idf_cache()      # close any mapping first: Windows will not
                                         # reopen a mapped file for writing
            with open(idx_path, "wb") as fh:
                fh.write(blob)
            got = core.load_idf(p)
            want("a %-9s sidecar falls back instead of raising" % label,
                 isinstance(got, dict) and got.get("python") == 99.0, type(got).__name__)

        # The contract when there is no idf.json at all has to survive all of this.
        core._reset_idf_cache()
        want("no idf.json -> None", core.load_idf(os.path.join(tmp, "nope.json")) is None)
    finally:
        core._reset_idf_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def never_written_by_a_read():
    print("=" * 74)
    print("a read never builds the sidecar (the build is ~3.5 s)")
    print("=" * 74)
    tmp = tempfile.mkdtemp(prefix="idfidx_")
    try:
        p = _write(tmp, _synthetic(n=200, seed=9))
        core._reset_idf_cache()
        got = core.load_idf(p)
        want("load_idf with no sidecar still answers",
             isinstance(got, dict) and len(got) > 100, type(got).__name__)
        want("...and left no sidecar behind", not os.path.exists(p + ".idx"))
        want("...and no .tmp residue",
             not [n for n in os.listdir(tmp) if n.endswith(".tmp")])

        # eager=True is the documented escape hatch and must bypass the index even when one
        # exists, or a caller that asked for a dict silently gets something else.
        core.build_idf_index(p)
        core._reset_idf_cache()
        want("eager=True returns a real dict", isinstance(core.load_idf(p, eager=True), dict))
        core._reset_idf_cache()
        want("eager=False returns the index", type(core.load_idf(p)).__name__ == "_IdfIndex")
    finally:
        core._reset_idf_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def memo_bound():
    print("=" * 74)
    print("the lookup memo is bounded, and clearing it changes no answer")
    print("=" * 74)
    tmp = tempfile.mkdtemp(prefix="idfidx_")
    saved = core._IDX_MEMO_MAX
    try:
        idf = _synthetic(n=6000, seed=17)
        p = _write(tmp, idf)
        core.build_idf_index(p)
        core._reset_idf_cache()
        idx = core.load_idf(p)
        # Forced low so the branch actually fires. At the shipped 200,000 it would not, and
        # an assertion that cannot trip is decoration -- the same rule _cache_max's
        # "cold == loaded" check needed.
        core._IDX_MEMO_MAX = 100
        bad = [k for k, v in idf.items() if idx.get(k) != v]
        want("the cap fires and no answer changes", not bad, "%d wrong" % len(bad))
        want("the memo stayed under the cap", len(idx._memo) <= 100, str(len(idx._memo)))
        # It must survive being asked for the same key on both sides of a clear.
        k = "python"
        first = idx.get(k)
        for i in range(300):
            idx.get("filler_%d" % i)
        want("a value is identical after the memo is cleared", idx.get(k) == first == idf[k])
        idx.close()
    finally:
        core._IDX_MEMO_MAX = saved
        core._reset_idf_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def shown_to_trip():
    print("=" * 74)
    print("the round-trip check can actually fail (a table with a key removed)")
    print("=" * 74)
    tmp = tempfile.mkdtemp(prefix="idfidx_")
    try:
        idf = _synthetic(n=300, seed=13)
        p = _write(tmp, idf)
        core.build_idf_index(p)
        core._reset_idf_cache()
        idx = core.load_idf(p)
        # Corrupt ONE slot in the live mapping's file and rebuild the reader: the sweep must
        # notice. Without this the round-trip assertion above is untestable decoration.
        idx.close()
        core._reset_idf_cache()
        with open(p + ".idx", "r+b") as fh:
            fh.seek(8 + core._IDX_HDR.size + 24)     # the first slot of the table
            fh.write(struct.pack("<I", 0))
        # The stamp is untouched, so the index is still accepted -- exactly the situation the
        # sweep is for.
        idx = core.load_idf(p)
        missing = [k for k, v in idf.items() if idx.get(k) != v]
        want("zeroing one slot IS detected by the sweep", len(missing) >= 1,
             "%d term(s) lost" % len(missing))
        idx.close()
    finally:
        core._reset_idf_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def real_file():
    print("=" * 74)
    print("the real idf.json, if this machine has one")
    print("=" * 74)
    app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(app, core._IDF_PATH)
    if not os.path.exists(p):
        print("  --  no idf.json here; the synthetic tables above are the whole check")
        return
    core._reset_idf_cache()
    d = core.load_idf(p, eager=True)
    core._reset_idf_cache()
    core.build_idf_index(p)
    core._reset_idf_cache()
    idx = core.load_idf(p)
    want("the real file loads through the index",
         type(idx).__name__ == "_IdfIndex", type(idx).__name__)
    if type(idx).__name__ != "_IdfIndex":
        return
    want("term count matches json.load", len(idx) == len(d), "%d vs %d" % (len(idx), len(d)))
    bad = [k for k, v in d.items() if idx.get(k) != v]
    want("all %d real terms round-trip" % len(d), not bad, "%d wrong: %r" % (len(bad), bad[:3]))
    idx.close()
    core._reset_idf_cache()


def main():
    for fn in (round_trip, cross_process, staleness, never_written_by_a_read,
               memo_bound, shown_to_trip, real_file):
        fn()
        print("")
    print("=" * 74)
    print("FAILURES: %d %s" % (len(FAILS), FAILS if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
