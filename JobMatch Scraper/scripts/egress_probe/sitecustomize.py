"""Auto-installs the egress meter — see _meter.py for what it measures and why.

CPython imports a module named `sitecustomize` at interpreter startup if one is importable, so
putting THIS directory (and only this one — it is a directory of its own precisely so that
nothing else here lands on the path) on PYTHONPATH instruments a command without touching it:

    # what would this cost, without spending it:
    EGRESS_DRY=1 PYTHONPATH=scripts/egress_probe python scripts/test_prefs.py

    # what did it actually cost:
    PYTHONPATH=scripts/egress_probe python -m scraper.score_jobs

Set EGRESS_REPORT=<file> to also append a JSON line per command, for summing a whole suite.

Never raises. A diagnostic that can break the program it measures does not get used, and an
interpreter whose sitecustomize throws prints a traceback before main() ever runs.
"""
try:
    import _meter
    _meter.install()
except Exception:
    pass
