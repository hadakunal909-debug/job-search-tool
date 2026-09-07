-- MIGRATION_contract.sql -- the contract half of expand/contract: drop the duplicated columns.
--
-- Every value these eleven columns hold is already stored in the table that now owns it, and has
-- been read from there since the stamps went in. Until this runs, each one exists TWICE -- which
-- is the duplicate-column arrangement the whole separation was a response to. Two columns that
-- can disagree is a bug generator; the mirror keeps them in step today only because every writer
-- happens to go through db.update_job_fields, and nothing forces a future one to.
--
-- THIS IS THE ONLY IRREVERSIBLE STEP IN THE WHOLE REVAMP. Everything before it degrades: unstamp
-- a gate and the app reads the old columns again. After this there is no fallback, which is why
-- it was held until a real scrape -> score -> mirror cycle had run end to end (it did, on
-- 2026-09-06: 41 rows, every table +41, 0 columns disagreeing).
--
-- RUN THE CODE FIRST. This is the REVERSE of the order the expand phase used. Going in, the DDL
-- had to land before the deploy so the new tables existed; coming out, the deploy has to land
-- before the DDL, because the running app still selects these columns until it does. The
-- deployed commit must contain db.MOVED_OFF_JOBS and the retired gates.
--
-- WHAT STAYS, and it is deliberate: jd_fp and facts_fp both remain on `jobs`. The staleness query
-- is `where jd_fp is distinct from facts_fp`, pgrest.py translates no joins, and splitting the
-- pair would turn a one-table comparison into something this stack cannot express. facts_fp is
-- written to both tables for that reason.
--
-- NO INDEXES HERE, though the plan listed them. job_facts(exp_max_years), (loc_state, remote) and
-- (salary_min) only pay off once something QUERIES by them, and nothing does: the feed loads the
-- corpus once and filters in Python. Adding index maintenance for a reader that does not exist is
-- a cost with no return. They belong with the DB-fallback read path, whenever that is built.

-- ---------------------------------------------------------------------------------------------
-- THE GUARD. A migration that drops data should refuse rather than trust the operator's memory of
-- whether the backfills finished. Each check is the exact question the corresponding backfill's
-- --verify answers, asked again at the moment of the drop.
-- ---------------------------------------------------------------------------------------------
do $$
declare
  missing bigint;
begin
  select count(*) into missing
    from public.jobs j
    left join public.job_facts f on f.url = j.url
   where f.url is null;
  if missing > 0 then
    raise exception 'REFUSING TO DROP: % job row(s) have no job_facts row. '
                    'Run scripts/backfill_job_facts.py --apply until it reports 0 remaining.',
                    missing;
  end if;

  select count(*) into missing
    from public.jobs j
   where j.jd is not null and j.jd <> ''
     and not exists (select 1 from public.job_descriptions d where d.url = j.url);
  if missing > 0 then
    raise exception 'REFUSING TO DROP: % row(s) hold a description that is not in '
                    'job_descriptions. Run scripts/backfill_job_descriptions.py --apply.',
                    missing;
  end if;

  select count(*) into missing
    from public.jobs j
   where j.jd_terms is not null and j.jd_terms <> ''
     and not exists (select 1 from public.job_terms t where t.url = j.url);
  if missing > 0 then
    raise exception 'REFUSING TO DROP: % row(s) hold an analysis that is not in job_terms. '
                    'Run scripts/backfill_job_terms.py --apply.', missing;
  end if;
end $$;

-- ---------------------------------------------------------------------------------------------
-- The drops. `if exists` so this is re-runnable, and one statement per column so a failure names
-- the column it failed on.
-- ---------------------------------------------------------------------------------------------
alter table public.jobs drop column if exists jd;
alter table public.jobs drop column if exists jd_terms;
alter table public.jobs drop column if exists loc_state;
alter table public.jobs drop column if exists loc_metro;
alter table public.jobs drop column if exists remote;
alter table public.jobs drop column if exists salary_min;
alter table public.jobs drop column if exists salary_max;
alter table public.jobs drop column if exists salary_period;
alter table public.jobs drop column if exists exp_max_years;
alter table public.jobs drop column if exists sponsor_jd;
alter table public.jobs drop column if exists sponsor_reason;

-- What is left, so the run is not taken on trust.
select string_agg(column_name, ', ' order by ordinal_position) as jobs_columns_now
  from information_schema.columns
 where table_schema = 'public' and table_name = 'jobs';

-- DROP COLUMN IS METADATA-ONLY IN POSTGRES. The heap still holds every dropped value; the space
-- comes back on a rewrite. That is `vacuum full public.jobs`, which takes an ACCESS EXCLUSIVE
-- lock (the site stalls for its duration) and cannot run inside a transaction block -- so it is
-- NOT in this file, which is meant to be run with -1. Run it separately, and only once this has
-- been verified. Measured before: jobs is 332 MB, of which 200 MB is the description TOAST.

notify pgrst, 'reload schema';
