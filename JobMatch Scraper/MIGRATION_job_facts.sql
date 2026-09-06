-- JobMatch: what we DERIVED about a posting, separated from what the employer STATED.
-- Paste into the SQL editor and run. Safe to re-run (all 'if not exists').
--
-- Phase 3a. Independent of Phases 0-2; any order. Phase 3b (jd_terms into job_terms) is
-- deliberately NOT here -- see the note at the foot.
--
-- WHY. `jobs` mixes three kinds of fact and a reader cannot tell them apart. title, company,
-- location and found_date are what the EMPLOYER published. first_seen, last_seen, is_active and
-- miss_count are what WE track. And nine columns in the middle are what we DERIVED by reading
-- the description -- a reading that can be wrong, can go stale, and did both on 2026-09-04.
-- db.py::FIELDS already groups them with comments saying exactly this; the table does not.
--
-- Separating them makes the provenance of every column obvious from the table it is in, and it
-- gives the derived group somewhere to carry its own metadata: facts_fp (which text produced
-- this reading) and derived_at (when).
--
-- THIS IS THE PHASE THAT CAN GO WRONG QUIETLY, so the mechanism is worth stating. db.COLS_SCORE
-- names nine of these columns NOT because the scorer reads them but because it DIFFS against
-- them -- _persist_derived compares what it just computed with what is stored and writes only
-- the rows that changed. Its docstring is blunt about the consequence: "Drop those and every run
-- would think every derived field had changed and re-upsert the whole corpus." That failure does
-- not look like a bug. It looks like the scrape getting slower.
--
-- So the split keeps all nine reachable through db.load_jobs(cols=COLS_SCORE) whatever table
-- they physically live in, and scripts/backfill_job_facts.py --verify compares both stores row
-- for row before anything is allowed to read the new one.

create table if not exists public.job_facts (
    url            text primary key,

    -- ---- derived from the location string, and from the description for `remote` -----------
    loc_state      text,
    loc_metro      text,
    remote         boolean,

    -- ---- derived from the description ------------------------------------------------------
    salary_min     integer,
    salary_max     integer,
    salary_period  text,
    exp_max_years  integer,
    sponsor_jd     text,
    sponsor_reason text,

    -- WHICH TEXT these nine are a reading of. The same column Phase 0 added to `jobs`, now
    -- beside the facts it describes rather than beside the job. Both are written for now:
    -- jobs.facts_fp is what scripts/check_derived.py compares against jobs.jd_fp in a single
    -- read, and it keeps that job until the contract step drops it -- at which point
    -- check_derived reads this table instead. Expand, then contract; never both at once.
    facts_fp       text,
    derived_at     timestamptz not null default now()
);

-- The rows must die with the job. db.delete_urls deletes from `jobs` alone and prune_old_jobs
-- runs a 30-day window; without this every pruned posting leaves its readings behind for ever.
-- user_scores and job_descriptions learned this the same way.
--
-- DO block because Postgres has no ADD CONSTRAINT IF NOT EXISTS. It will FAIL if orphans exist:
--   delete from public.job_facts f
--    where not exists (select 1 from public.jobs j where j.url = f.url);
do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'job_facts_url_fkey'
                     and conrelid = 'public.job_facts'::regclass) then
        alter table public.job_facts
            add constraint job_facts_url_fkey
            foreign key (url) references public.jobs (url) on delete cascade;
    end if;
end $$;

-- THE FEED FILTERS ON THESE, which `jobs` never had indexes for: it carries two
-- (loc_state, is_active) for a feed that filters on ten-plus attributes. These are the columns
-- _filter_rows actually compares, and they matter now in a way they did not before -- the plan's
-- DB-fallback feed path pushes exactly these comparisons into the WHERE clause.
create index if not exists job_facts_exp_idx    on public.job_facts (exp_max_years);
create index if not exists job_facts_loc_idx    on public.job_facts (loc_state, remote);
create index if not exists job_facts_salary_idx on public.job_facts (salary_min);

-- NOT HERE, AND ON PURPOSE:
--
--   * jd_terms. It is 33 MB and the feed reads it corpus-wide -- web.job_analysis unpacks it off
--     every built row to score postings user_scores has no entry for. Moving it changes what the
--     FEED reads, not just where a writer writes, and that interacts with score_pending and the
--     stored per-user scores. A separate phase with its own verification, not a passenger on
--     this one.
--   * exp_src, roles, track, intern, jd_admit. Those are computed per request today and nothing
--     writes them; adding the columns now would ship five that stay NULL and later read as
--     something someone forgot to finish. They arrive with their writer, in Phase 4.
--   * No backfill. scripts/backfill_job_facts.py does it in batches and stamps
--     data_versions['job_facts'] only when a row-for-row verify passes -- which is also the flag
--     db.py reads to decide whether this table may be trusted. Until then `jobs` is authoritative
--     and this is a shadow copy.

-- LAST. Without this PostgREST answers from its cached schema and every column above reads as
-- missing until it happens to reload.
notify pgrst, 'reload schema';
