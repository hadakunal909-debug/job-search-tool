-- ============================================================================
-- JobMatch — admin panel, Phase 2 (user management).
-- Paste into Supabase -> SQL Editor -> Run. Safe to re-run: every statement is
-- `if not exists` / `create or replace` / guarded by a catalog lookup.
--
-- The app works WITHOUT this: list_users() falls back to the old column set,
-- delete_user() cleans children from Python, and the disable controls render
-- as unavailable. Running it turns disable on and makes the cleanup structural.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Accounts: disable + a revocable extension token
-- ---------------------------------------------------------------------------
-- disabled_at is nullable-timestamp rather than a boolean so it records WHEN,
-- which is the question you actually ask when an account stops working.
alter table public.users add column if not exists disabled_at timestamptz;

-- token_epoch is folded into the extension token's HMAC message (web._ext_token).
-- Incrementing it invalidates every token that user has ever been issued — the
-- only revocation mechanism available, since the tokens are derived, not stored.
-- Epoch 0 deliberately keeps the original message shape, so running this
-- migration does not break the tokens already pasted into installed extensions.
alter table public.users add column if not exists token_epoch integer not null default 0;


-- ---------------------------------------------------------------------------
-- 2. Orphan cleanup — MUST run before section 3
-- ---------------------------------------------------------------------------
-- db.delete_user() historically removed only user_jobs + users, so every other
-- username-keyed table can hold rows belonging to accounts that no longer exist.
-- The foreign keys below REFUSE TO BE CREATED while such rows are present, so
-- this is a prerequisite, not housekeeping.
--
-- If you want to see what will go first, run the selects instead of the deletes:
--   select count(*) from public.learned_answers
--    where username not in (select username from public.users);
delete from public.user_jobs       where username not in (select username from public.users);
delete from public.profiles        where username not in (select username from public.users);
delete from public.applications    where username not in (select username from public.users);
delete from public.resumes         where username not in (select username from public.users);
delete from public.learned_answers where username not in (select username from public.users);

-- tailored_cache is deliberately NOT cleaned by username and gets no foreign key:
-- db.put_tailored() writes username='' for anonymous entries, which no FK tolerates
-- and which the query above would delete. It is pruned by age instead (Phase 3).


-- ---------------------------------------------------------------------------
-- 3. Cascade deletes
-- ---------------------------------------------------------------------------
-- Makes the cleanup structural, so a delete performed from the Supabase dashboard
-- or `manage_users.py remove` is as complete as one done through the admin panel.
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


-- ---------------------------------------------------------------------------
-- 4. Company blocklist
-- ---------------------------------------------------------------------------
-- Deleting a company's jobs does not stick on its own: the scraper runs twice every
-- weekday and puts them straight back (this has already happened — see the comment
-- above prune_old_jobs' call site in scraper/__init__.py). The scraper and the
-- extension's bulk import both consult this table on every run.
--
-- name_key is db.normalize_label(name): lowercased, punctuation collapsed. Company
-- suffixes are deliberately NOT stripped — reducing "Apple Inc" to "apple" would also
-- block "Apple Hospitality", which is a different employer.
create table if not exists public.blocked_companies (
  name_key   text primary key,
  name       text not null,          -- as it appeared, for display
  reason     text,
  added_by   text,
  created_at timestamptz default now()
);


-- ---------------------------------------------------------------------------
-- 5. Admin audit trail
-- ---------------------------------------------------------------------------
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


-- ---------------------------------------------------------------------------
-- 6. Index for company-scoped counts and deletes
-- ---------------------------------------------------------------------------
-- The delete preview counts and then removes by company; without this each is a
-- sequential scan of ~19k rows.
create index if not exists jobs_company_idx on public.jobs (company);


-- ---------------------------------------------------------------------------
-- 7. Reload PostgREST's schema cache
-- ---------------------------------------------------------------------------
-- PostgREST caches the schema and will keep answering with the OLD column list
-- until told otherwise — without this, disabled_at reads as missing for a while.
notify pgrst, 'reload schema';
