-- JobMatch: PROVENANCE for every reading we derive from a job description.
-- Paste into the SQL editor and run. Safe to re-run (all 'if not exists').
--
-- Also lives in db.JOBS_DERIVED_SQL, which is what score_jobs prints when a write fails because
-- these columns do not exist yet. The two must stay in step.
--
-- WHY THIS EXISTS. A description is supposed to be immutable: score_jobs queues a fetch only
-- when the `jd` column is empty, so exp_max_years / jd_terms / sponsor_jd are normally a true
-- reading of the text the row holds. On 2026-09-04 the sweep wrote 1,783 descriptions onto rows
-- the table ALREADY HELD -- that path deliberately does not gate on whether the posting is new,
-- because a slice in which every aggregator row is already known would otherwise drop the
-- descriptions it just rescued. The text was replaced. The readings were not. Nothing noticed.
--
-- What that cost, measured on this corpus two days later:
--
--     778 rows lost exp_max_years entirely, so nine postings asking 3 to 10 years turned up
--         in a "0 to 2 Years" search -- the experience filter keeps a row it has no number for,
--         because many genuine entry-level posts state none
--     117 rows kept a number that was too HIGH (typically stored 3, description says 1), so
--         genuinely entry-level jobs were hidden from that same search. Nobody reports the job
--         they never saw, which is why this half went unnoticed longer
--     ~7% of the corpus still carries jd_terms -- the input to every match score -- derived
--         from text the row no longer stores
--
-- Finding those took a 200-second scan that re-read every description over the network. The
-- point of these two columns is that the same question is now a WHERE clause.
--
-- THE PATTERN IS NOT NEW HERE. user_scores already stores resume_fp, the md5 of the resume a
-- score was computed against, and filters every read on it -- so that table may be out of date
-- but cannot serve a number computed against something else. This is the same bargain applied
-- to the other half: a reading may be old, but it can no longer lie about which document it is
-- a reading OF.

-- The fingerprint of the description this row STORES. Written by db.update_jds, in the same
-- row of the same statement as the text, so there is no window where they disagree.
alter table public.jobs add column if not exists jd_fp text;

-- The fingerprint of the text the DERIVED columns beside it were read from. Written by
-- scraper/score_jobs.py::_persist_derived, in the same payload as the readings themselves.
alter table public.jobs add column if not exists facts_fp text;

-- THE QUERY THIS IS ALL FOR:
--
--     select url from public.jobs where jd_fp is distinct from facts_fp;
--
-- `is distinct from`, not `<>`. Both columns are NULL for a row we hold no description for --
-- that row has no reading to be stale, and `<>` would answer NULL and quietly drop it from
-- either side of the comparison. Both NULL must read as agreement.
--
-- PARTIAL, because that disagreement is the only query anyone runs against this pair and on a
-- healthy corpus it matches almost nothing. A plain index on two 32-char columns across 47k
-- rows would cost ~3 MB to answer a question about a handful of them.
create index if not exists jobs_facts_stale_idx on public.jobs (url)
  where jd_fp is distinct from facts_fp;

-- BACKFILL IS DELIBERATELY NOT DONE HERE, and that is the point rather than an omission.
--
-- There is no way to compute facts_fp for an existing row from inside the database: it is a
-- claim about which text produced a reading, and for rows already in the table that fact was
-- never recorded. Stamping both columns from the current `jd` would assert the readings are
-- current, which is exactly the false claim this migration exists to make impossible -- and it
-- would mark the ~2,300 known-bad rows as clean.
--
-- So both columns start NULL and fill in truthfully as rows are re-read:
--   * jd_fp    on the next write of that row's description (db.update_jds)
--   * facts_fp on the next scoring pass that analyses it (_persist_derived)
-- A full pass (`python -m scraper.score_jobs`) fills every readable row in one go.
--
-- Until then a NULL facts_fp means "we do not know what this reading is about", which is the
-- honest state and the one scripts/check_derived.py reports separately from a real mismatch.

-- LAST. Without this PostgREST answers from its cached schema and both columns above read as
-- missing until it happens to reload.
notify pgrst, 'reload schema';
