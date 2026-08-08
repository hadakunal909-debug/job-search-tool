-- ============================================================================
-- JobMatch — product analytics (admin panel, Phase 4).
-- Paste into Supabase -> SQL Editor -> Run. Safe to re-run.
--
-- The app works WITHOUT this: analytics.emit() drops events when the table is
-- missing and /admin/usage renders its "today" panels from user_jobs and
-- applications as before. Running it starts the event history — which cannot be
-- backfilled, so the sooner it runs the sooner the trends exist.
--
-- Size: ~350 bytes/event including indexes, ~20k events/month at 3 active users,
-- so with the 90-day prune below the table plateaus around 60k rows / 21 MB —
-- about 4% of the 500 MB free tier, and small next to `jobs` (whose 8 KB-capped
-- jd column across ~19k rows is well over 100 MB).
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Raw events
-- ---------------------------------------------------------------------------
create table if not exists public.events (
  id        bigserial   primary key,
  ts        timestamptz not null default now(),
  username  text        not null,
  sid       text        not null,     -- 30-minute session id, minted server-side
  event     text        not null,
  -- company / source / score are DENORMALISED on purpose. The scraper prunes
  -- `jobs` at 30 days, so job_url becomes a dangling reference and a join back
  -- to jobs returns nothing for anything merely viewed. score is worse: it is
  -- rewritten on every scoring run AND deleted with the row, so it cannot be
  -- recovered later at any price. Capture at event time or lose it.
  -- Consequence: never join events to jobs. These columns exist so you don't.
  job_url   text,
  company   text,
  source    text,                     -- ATS host
  score     smallint,                 -- match score AS SHOWN, at click time
  props     jsonb       not null default '{}'::jsonb
);

-- Only two indexes beyond the PK, deliberately. At ~60k rows the heap is ~15 MB
-- and a sequential scan is tens of milliseconds; a third index would cost ~30 B
-- a row for no measurable gain. Add more only when a query is MEASURED slow.
create index if not exists events_ts_idx      on public.events (ts desc);
create index if not exists events_user_ts_idx on public.events (username, ts desc);

-- No foreign keys, for the same pruning reason: a deleted job or a deleted
-- account must not cascade away the history you are trying to analyse.


-- ---------------------------------------------------------------------------
-- 2. Daily rollup — kept forever, raw events are not
-- ---------------------------------------------------------------------------
-- Counts only: no URLs, no search text, no session ids. That is what makes it
-- safe to keep indefinitely once the raw rows age out at 90 days.
create table if not exists public.events_daily (
  day      date    not null,
  username text    not null,
  event    text    not null,
  dim      text    not null default '',
  n        integer not null default 0,
  primary key (day, username, event, dim)
);


-- ---------------------------------------------------------------------------
-- 3. The aggregation RPC
-- ---------------------------------------------------------------------------
-- /admin/usage must never page the events table over the wire — PostgREST
-- returns 1000 rows a request, and 60k rows is 60 sequential round trips from a
-- shared cPanel box. The group-bys happen in Postgres and one small JSON object
-- comes back instead.
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
    -- Score-band calibration: opens and applies per 20-point band. If this curve
    -- is flat, the match score is noise and the min-score slider is a placebo.
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
    -- Which of the 12 saved filters anyone actually touches. props.f holds only
    -- the keys that differ from core.DEFAULT_PREFS, so this counts real usage.
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


-- ---------------------------------------------------------------------------
-- 4. Two tiny columns that outlive the 90-day event retention
-- ---------------------------------------------------------------------------
-- The score a job had when someone applied to it. `applications` had no score
-- column and jobs.match_score is rewritten every scoring run and deleted at 30
-- days, so without this the single most valuable question — does a high score
-- predict an application — is answerable only inside the event window.
alter table public.applications add column if not exists match_score smallint;

-- user_jobs stores current state with no timestamp at all, so there is no way to
-- tell when someone liked something. This does not recover history, but it makes
-- recency available from here on. NOTE set_user_status upserts with
-- merge-duplicates, so a bare `default now()` would NOT refresh on update — the
-- value has to be written in the payload, which db.set_user_status now does.
alter table public.user_jobs add column if not exists updated_at timestamptz default now();


-- ---------------------------------------------------------------------------
-- 5. Reload PostgREST's schema cache
-- ---------------------------------------------------------------------------
notify pgrst, 'reload schema';
