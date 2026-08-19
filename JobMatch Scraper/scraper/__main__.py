"""Entry point for `python -m scraper` (replaces the old `python scraper.py`).

The scraper module's code lives in this package's __init__.py, so `import scraper`
keeps working everywhere; running the scrape from the CLI goes through here.

The one wrapped failure is proxy authentication. It has a single cause and a single fix, and it
had been killing scheduled runs behind a raw PgRestError traceback that named neither — so it is
translated into an instruction here rather than left to whoever reads the log. Everything else
still raises, because a traceback is the right output for a bug.
"""
import sys

from scraper import main, _explain_auth_failure

if __name__ == "__main__":
    try:
        main()
    except Exception as err:                       # noqa: BLE001 - re-raised unless it is the 401
        if not _explain_auth_failure(err):
            raise
        sys.exit(1)
