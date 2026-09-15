-- A row-count fingerprint cannot detect edits to an existing posting's date or facts.
-- One revision per SQL statement, rather than per row, keeps bulk scraper writes cheap.
begin;
insert into public.data_versions(name, version) values ('job_data', '0')
on conflict (name) do nothing;

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

do $$
declare tbl text;
begin
    foreach tbl in array array['jobs', 'job_facts', 'job_descriptions', 'job_terms'] loop
        execute format('drop trigger if exists job_data_changed on public.%I', tbl);
        execute format('create trigger job_data_changed after insert or update or delete or truncate on public.%I for each statement execute function public.bump_job_data_version()', tbl);
    end loop;
end;
$$;
commit;
