# Job-Search Tool — Architecture & Data Flow

Two scripts produce **`jobs.csv`**; the Streamlit app only reads it. No paid jobs API, no database — everything is file-based.

**Run order:** `python scraper.py` → `python score_jobs.py` → `streamlit run app.py`

```mermaid
flowchart TB

subgraph C["① COLLECT — scraper.py"]
  direction TB
  SRC["SOURCES: 26 verified company boards<br/>(company, ats_type, url)"]
  API["ATS public JSON APIs<br/>Greenhouse · Lever · Ashby · SmartRecruiters"]
  FILT["Filters<br/>dedup by URL · entry-level title (INCLUDE/EXCLUDE)<br/>· US location · H1B flag"]
  SRC --> API --> FILT
end

CSV[("jobs.csv<br/>title, company, location, url, sponsors_h1b")]
FILT --> CSV

subgraph S["② SCORE — score_jobs.py"]
  direction TB
  GETJD["Fetch full job description<br/>from the same ATS APIs"]
  SKILL["skill_match(resume, JD)<br/>= % of the job's skills your resume covers"]
  GETJD --> SKILL
end
RESUME["resume.txt"] --> SKILL
CSV --> GETJD
SKILL --> CSV2[("jobs.csv + match_score")]

subgraph D["③ DISPLAY — app.py (Streamlit)"]
  direction TB
  FEED["Card feed<br/>match ring · H1B badge · logo<br/>sort: Best match · search/filter/tabs"]
  TAILOR["Tailor view (per job)<br/>live JD · skill gaps · AI tailor · download"]
  FEED --> TAILOR
end
CSV2 --> FEED

subgraph SP["sponsor data (optional) — build_sponsors.py"]
  direction TB
  DOL["DOL H1B disclosure data (.xlsx)"] --> SPT["sponsors.txt"]
end
SPT -.-> FILT

YOU(["YOU<br/>choose boards · edit resume · like / hide / apply"])
YOU -.-> SRC
YOU -.-> RESUME
YOU -.-> FEED
```

## Stages

1. **COLLECT — `scraper.py`** — calls each board's public ATS JSON API, then keeps only postings that are new (deduped by URL), entry-level + on-target (title filter), US-based, and tags an H1B flag. Writes survivors to `jobs.csv`.
2. **SCORE — `score_jobs.py`** — pulls each job's full description from the APIs and scores it against `resume.txt` with `skill_match` (share of the job's skills your resume covers). Writes a `match_score` column.
3. **DISPLAY — `app.py`** — a Streamlit card feed that reads `jobs.csv`: match ring, H1B badge, logo, best-match sort, filters/tabs, and a per-job tailor view (live JD fetch, skill gaps, optional AI tailoring, download).

Solid arrows = data flow · dashed = your inputs / optional pieces.
