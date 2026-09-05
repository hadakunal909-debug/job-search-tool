-- JobMatch: the per-(user, job) match score, STORED rather than recomputed per request.
-- Paste into the SQL editor and run. Safe to re-run (all 'if not exists').
--
-- WHY A TABLE AND NOT A CACHE. The score was computed on demand and kept in
-- score_cache/*.json.gz, one file per (user, resume, corpus fingerprint). That is fast and it is
-- correct, but it is PER PROCESS ON DISK: a restarted app, a new Passenger worker, or a laptop
-- with a stale snapshot all start from nothing, and the reader sees "Not scored" for a job the
-- database could already answer for. Stored here, one number is written once and every worker,
-- every page and every device reads the same one.
--
-- WHY resume_fp IS NOT OPTIONAL. A stored score is a claim about a SPECIFIC resume. Edit your
-- Resume Brain and all 47,845 of your rows are about a document that no longer exists -- and
-- unlike a cache keyed on the resume's hash, a plain table has no way to notice. So the hash it
-- was scored against is stored WITH it, and every read filters on the caller's current hash. A
-- row scored against an older resume simply does not come back, the reader treats it as missing,
-- and falls back to computing. That is the whole safety property: this table can be out of date,
-- but it cannot silently serve a number computed against something else.
--
-- The job side is the writer's job: when the scoring pass rewrites a job's jd_terms, the analysis
-- the score was derived from has changed, so it deletes that job's rows on the way past.

create table if not exists public.user_scores (
    username   text        not null,
    url        text        not null,
    score      integer     not null,
    -- md5 of the profile text this score was computed against. See above; this is the
    -- correctness mechanism, not metadata.
    resume_fp  text        not null,
    updated_at timestamptz not null default now(),
    primary key (username, url)
);

-- THE READ. The feed needs one user's whole corpus at once to rank it, filtered on the profile
-- in play -- so this covers the only query the app makes, and `score` is included so it is an
-- index-only scan rather than 47,845 heap fetches.
create index if not exists user_scores_user_fp
    on public.user_scores (username, resume_fp) include (url, score);

-- THE INVALIDATION. The scoring pass deletes by url when a job's analysis changes, across every
-- user at once. Without this that is a sequential scan of the whole table per re-analysed job.
create index if not exists user_scores_url on public.user_scores (url);

-- THE ROWS HAVE TO DIE WITH THE JOB. db.delete_urls deletes from `jobs` and nothing else, and
-- prune_old_jobs runs on a 30-day window -- so without this every pruned posting leaves one dead
-- row per user behind, for ever, in a table that is already 35% of the database. There is no
-- cleanup call to forget because there is no cleanup call: the constraint is the mechanism.
--
-- The insert side of it is a feature, not a cost. A score for a job that is not in `jobs` is a
-- claim about nothing, and db.save_user_scores now drops such a batch rather than failing the
-- run -- that is the corpus-read/prune race, and ON DELETE CASCADE means the row could not have
-- survived it anyway.
--
-- DO block because Postgres has no ADD CONSTRAINT IF NOT EXISTS, and this file must stay
-- re-runnable. Adding it will FAIL if orphans already exist; delete them first:
--   delete from public.user_scores s where not exists
--     (select 1 from public.jobs j where j.url = s.url);
do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'user_scores_url_fkey'
                     and conrelid = 'public.user_scores'::regclass) then
        alter table public.user_scores
            add constraint user_scores_url_fkey
            foreign key (url) references public.jobs (url) on delete cascade;
    end if;
end $$;
