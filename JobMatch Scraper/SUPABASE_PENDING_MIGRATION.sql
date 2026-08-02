-- ============================================================================
-- JobMatch — pending Supabase migration
-- Generated from db.py (JOBS_DERIVED_SQL + APPLICATIONS_SQL) on 2026-08-01.
--
-- WHY THIS IS NEEDED. A live probe of the database found 9 columns missing from
-- `jobs` and 8 from `profiles`. Two user-visible consequences today:
--
--   1. "Save as default" on the jobs feed reports success and saves NOTHING.
--      db.save_profile() gets a 400 because `search_prefs` doesn't exist, drops the
--      migration-dependent columns, retries, and returns ok with a note about
--      work-authorization dates. The saved search is silently discarded — which also
--      means the daily email digest has no search to run.
--   2. The pay filter, remote filter and closed-posting detection are dormant, because
--      salary_*/remote/is_active are read from columns that don't exist. (Location still
--      works: web.py falls back to parsing the raw location string per row.)
--
-- HOW TO RUN: Supabase dashboard -> SQL Editor -> paste all of this -> Run.
-- Safe to run more than once: every statement is `add column if not exists`,
-- `create ... if not exists`, `create or replace`, or an `update ... where <col> is null`
-- that matches nothing on a second pass. It adds columns and fills the new one; it never
-- drops a column and never rewrites a value it did not itself just create.
--
-- AFTER RUNNING: `python -m scraper.score_jobs` backfills loc_state/loc_metro/remote/
-- salary_* for existing rows (it already computes them; the write is what was failing).
-- ============================================================================


-- 0) jobs.first_seen — when a job first entered THIS database
--
-- Distinct from found_date, which is the publisher's posting date (or, for most boards, the
-- scrape stamp we fall back to). Some employers publish no posting date ANYWHERE: Tesla's
-- /cua-api listing objects have no date field, their job-detail endpoint has none either, and
-- their job pages carry no JSON-LD. 374 Tesla rows plus a ~180-row tail therefore render with
-- no date at all AND slip through every "posted within" filter, because both the server and
-- client date filters skip rows with an empty date.
--
-- first_seen is write-once: it is set on insert and never moved afterwards, so a re-scrape or
-- a browser-extension re-import cannot make an old job look new.

alter table public.jobs add column if not exists first_seen date;

-- Backfill 1: a row that already has a date — that date is our best evidence of when we first
-- saw it. found_date is TEXT, either 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM', hence substring.
update public.jobs
   set first_seen = substring(found_date from 1 for 10)::date
 where first_seen is null
   and found_date ~ '^\d{4}-\d{2}-\d{2}';

-- Backfill 2: the rows with no date at all. We have no record of when they arrived, only that
-- it was on or before today, so today is the honest floor.
update public.jobs set first_seen = current_date where first_seen is null;

-- Default LAST. `add column ... default current_date` as a SINGLE statement would have stamped
-- all ~26,800 existing rows with today — exactly the lie this column exists to avoid.
alter table public.jobs alter column first_seen set default current_date;

-- Write-once at the database level. Belt and braces, because db._upsert() normalizes each
-- chunk to the UNION of its rows' keys (PostgREST requires uniform keys in a bulk write) and
-- fills the gaps with null — so one row carrying first_seen would make every other row in that
-- chunk send an explicit null, and merge-duplicates would then blank a date already recorded.
-- A column DEFAULT does not help there: defaults do not apply when the payload names the
-- column, even as null. These two triggers do.
create or replace function public.jobs_first_seen_set()
returns trigger language plpgsql as $$
begin
  new.first_seen := coalesce(new.first_seen, current_date);
  return new;
end $$;
drop trigger if exists jobs_first_seen_set on public.jobs;
create trigger jobs_first_seen_set before insert on public.jobs
  for each row execute function public.jobs_first_seen_set();

create or replace function public.jobs_first_seen_keep()
returns trigger language plpgsql as $$
begin
  new.first_seen := coalesce(old.first_seen, new.first_seen, current_date);
  return new;
end $$;
drop trigger if exists jobs_first_seen_keep on public.jobs;
create trigger jobs_first_seen_keep before update on public.jobs
  for each row execute function public.jobs_first_seen_keep();

-- 1) jobs: location, pay and liveness columns
-- Location + pay + liveness, derived from data already stored on each job.
alter table public.jobs add column if not exists loc_state text;
alter table public.jobs add column if not exists loc_metro text;
alter table public.jobs add column if not exists remote boolean;
alter table public.jobs add column if not exists salary_min integer;
alter table public.jobs add column if not exists salary_max integer;
alter table public.jobs add column if not exists salary_period text;
alter table public.jobs add column if not exists last_seen date;
alter table public.jobs add column if not exists is_active boolean default true;
-- consecutive successful fetches of its own board a job has been absent from
alter table public.jobs add column if not exists miss_count integer default 0;
create index if not exists jobs_loc_state_idx on public.jobs (loc_state);
create index if not exists jobs_is_active_idx on public.jobs (is_active);


-- 2) applications / resumes / profiles, incl. profiles.search_prefs
create table if not exists public.applications (
  id text primary key,
  username text not null,
  company text, title text, url text,
  status text, applied_date date,
  resume_name text, resume_used text, notes text,
  created_at timestamptz default now());
alter table public.applications add column if not exists resume_name text;
create index if not exists applications_user_idx on public.applications (username);

create table if not exists public.resumes (
  id text primary key, username text not null,
  name text, content text, created_at timestamptz default now());
create index if not exists resumes_user_idx on public.resumes (username);

create table if not exists public.profiles (
  username text primary key,
  name text, email text, phone text, location text, linkedin text,
  work_authorized text, needs_sponsorship text, default_resume text, notes text,
  updated_at timestamptz default now());
alter table public.profiles add column if not exists default_resume text;
alter table public.profiles add column if not exists first_name text;
alter table public.profiles add column if not exists last_name text;
alter table public.profiles add column if not exists pronouns text;
alter table public.profiles add column if not exists address_line1 text;
alter table public.profiles add column if not exists address_line2 text;
alter table public.profiles add column if not exists city text;
alter table public.profiles add column if not exists state text;
alter table public.profiles add column if not exists postal_code text;
alter table public.profiles add column if not exists country text;
alter table public.profiles add column if not exists github text;
alter table public.profiles add column if not exists portfolio text;
alter table public.profiles add column if not exists website text;
alter table public.profiles add column if not exists work_auth_status text;
alter table public.profiles add column if not exists requires_sponsorship_now text;
alter table public.profiles add column if not exists requires_sponsorship_future text;
alter table public.profiles add column if not exists gender text;
alter table public.profiles add column if not exists race_ethnicity text;
alter table public.profiles add column if not exists hispanic_latino text;
alter table public.profiles add column if not exists veteran_status text;
alter table public.profiles add column if not exists disability_status text;
alter table public.profiles add column if not exists desired_salary text;
alter table public.profiles add column if not exists salary_currency text;
alter table public.profiles add column if not exists available_start_date text;
alter table public.profiles add column if not exists willing_to_relocate text;
alter table public.profiles add column if not exists how_did_you_hear text;
alter table public.profiles add column if not exists program_end_date text;
alter table public.profiles add column if not exists opt_type text;
alter table public.profiles add column if not exists opt_start_date text;
alter table public.profiles add column if not exists opt_end_date text;
alter table public.profiles add column if not exists stem_eligible text;
alter table public.profiles add column if not exists unemployment_days_used text;
-- saved feed filters + email-digest opt-in (core.normalize_prefs)
alter table public.profiles add column if not exists search_prefs jsonb default '{}'::jsonb;
alter table public.profiles add column if not exists extra jsonb default '{}'::jsonb;
alter table public.profiles add column if not exists application_defaults jsonb default '{}'::jsonb;

create table if not exists public.tailored_cache (
  id text primary key, username text, data jsonb, created_at timestamptz default now());
create index if not exists tailored_cache_user_idx on public.tailored_cache (username);

create table if not exists public.learned_answers (
  username text not null, key text not null,
  label text, value text, type text, options jsonb, company text,
  count int default 1, updated_at timestamptz default now(),
  primary key (username, key));
create index if not exists learned_answers_user_idx on public.learned_answers (username);
