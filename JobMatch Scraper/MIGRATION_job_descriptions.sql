-- JobMatch: the job description moves into its own table.
-- Paste into the SQL editor and run. Safe to re-run (all 'if not exists').
--
-- Phase 2 of the schema separation. Independent of Phase 0 and Phase 1; any order.
--
-- THIS IS A GUARDRAIL, NOT AN OPTIMISATION, and the difference is worth stating because the
-- obvious reading of "the descriptions are 85% of the table" is wrong. Measured 2026-09-06:
--
--     jobs, total          265 MB   of which TOAST   200 MB
--     jd column            263 MB   median 5,991 chars, p90 8,000
--     everything else       ~11 MB
--
-- Postgres ALREADY stores a value that large out of line and does not read it unless you select
-- it. `select url, title from jobs` has never touched the description. So the storage layer has
-- been doing this split all along, and moving the column saves no disk and no query time.
--
-- What it changes is what a MISTAKE costs. db.load_jobs(include_jd=True) sends `select=*`, and
-- three things route into it: the scorer's full pass (which genuinely wants every description,
-- to build IDF), a caller that forgets `cols=`, and -- until it was fixed the same day this was
-- written -- any narrow `cols=` read whose select failed, because the fallback path did not
-- clear include_jd. That last one spent ~300 MB of a 5 GB monthly egress budget twice in one
-- afternoon, from a caller that had asked for six columns.
--
-- With the text in its own table, `select=*` on jobs CANNOT return a description. The class of
-- accident stops being a discipline problem and becomes an impossible one.
--
-- WHAT IS NOT HERE, deliberately:
--   * jd_fp stays on `jobs`, beside facts_fp. It is not metadata about the text, it is one half
--     of a COMPARISON with the other -- `where jd_fp is distinct from facts_fp` -- and pgrest.py
--     translates no joins, so splitting the pair would turn a single-table read into two reads
--     and a merge to answer the question Phase 0 exists to make cheap.
--   * host_verdict. It lives in a KV blob today and moving it is a separate, smaller job; an
--     unused column is clutter that later reads as a feature someone forgot to finish.

create table if not exists public.job_descriptions (
    url        text primary key,
    -- Capped at db.JD_MAX_CHARS (8,000) by the writer, not by a constraint here: the cap is a
    -- policy about how much of a posting is worth storing, and a database-level truncation would
    -- silently disagree with the fingerprint, which hashes exactly what update_jds sends.
    jd         text,
    -- Length WITHOUT reading the text. The thin-JD machinery (core._MIN_JD_CHARS,
    -- score_jobs._is_thin_jd, refetch_thin_jds) currently answers "is this a shell rather than a
    -- posting" by pulling every description it might be true of. This makes that a number.
    jd_chars   integer,
    fetched_at timestamptz,
    updated_at timestamptz not null default now()
);

-- THE ROWS MUST DIE WITH THE JOB. db.delete_urls deletes from `jobs` alone and prune_old_jobs
-- runs a 30-day window, so without this every pruned posting leaves its description behind for
-- ever -- in the one table where that costs 5.8 KB a row rather than a few bytes. There is no
-- cleanup call to remember because there is no cleanup call: the constraint is the mechanism.
-- user_scores learned this the same way; see MIGRATION_user_scores.sql.
--
-- DO block because Postgres has no ADD CONSTRAINT IF NOT EXISTS and this file must stay
-- re-runnable. It will FAIL if orphans exist; delete them first:
--   delete from public.job_descriptions d
--    where not exists (select 1 from public.jobs j where j.url = d.url);
do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'job_descriptions_url_fkey'
                     and conrelid = 'public.job_descriptions'::regclass) then
        alter table public.job_descriptions
            add constraint job_descriptions_url_fkey
            foreign key (url) references public.jobs (url) on delete cascade;
    end if;
end $$;

-- urls_missing_jd() and urls_with_jd() are the fetch queue and its complement, asked on every
-- scrape over the whole corpus. Partial so it indexes only the rows that HAVE text -- the
-- complement is derived by set difference against `jobs`, which the scraper already holds.
create index if not exists job_descriptions_present_idx
    on public.job_descriptions (url) where jd is not null and jd <> '';

-- NO BACKFILL HERE. Copying 263 MB inside one statement on a shared box with a ~1.2 GB
-- account-wide memory cap is the kind of thing that gets a Passenger worker killed while it is
-- serving the site. scripts/backfill_job_descriptions.py does it in batches, resumably, and
-- stamps data_versions['job_descriptions'] only when every row has landed -- which is also the
-- flag db.py reads to decide whether this table may be trusted as the source. Until that stamp
-- exists, jobs.jd remains authoritative and this table is a shadow copy.

-- LAST. Without this PostgREST answers from its cached schema and the table reads as missing
-- until it happens to reload.
notify pgrst, 'reload schema';
