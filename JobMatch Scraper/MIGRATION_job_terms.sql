-- JobMatch: the packed keyword analysis moves into its own table.
-- Paste into the SQL editor and run. Safe to re-run (all 'if not exists').
--
-- Phase 3b. Independent of the others; any order.
--
-- WHY THIS ONE IS ABOUT MEMORY, not disk and not really seconds. Measured against the live box
-- on 2026-09-06, the feed's own corpus select:
--
--     with jd_terms      47,133 rows   69.8 MB   45.1 s
--     without jd_terms   47,133 rows   31.1 MB   27.2 s
--
-- So the column is 55% of the bytes and 40% of the time -- and 99.5% of rows carry it. But the
-- corpus read is cached, so those seconds are paid a few times a day, not per request. What is
-- paid CONTINUOUSLY is that the same 38.7 MB sits RESIDENT in every Passenger worker, on a
-- shared account capped at ~1.2 GB in total, where memory is the binding constraint on how many
-- users this can serve.
--
-- WHAT THIS MIGRATION DOES NOT DO, and the distinction is the whole reason Phase 3 was split in
-- two. It does not change what the feed reads. db.load_jobs still returns jd_terms on every row;
-- it simply fetches it from here and merges it back, exactly as Phase 2 did for the description.
-- Making the read LAZY -- fetching terms only for the rows a user still needs scoring for, which
-- is ~23% for an account with stored scores and 100% for a fresh one -- is a change to the
-- scoring path, which is the most carefully tuned code in the app. It deserves its own change
-- and its own measurement rather than arriving as a side effect of a storage move.
--
-- n_terms is stored now, unused, for that later change: it answers "does this row have an
-- analysis, and is it thin" without reading 38.7 MB, which is what web._row_pending needs and
-- what jobs_fingerprint's third component counts. Adding the column later would leave it NULL on
-- every existing row and require its own backfill to become useful; adding it with its writer
-- costs nothing.

create table if not exists public.job_terms (
    url       text primary key,
    -- TEXT, deliberately not jsonb -- the same reasoning JOBS_DERIVED_SQL gives for the column
    -- this replaces. jsonb normalises an object and does not preserve key order, which breaks
    -- this twice: score_jobs diffs the stored value against the one it just built to decide
    -- whether to write (a reordered read re-upserts the whole corpus every run), and the key
    -- order IS analyze_jd's frozen term order, which breaks ties between equal-weight terms in
    -- the panel's skill lists. Nothing ever queries inside this column.
    jd_terms  text,
    -- Length of the packed string. NULL text and 0 mean the same thing here, and
    -- mirror_job_terms writes NULL rather than '' so `n_terms > 0` is exactly the predicate
    -- `jd_terms is not null` used to be on `jobs`.
    n_terms   integer not null default 0,
    -- Which description this analysis was read from -- the same stamp Phase 0 put on `jobs`,
    -- carried here so a reading and its provenance cannot be separated by a partial write.
    facts_fp  text,
    updated_at timestamptz not null default now()
);

-- The rows must die with the job, or every pruned posting leaves ~730 B of packed analysis
-- behind for ever. db.delete_urls only ever deletes from `jobs`.
do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'job_terms_url_fkey'
                     and conrelid = 'public.job_terms'::regclass) then
        alter table public.job_terms
            add constraint job_terms_url_fkey
            foreign key (url) references public.jobs (url) on delete cascade;
    end if;
end $$;

-- jobs_fingerprint()'s third component counts rows carrying an analysis, on every corpus
-- revalidation. It is a HEAD, so this index is what keeps it from being a sequential scan of a
-- table whose rows are mostly TOAST pointers.
create index if not exists job_terms_present_idx on public.job_terms (url) where n_terms > 0;

-- No backfill here: 38.7 MB in one statement, on a box whose Passenger workers are serving the
-- site from the same ~1.2 GB budget, is how a run gets SIGKILLed. scripts/backfill_job_terms.py
-- does it in batches and stamps data_versions['job_terms'] only after a row-for-row verify --
-- which is also the flag db.py reads before believing this table.

-- LAST. Without this PostgREST answers from its cached schema and the table reads as missing
-- until it happens to reload.
notify pgrst, 'reload schema';
