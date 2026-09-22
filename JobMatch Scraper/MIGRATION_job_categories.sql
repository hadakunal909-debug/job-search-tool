-- Versioned posting categories. Safe to re-run; no existing job or JD is modified.
-- Deploy dbproxy.py's allowlist update before running the backfill over HTTPS.
begin;
create table if not exists public.job_categories (
    url text primary key references public.jobs(url) on delete cascade on update cascade,
    category text not null,
    category_label text not null,
    category_source text not null check (category_source in ('jd','title','company','unknown')),
    category_confidence text not null check (category_confidence in ('high','medium','low')),
    category_evidence text not null default '',
    category_version text not null,
    category_input_fp text not null,
    category_jd_fp text
);
create index if not exists job_categories_category_idx on public.job_categories(category);

-- Existing feed snapshots must notice changed categories even when job count is unchanged.
create or replace function public.bump_job_data_version() returns trigger
language plpgsql as $$
begin
    insert into public.data_versions(name, version, updated_at)
    values ('job_data', '1', clock_timestamp())
    on conflict (name) do update
       set version = (public.data_versions.version::bigint + 1)::text,
           updated_at = clock_timestamp();
    return null;
end;
$$;
drop trigger if exists job_data_changed on public.job_categories;
create trigger job_data_changed after insert or update or delete or truncate
on public.job_categories for each statement execute function public.bump_job_data_version();
commit;
notify pgrst, 'reload schema';
