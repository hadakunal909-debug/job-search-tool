# Repo root — three unrelated projects live here

| Directory | What |
|---|---|
| `JobMatch Scraper/` | The Flask job board. **The active project** — it has its own `CLAUDE.md`, read that. |
| `Resume Tailoring/` | Separate, largely superseded. Has its own `web.py`; don't confuse the two. |
| `USCIS H-1B Data Hub/` | Bulk data export, gitignored. |

Root-only files: `.github/workflows/` (4 workflows — `scrape.yml`, `python-tests.yml`,
`web-build.yml`, `jobspy-shadow.yml`), `.cpanel.yml`, `.gitattributes`.

**`.claude/worktrees/` holds three complete stale copies of the tree.** Any repo-wide glob or
`find` run from here returns 4× hits — 254 `.py` files instead of the app's 117. Work inside the
project directory, not from here.

Commit messages are `area: declarative sentence` — see `git log`.
