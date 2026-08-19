alter table public.resumes add column if not exists active boolean default false;
create index if not exists resumes_active_idx on public.resumes using btree (username) where active;
alter table public.users add column if not exists brain_kb jsonb;
create table if not exists public.brain_companies (
  domain text primary key,
  data jsonb,
  fetched_at timestamp with time zone default now()
);
create table if not exists public.resume_files (
  id text not null,
  resume_id text,
  username text not null,
  kind text not null,
  filename text,
  mime text,
  b64 text,
  size integer,
  created_at timestamp with time zone default now(),
  constraint resume_files_pkey PRIMARY KEY (id)
);
create index if not exists resume_files_resume_idx on public.resume_files using btree (resume_id);
create index if not exists resume_files_created_idx on public.resume_files using btree (created_at);
do $$ begin if not exists (select 1 from pg_constraint where conname = 'resume_files_username_fkey') then alter table public.resume_files add constraint resume_files_username_fkey FOREIGN KEY (username) REFERENCES users(username) ON DELETE CASCADE; end if; end $$;
do $$ begin if not exists (select 1 from pg_constraint where conname = 'resume_files_resume_fkey') then alter table public.resume_files add constraint resume_files_resume_fkey FOREIGN KEY (resume_id) REFERENCES resumes(id) ON DELETE CASCADE; end if; end $$;
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
update public.users u
set resume = r.content
from public.resumes r
where r.username = u.username and r.active
  and coalesce(u.resume, '') = '' and coalesce(r.content, '') <> '';
notify pgrst, 'reload schema';
