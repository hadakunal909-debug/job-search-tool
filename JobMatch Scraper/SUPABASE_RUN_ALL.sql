-- ============================================================================
-- JobMatch admin panel — EVERYTHING, phases 1 to 4, in the order it must run.
--
-- HOW TO RUN
--   Supabase -> your project -> SQL Editor -> New query -> paste ALL of this -> Run.
--   One paste, one click. It is safe to re-run: every statement is `if not exists`,
--   `create or replace`, or guarded by a catalog lookup.
--
-- WHAT IT TOUCHES
--   Adds : blocked_companies, admin_audit, events, events_daily
--   Adds : users.disabled_at, users.token_epoch, applications.match_score,
--          user_jobs.updated_at
--   Adds : db_stats(), ev_usage() functions; 4 indexes; 5 foreign keys
--   DELETES: orphaned rows in section 3 — rows belonging to accounts that no longer
--            exist. See the note there before you run it if that worries you.
--   Nothing else is modified. No job rows are touched.
--
-- ORDER MATTERS in exactly two places:
--   * Section 3 (orphan cleanup) MUST precede section 4 (foreign keys) — the FK
--     creation FAILS while orphaned rows exist.
--   * The final `notify pgrst` must be last, or PostgREST keeps serving the old
--     schema and the new columns/functions read as missing for a while.
--
-- AFTER RUNNING: reload /admin/data and /admin/usage. Both should switch from
-- "run this migration" banners to real numbers.
-- ============================================================================


-- ############################################################################
-- PHASE 1 — database size reporting
-- Powers the storage tiles on /admin/data. PostgREST cannot run arbitrary SQL,
-- so Postgres' own size functions are only reachable through a stored function.
-- ############################################################################

create or replace function public.db_stats()
returns json
language sql
security definer
set search_path = public, pg_catalog
as $$
  select json_build_object(
    'db_bytes',    pg_database_size(current_database()),
    'db_pretty',   pg_size_pretty(pg_database_size(current_database())),
    'measured_at', now(),
    'tables', (
      -- NOTE the ordering lives INSIDE json_agg. Sorting the rendered JSON instead would
      -- compare total_bytes as text and put 9 MB above 400 MB.
      select coalesce(json_agg(json_build_object(
               'table',       x.relname,
               'est_rows',    x.reltuples::bigint,
               'total_bytes', x.total_bytes,
               'table_bytes', x.table_bytes,
               'index_bytes', x.index_bytes,
               'toast_bytes', x.toast_bytes,
               'pretty',      pg_size_pretty(x.total_bytes)
             ) order by x.total_bytes desc), '[]'::json)
      from (
        select c.relname, c.reltuples,
               pg_total_relation_size(c.oid)                        as total_bytes,
               pg_table_size(c.oid)                                 as table_bytes,
               pg_indexes_size(c.oid)                               as index_bytes,
               coalesce(pg_total_relation_size(c.reltoastrelid), 0) as toast_bytes
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relkind = 'r'
      ) x)
  );
$$;

revoke all on function public.db_stats() from public;
grant execute on function public.db_stats() to anon, authenticated, service_role;


-- ############################################################################
-- PHASE 2 — user management
-- ############################################################################

-- --- 2. Accounts: disable + a revocable extension token --------------------
-- disabled_at is a nullable timestamp rather than a boolean so it records WHEN,
-- which is the question you actually ask when an account stops working.
alter table public.users add column if not exists disabled_at timestamptz;

-- token_epoch is folded into the extension token's HMAC message (web._ext_token).
-- Incrementing it invalidates every token that user has ever been issued — the only
-- revocation available, since those tokens are derived rather than stored.
-- Epoch 0 deliberately keeps the ORIGINAL message shape, so running this does NOT
-- break the tokens already pasted into installed extensions.
alter table public.users add column if not exists token_epoch integer not null default 0;


-- --- 3. Orphan cleanup — MUST run before section 4 -------------------------
-- db.delete_user() historically removed only user_jobs + users, so every other
-- username-keyed table can hold rows belonging to accounts that no longer exist.
-- The foreign keys in section 4 REFUSE TO BE CREATED while such rows are present,
-- so this is a prerequisite, not housekeeping.
--
-- TO PREVIEW INSTEAD OF DELETING, run these first:
--   select 'user_jobs' t, count(*) from public.user_jobs
--     where username not in (select username from public.users)
--   union all select 'profiles', count(*) from public.profiles
--     where username not in (select username from public.users)
--   union all select 'applications', count(*) from public.applications
--     where username not in (select username from public.users)
--   union all select 'resumes', count(*) from public.resumes
--     where username not in (select username from public.users)
--   union all select 'learned_answers', count(*) from public.learned_answers
--     where username not in (select username from public.users);
--
-- (On 2026-08-08 the health check found exactly one such row, in learned_answers.)
delete from public.user_jobs       where username not in (select username from public.users);
delete from public.profiles        where username not in (select username from public.users);
delete from public.applications    where username not in (select username from public.users);
delete from public.resumes         where username not in (select username from public.users);
delete from public.learned_answers where username not in (select username from public.users);

-- tailored_cache is deliberately NOT cleaned by username and gets no foreign key:
-- db.put_tailored() writes username='' for anonymous entries, which no FK tolerates
-- and which the query above would wrongly delete. It is pruned by age instead.


-- --- 4. Cascade deletes ----------------------------------------------------
-- Makes the cleanup structural, so a delete done from the Supabase dashboard or
-- `manage_users.py remove` is as complete as one done through the admin panel.
-- db.delete_user() still deletes children explicitly — that keeps it correct before
-- this migration is run, on the local-file backend, and for its dry-run counts.
do $$
declare
  t text;
begin
  foreach t in array array['user_jobs', 'profiles', 'applications', 'resumes', 'learned_answers']
  loop
    if not exists (select 1 from pg_constraint where conname = t || '_username_fkey') then
      execute format(
        'alter table public.%I add constraint %I foreign key (username) '
        'references public.users(username) on delete cascade',
        t, t || '_username_fkey');
    end if;
  end loop;
end $$;


-- ############################################################################
-- PHASE 3 — deleting jobs, the company blocklist, and the audit trail
-- ############################################################################

-- --- 5. Company blocklist --------------------------------------------------
-- Deleting a company's jobs does not stick on its own: the scraper runs twice every
-- weekday and puts them straight back. Both ingestion paths (the scraper and the
-- extension's bulk import) consult this table on every run.
--
-- name_key is db.block_key(name): lowercased, punctuation collapsed. Company
-- suffixes are deliberately NOT stripped — reducing "Apple Inc" to "apple" would
-- also block "Apple Hospitality", which is a different employer.
create table if not exists public.blocked_companies (
  name_key   text primary key,
  name       text not null,          -- as it appeared, for display
  reason     text,
  added_by   text,
  created_at timestamptz default now()
);


-- --- 6. Admin audit trail --------------------------------------------------
-- Written BEFORE a destructive action starts, so the intent survives a process that
-- dies mid-batch, then updated with the row count afterwards.
create table if not exists public.admin_audit (
  id         text primary key,       -- uuid4 hex, generated app-side
  at         timestamptz default now(),
  actor      text not null,
  action     text not null,          -- jobs.delete | company.delete | company.block | ...
  target     text,
  count      integer default 0,
  -- Holds a small sample only. Storing every deleted URL would make the audit table
  -- the storage problem it exists to help you watch.
  detail     jsonb default '{}'::jsonb
);
create index if not exists admin_audit_at_idx on public.admin_audit (at desc);


-- --- 7. Index for company-scoped counts and deletes ------------------------
-- The delete preview counts and then removes by company; without this each is a
-- sequential scan of ~19k rows.
create index if not exists jobs_company_idx on public.jobs (company);


-- ############################################################################
-- PHASE 4 — product analytics
--
-- Size: ~350 bytes/event including indexes, ~20k events/month at 3 active users,
-- so with the 90-day prune (scripts/ev_maintain.py) the table plateaus around
-- 60k rows / 21 MB — about 4% of the 500 MB free tier, and small next to `jobs`.
--
-- Event history STARTS when you run this and cannot be backfilled.
-- ############################################################################

-- --- 8. Raw events ---------------------------------------------------------
create table if not exists public.events (
  id        bigserial   primary key,
  ts        timestamptz not null default now(),
  username  text        not null,
  sid       text        not null,     -- 30-minute session id, minted server-side
  event     text        not null,
  -- company / source / score are DENORMALISED on purpose. The scraper prunes
  -- `jobs` at 30 days, so job_url becomes a dangling reference and a join back to
  -- jobs returns nothing for anything merely viewed. score is worse: it is
  -- rewritten on every scoring run AND deleted with the row, so it cannot be
  -- recovered later at any price. Capture at event time or lose it.
  -- Consequence: never join events to jobs. These columns exist so you don't.
  job_url   text,
  company   text,
  source    text,                     -- ATS host
  score     smallint,                 -- match score AS SHOWN, at click time
  props     jsonb       not null default '{}'::jsonb
);

-- Only two indexes beyond the PK, deliberately. At ~60k rows the heap is ~15 MB and
-- a sequential scan is tens of milliseconds; a third index would cost ~30 B a row
-- for no measurable gain. Add more only when a query is MEASURED slow.
create index if not exists events_ts_idx      on public.events (ts desc);
create index if not exists events_user_ts_idx on public.events (username, ts desc);

-- No foreign keys, for the same pruning reason: a deleted job or a deleted account
-- must not cascade away the history you are trying to analyse.


-- --- 9. Daily rollup — kept forever, raw events are not --------------------
-- Counts only: no URLs, no search text, no session ids. That is what makes it safe
-- to keep indefinitely once the raw rows age out at 90 days.
create table if not exists public.events_daily (
  day      date    not null,
  username text    not null,
  event    text    not null,
  dim      text    not null default '',
  n        integer not null default 0,
  primary key (day, username, event, dim)
);


-- --- 10. The aggregation RPC ----------------------------------------------
-- /admin/usage must never page the events table over the wire — PostgREST returns
-- 1000 rows a request, and 60k rows is 60 sequential round trips from a shared
-- cPanel box. The group-bys happen here and one small JSON object comes back.
create or replace function public.ev_usage(days int default 7)
returns jsonb
language sql
stable
as $$
  with w as (
    select * from public.events where ts > now() - (days || ' days')::interval
  )
  select jsonb_build_object(
    'events',   (select count(*) from w),
    'sessions', (select count(distinct sid) from w),
    'users',    (select count(distinct username) from w),
    'by_event', (select coalesce(jsonb_object_agg(event, n), '{}'::jsonb)
                 from (select event, count(*) n from w group by 1) x),
    'by_route', (select coalesce(jsonb_agg(jsonb_build_object('ep', ep, 'n', n) order by n desc), '[]'::jsonb)
                 from (select props->>'ep' ep, count(*) n from w
                       where event = 'page_view' and props ? 'ep' group by 1) x),
    -- The funnel. `shown` sums the card count each feed render reported, which is
    -- the only impression denominator that exists — per-job impressions would be
    -- ~60 rows a render and are deliberately not stored.
    'funnel',   (select jsonb_build_object(
                   'shown',   coalesce(sum((props->>'n')::int) filter (where event = 'feed_view'), 0),
                   'opens',   count(*) filter (where event = 'job_open'),
                   'likes',   count(*) filter (where event = 'action' and props->>'to' = 'liked'),
                   'applies', count(*) filter (where event = 'action' and props->>'to' = 'applied'),
                   'hides',   count(*) filter (where event = 'action' and props->>'to' = 'hidden'))
                 from w),
    -- Score-band calibration: opens and applies per 20-point band. If this curve is
    -- flat, the match score is noise and the min-score slider is a placebo.
    'by_score', (select coalesce(jsonb_agg(jsonb_build_object('band', b, 'opens', o, 'applies', a) order by b), '[]'::jsonb)
                 from (select (least(score, 100) / 20) b,
                              count(*) filter (where event = 'job_open') o,
                              count(*) filter (where event = 'action' and props->>'to' = 'applied') a
                       from w where score is not null group by 1) s),
    'top_co',   (select coalesce(jsonb_agg(jsonb_build_object('co', company, 'n', n) order by n desc), '[]'::jsonb)
                 from (select company, count(*) n from w
                       where event = 'action' and props->>'to' in ('liked', 'applied')
                         and company is not null and company <> ''
                       group by 1 order by 2 desc limit 15) c),
    -- Which of the 12 saved filters anyone actually touches. props.f holds only the
    -- keys that differ from core.DEFAULT_PREFS, so this counts real usage.
    'filters',  (select coalesce(jsonb_object_agg(k, n), '{}'::jsonb)
                 from (select k, count(*) n from w, lateral jsonb_object_keys(coalesce(props->'f', '{}'::jsonb)) k
                       where event = 'feed_view' group by 1) f),
    -- Searches that found nothing: the best available list of what the corpus is
    -- missing. Raw text is stored ONLY in this case (see analytics.py).
    'zero_q',   (select coalesce(jsonb_agg(jsonb_build_object('q', q, 'n', n) order by n desc), '[]'::jsonb)
                 from (select props->>'q' q, count(*) n from w
                       where event = 'feed_view' and (props->>'n')::int = 0 and props ? 'q'
                       group by 1 order by 2 desc limit 20) z)
  );
$$;

revoke all on function public.ev_usage(int) from public;
grant execute on function public.ev_usage(int) to anon, authenticated, service_role;


-- --- 11. Two tiny columns that outlive the 90-day event retention ----------
-- The score a job had when someone applied to it. `applications` had no score column
-- and jobs.match_score is rewritten every scoring run and deleted at 30 days, so
-- without this the single most valuable question — does a high score predict an
-- application — is answerable only inside the event window.
alter table public.applications add column if not exists match_score smallint;

-- user_jobs stores current state with no timestamp at all, so there is no way to tell
-- when someone liked something. This does not recover history, but it makes recency
-- available from here on. NOTE set_user_status upserts with merge-duplicates, so a
-- bare `default now()` would NOT refresh on update — the value has to be written in
-- the payload, which db.set_user_status now does.
alter table public.user_jobs add column if not exists updated_at timestamptz default now();


-- ############################################################################
-- LAST — reload PostgREST's schema cache
-- Without this, PostgREST keeps answering from its cached schema and every new
-- column and function above reads as missing for a while.
-- ############################################################################
notify pgrst, 'reload schema';
