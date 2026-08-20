"""
detect_reposts.py — which employers keep re-posting the same role?

A SHIM. The report and the clustering both live in `scraper/reposts.py`, which is where the
implementation had to move: only `scraper/`, `resume_brain/`, `templates/`, `static/` and a named
list of root modules reach the cPanel box (see ../.cpanel.yml), so a cron line pointing at
`scripts/` could never have worked. `bin/cron_scrape.sh` calls the module form for that reason.

This file stays because the path is in commit messages, the workflow and muscle memory, and one
implementation with two entry points beats two implementations.

    python scripts/detect_reposts.py --write     # identical to:
    python -m scraper.reposts --write
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scraper.reposts import main

if __name__ == "__main__":
    main()
