-- JobMatch: Resume Brain review panel -- the active-resume flag, the two Brain columns that were
-- never created, and durable storage for uploaded resume FILES.
-- Paste into SQL Editor -> Run. Safe to re-run (every statement is guarded).
-- Also lives in db.RESUME_FILES_SQL, which the app prints if a write hits a missing column.

-- ---------------------------------------------------------------------------------------------
-- 1. WHICH resume is the live one.
--
-- There were two resume stores and nothing joined them: users.resume (one text column, and the
-- ONLY thing the feed's match % scores against) and the resumes table (the library Resume Brain
-- manages). _ensure_resume_migrated copied legacy -> library one way and only when the library
-- was empty, so measured live: two of four accounts had a resume in the library and '' in
-- users.resume, i.e. their entire feed scored against an empty string, and a third had two
-- different documents in the two places.
--
-- After this, the library row is the truth and users.resume is a derived cache of whichever row
-- carries active = true. Nothing that reads users.resume has to change.
alter table public.resumes add column if not exists active boolean default false;
-- Partial index, not a unique constraint: "exactly one active per user" is enforced in
-- db.set_active_resume (clear-then-set), and a hard constraint would make the clear half of that
-- pair fail the moment it briefly leaves a user with zero active rows.
create index if not exists resumes_active_idx on public.resumes using btree (username) where active;

-- ---------------------------------------------------------------------------------------------
-- 2. The two Brain objects that never existed.
--
-- These were documented from the start, but the ALTER was never run, so db.get_brain_kb /
-- save_brain_kb silently took their local-file fallback on EVERY call. That file
-- (brain_kb_local.json) is gitignored AND absent from the deploy bundle, so on the live host
-- stories, lessons and the self-training model had nowhere durable to live -- which is why every
-- account reads 0 stories and 0 lessons. That is not disuse, it is a missing column.
alter table public.users add column if not exists brain_kb jsonb;
-- Company research is SHARED across users (public facts), so it is keyed by domain, not username,
-- and deliberately has no FK to users.
create table if not exists public.brain_companies (
  domain text primary key,
  data jsonb,
  fetched_at timestamp with time zone default now()
);

-- ---------------------------------------------------------------------------------------------
-- 3. Uploaded resume files.
--
-- A SIBLING table, not columns on resumes, and that is load-bearing: db.list_resumes selects *,
-- resume_brain.brain.get_resume re-lists every row to fetch one, and db.profile_text walks all
-- rows on nearly every request. A blob column on resumes would be downloaded constantly. Here it
-- can only be read by asking for it.
--
-- b64 is TEXT holding base64, not bytea. pgrest.jsonify() decodes any bytes/memoryview it sees
-- with .decode('utf-8', 'replace'), so a bytea column would come back from the direct-Postgres
-- transport full of U+FFFD -- silently destroyed, on read, with no error. base64-in-text is also
-- what the existing blob store (tailored_cache.data->'file'->>'b64') already does.
create table if not exists public.resume_files (
  id text not null,
  resume_id text,
  username text not null,
  kind text not null,                 -- 'pdf' | 'docx' | 'tex' | 'txt'
  filename text,
  mime text,
  b64 text,
  size integer,
  created_at timestamp with time zone default now(),
  constraint resume_files_pkey PRIMARY KEY (id)
);
-- Both indexes earn their keep: the first is the "files for this resume" lookup the Original tab
-- makes, the second is the age prune below.
create index if not exists resume_files_resume_idx on public.resume_files using btree (resume_id);
create index if not exists resume_files_created_idx on public.resume_files using btree (created_at);

-- foreign keys, after every table exists
do $$ begin if not exists (select 1 from pg_constraint where conname = 'resume_files_username_fkey') then alter table public.resume_files add constraint resume_files_username_fkey FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE; end if; end $$;
-- ON DELETE CASCADE from the resume too, so deleting a resume version cannot orphan its files.
-- This is the delete path tailored_cache never got: web.py's own health check has been warning
-- that it "holds N rows and has no expiry anywhere in the codebase". Not repeating that here.
do $$ begin if not exists (select 1 from pg_constraint where conname = 'resume_files_resume_fkey') then alter table public.resume_files add constraint resume_files_resume_fkey FOREIGN KEY (resume_id) REFERENCES resumes(id) ON DELETE CASCADE; end if; end $$;

-- ---------------------------------------------------------------------------------------------
-- 4. Backfill: adopt one active row per user, so nobody lands on the new panel with a library
-- full of resumes and none of them selected. Prefers the row whose content matches the legacy
-- users.resume (that is the one the feed has been scoring), else the newest.
-- Written as a plain UPDATE rather than the obvious WITH ... UPDATE. phpPgAdmin decides whether a
-- submission returns rows by looking at how it starts, and a leading WITH makes it wrap the WHOLE
-- pasted script in "select count(*) from ( ... ) as sub" -- at which point the ALTERs above are
-- syntax errors inside a subquery. Avoiding the CTE keeps this file pasteable as one block.
update public.resumes t
set active = true
where t.active is not true
  and not exists (select 1 from public.resumes a
                  where a.username = t.username and a.active)
  and t.id = (select r.id
              from public.resumes r
              left join public.users u on u.username = r.username
              where r.username = t.username
              order by (r.content = u.resume) desc nulls last, r.created_at desc
              limit 1);

-- And the reverse repair the one-way migration never did: seed an empty users.resume from the
-- row we just activated. This is the statement that fixes the two accounts scoring against ''.
update public.users u
set resume = r.content
from public.resumes r
where r.username = u.username and r.active
  and coalesce(u.resume, '') = '' and coalesce(r.content, '') <> '';

-- LAST. Without this PostgREST answers from its cached schema and everything above reads as
-- missing until it happens to reload.
notify pgrst, 'reload schema';
