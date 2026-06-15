"""
resume_brain — the self-training résumé-tailoring brain, merged into JobMatch.

The deterministic core (analyze/match/research) reuses JobMatch's `core.py` (keyword/IDF) and
stores per-user data (résumés via `db.resumes`, stories/lessons/model via `db.brain_kb`) plus
shared company research (`db.brain_companies`). The optional AI layer (`ai.py`, BYO Gemini key)
turns the plan into a finished résumé + cover letter; `export.py` writes .docx.

web.py drives it via brain.run_tailor / brain.apply_feedback and the KB helpers here.
"""
