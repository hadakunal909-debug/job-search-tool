# Resume Brain — a self-training, AI-free résumé-tailoring brain

A standalone Flask app that learns. It does **not** use an LLM for its core: research,
matching, and learning are deterministic, self-training **code**. An **optional** AI rewrite
layer (BYO Gemini key) can turn the plan into finished prose + a cover letter, and any output
exports to **.docx** — but the brain works fully without a key.

## What it does
- **Teach** — your knowledge base: many **résumés**, many **stories** (STAR experiences),
  and **lessons**. The more you add, the smarter it gets.
- **Tailor** — paste a job (or a job link) + a company. The brain:
  - reads the JD: keywords, requirements, and its writing **"symphony"** (tone, voice, verbs),
  - **researches the company itself** — crawls its site (about/values/culture/careers), SSRF-safe
    and robots-aware, and extracts stated values + what they're looking for (accumulated under
    **Companies**),
  - **matches** your résumés + stories to the job and tells you: best-matching résumé, the
    stories to feature (and why), keyword gaps, phrasing to mirror, and culture fit.
- **Learn** — check the stories you'll use and add a note ("lead with the automation project");
  it becomes a durable **lesson** and reinforces which stories fit which jobs. Future tailors
  get better automatically (self-training: IDF corpus + term→story association weights).
- **Write it for me (optional AI)** — on the result page, paste your Gemini key once (kept in the
  session cookie only, never on disk) and it renders a finished tailored **résumé + cover letter**
  grounded in the brain's plan and your real material. Edit inline, then **Download .docx**.

> The self-training core is AI-free: it always gives a tailoring **plan + draft guidance**. The
> AI layer is purely additive prose-writing on top of that plan — never deciding relevance.

## Run locally
```
pip install -r requirements.txt
python web.py            # http://127.0.0.1:5055
```
1. **Teach** → add a résumé or two + several stories.
2. **Tailor** → company + paste a JD → it researches the company and shows the plan.
3. Check the stories you'll use + add feedback → **Save what I taught**. It learns.

Everything is stored locally under `data/` (JSON, gitignored) — nothing leaves your machine
except the company-website fetches during research.

## cPanel (Passenger, Python 3.9) — second Python app
Runs alongside JobMatch (e.g. a subdomain `tailor.astrochakra.co`):
1. cPanel → **Setup Python App** → Python 3.9, app root e.g. `ResumeBrain`, startup
   `passenger_wsgi.py`, entry `application`.
2. Upload this folder's files (keep the layout).
3. **Environment variables**: `APP_SECRET` = long random string; `SESSION_COOKIE_SECURE` = `1`.
4. **Configuration files** = `requirements.txt` → Run Pip Install → **Restart**.

### Notes
- No API key needed for the core. The optional AI rewrite is **BYO Gemini key** — set
  `GEMINI_API_KEY` as an env var, or just paste it on the result page (session-only).
- `data/` persists across restarts on cPanel (back it up if you rebuild). It holds your
  résumés/stories/lessons/companies and the learned model.
- All company/JD URL fetching is SSRF-guarded (`safefetch.py`): private/loopback/metadata IPs
  blocked, redirects re-validated, bodies capped at 5 MB; robots.txt honored. The Gemini calls
  hit a fixed Google host (not user input), so they bypass that guard by design.

## Files
- `web.py` routes · `brain.py` pipeline · `analyze.py` JD analysis + symphony ·
  `match.py` self-training relevance · `research.py` self-directed company crawler ·
  `ats.py` keyword/IDF · `store.py` knowledge base · `safefetch.py` SSRF guards ·
  `gemini.py` optional AI rewrite (BYO key) · `export.py` .docx builder (python-docx) ·
  `templates/` · `static/`.

## Roadmap
- Multiple-résumé auto-assembly; richer .docx templating (current export is clean but plain).
- Guided interview to grow the knowledge base (stories/lessons) faster.
- Optional bridge to pull JDs straight from JobMatch's Supabase.
