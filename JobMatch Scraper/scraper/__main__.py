"""Entry point for `python -m scraper` (replaces the old `python scraper.py`).

The scraper module's code lives in this package's __init__.py, so `import scraper`
keeps working everywhere; running the scrape from the CLI goes through here.
"""
from scraper import main

if __name__ == "__main__":
    main()
