# Repo root — three unrelated projects live here

| Directory | What |
|---|---|
| `JobMatch Scraper/` | The Flask job board. **The active project** — it has its own `CLAUDE.md`, read that. |
| `Resume Tailoring/` | Separate, largely superseded. Has its own `web.py`; don't confuse the two. |
| `USCIS H-1B Data Hub/` | Bulk data export, gitignored. |

Root-only files: `.github/workflows/` (**6** workflows — `scrape.yml`, `scrape-watchdog.yml`,
`python-tests.yml`, `web-build.yml`, `jobspy-sweep.yml`, `jobspy-shadow.yml`), `.cpanel.yml`,
`.gitattributes`.

**`.claude/worktrees/` holds three complete stale copies of the tree.** Any repo-wide glob or
`find` run from here returns 4× hits — 280 `.py` files instead of the app's 142. Work inside the
project directory, not from here.

Commit messages are `area: declarative sentence` — see `git log`.
