-- JobMatch: all five schema-separation migrations, in one paste.
-- Generated 2026-09-06. Every statement is `if not exists`; the whole file is re-runnable.
--
-- SAFE TO RUN AS ONE BLOCK. Nothing here drops, renames or rewrites an existing column;
-- it only adds two nullable columns to `jobs` and creates five empty tables. No row of
-- `jobs` is touched, so the live site keeps serving from it exactly as it does now --
-- the application code will not read any new table until a backfill stamps it complete.
--
-- If you would rather do them one at a time, each source file stands alone; the section
-- markers below say which is which.
--
-- 
-- TWO THINGS TO KNOW BEFORE YOU PASTE
--
-- 1. THIS FILE CONTAINS THREE `do $$ ... $$;` BLOCKS (the foreign keys). They contain their own
--    semicolons. A SQL client that splits input naively on ";" will chop them into fragments and
--    report a syntax error around `end if` -- phpPgAdmin has historically done this. If that
--    happens, the tables and indexes will all have been created fine and only the three foreign
--    keys will be missing. Run these three instead, one at a time:
--
--      alter table public.job_descriptions add constraint job_descriptions_url_fkey
--        foreign key (url) references public.jobs (url) on delete cascade;
--      alter table public.job_facts add constraint job_facts_url_fkey
--        foreign key (url) references public.jobs (url) on delete cascade;
--      alter table public.job_terms add constraint job_terms_url_fkey
--        foreign key (url) references public.jobs (url) on delete cascade;
--
--    Those are NOT re-runnable (Postgres has no ADD CONSTRAINT IF NOT EXISTS) -- running one
--    twice reports "already exists", which is the answer you wanted anyway.
--
--    The constraints matter: db.delete_urls only ever deletes from `jobs`, and prune_old_jobs
--    runs a 30-day window. Without ON DELETE CASCADE every pruned posting leaves its description,
--    its readings and its analysis behind for ever.
--
-- 2. `notify pgrst, 'reload schema';` is VESTIGIAL on this stack and safe either way. It dates
--    from the Supabase era; this database is reached through psycopg and the pgrest.py shim, and
--    there is no PostgREST server listening. A NOTIFY nobody is listening for is a no-op. It is
--    left in because db.JOBS_DERIVED_SQL still carries it and the two must not drift.
--
-- AFTER THIS, in order:
--   python scripts/build_companies_table.py --apply   && python scripts/companies_parity.py
--   python scripts/backfill_job_descriptions.py --apply  (repeat) --verify --stamp
--   python scripts/backfill_job_facts.py --apply         (repeat) --verify --stamp
--   python scripts/backfill_job_terms.py --apply         (repeat) --verify --stamp
--   python scripts/check_derived.py


-- ============================================================================================
-- [1/5]  MIGRATION_companies.sql
--   companies + data_versions -- FIRST, because data_versions is where every other
--   phase keeps its readiness stamp. The DDL below does not depend on it, but the
--   backfills afterwards cannot flip without it.
-- ============================================================================================
-- JobMatch: employer-level facts as a TABLE, and a version stamp for the caches that read them.
-- Paste into the SQL editor and run. Safe to re-run (all 'if not exists').
--
-- Phase 1 of the schema separation. Phase 0 (MIGRATION_jd_fingerprints.sql) is independent of
-- this one; either order is fine.
--
-- WHY. Everything the feed knows about an EMPLOYER -- its logo, whether it sponsors, whether it
-- is a staffing agency, whether it is cap-exempt -- currently lives in files that are read at
-- request time and shipped in the deploy zip. Measured on the live corpus 2026-09-06:
--
--     sponsor_counts.json   129,660 keys   3.3 MB on disk ->  11.8 MB in RAM, per worker
--     visa_tags.json        123,472 keys   2.9 MB on disk ->  11.2 MB in RAM, per worker
--
-- ...to answer questions about 3,395 employers. 127,682 of those sponsor rows and 121,459 of
-- those visa rows are NEVER ASKED ABOUT: they are the federal filing universe, not our corpus.
-- 23 MB of a ~1.2 GB account-wide budget, per Passenger worker, held for 2.6% utilisation.
--
-- Worse, they are lazy module globals that /reload does NOT clear (web.reload_jobs touches the
-- row, score, profile, resume, status and jdmeta caches and none of these). Only a worker
-- restart picks up a rebuilt file. A table has no such state.
--
-- THE PRIMARY KEY IS core.norm_company(name), NOT THE NAME. Three normalisations coexist in this
-- codebase -- core.norm_company / scraper._norm_name (aggressive: strips legal suffixes and
-- "group/labs/technologies"), scraper._strict_norm_name (keeps them), and db.block_key (keeps
-- legal suffixes) -- and picking the wrong one silently loses every employer whose corpus
-- spelling differs from its filing spelling. This is the aggressive one because it is what
-- core.sponsor_strength, core.visa_tags and core.is_everify already key on today; changing the
-- lookup and the storage in one step would make a miss impossible to attribute.

create table if not exists public.companies (
    -- core.norm_company(display_name). See above: this is a JOIN KEY, not a label.
    name_key       text primary key,
    -- The spelling the corpus actually uses, for display. Not unique: two spellings can
    -- normalise to one key, and the row keeps whichever the builder saw most.
    display_name   text not null,

    -- ---- identity, resolved from static/logos/index.json at BUILD time -------------------
    -- The FINAL values a card draws, not the inputs it draws them from. web._logo_slug does a
    -- two-step lookup (direct slug, then an alias map keyed on core.norm_company) and picking
    -- the extension needs the manifest as well -- so storing a slug here would mean shipping
    -- the manifest anyway and resolving twice. A facts table stores facts.
    logo_url       text,
    logo_ar        real,
    logo_mono      boolean,
    initials       text,

    -- ---- classification ------------------------------------------------------------------
    -- NULL for now. /companies still renders companies.json, which carries the sector index
    -- and the careers-URL prefix codes; Phase 1 replaces what the FEED reads, which is
    -- everything above. Moving that page is a separate, smaller job.
    sector         text,
    -- Pure regex today (core.is_agency / core.is_cap_exempt), no data file, NOT memoised, run
    -- per row per rebuild over ~36k rows. Materialising them here is the cheapest thing in this
    -- whole migration: two booleans replace two regex scans per card.
    is_agency      boolean not null default false,
    is_cap_exempt  boolean not null default false,
    is_everify     boolean not null default false,

    -- ---- sponsorship, resolved from the federal files at BUILD time ----------------------
    h1b_count      integer,
    h1b_by_fy      jsonb,
    -- The bitmask core.visa_tags returns: h1b=1, green_card=2, stem_opt=4, e3=8, h1b1=16.
    -- Stored as the int rather than expanded, so core.VISA_TAGS stays the one definition.
    visa_bits      integer not null default 0,

    careers_url    text,
    updated_at     timestamptz not null default now()
);

-- The feed reads this table WHOLE (a few thousand rows), so it needs no index beyond the PK.
-- This one is for the /companies page and for admin lookups by the spelling a human typed.
create index if not exists companies_display_idx on public.companies (lower(display_name));


-- ============================================================================================
-- data_versions -- one row per generated dataset, and the answer to a specific trap.
-- ============================================================================================
--
-- web._derived_signature() hashes the CONTENT of three files and that hash is half the
-- row_cache/*.rows.gz key. Two of those three files are the ones this migration replaces, so
-- moving them into a table strips the key of most of its inputs -- and a row cache keyed on
-- something that no longer changes serves stale cards for ever.
--
-- Its docstring states the rule that makes this dangerous: NOTHING THAT CAN FAIL SILENTLY MAY BE
-- IN THE KEY. That is not theoretical. The key once included two KV maps whose readers swallowed
-- a failure into {}, so workers computed different keys and each rebuilt 7 s over the other's
-- file; production showed 63 ms and 8,401 ms in the same second.
--
-- So the replacement is an explicit version a builder BUMPS, read by _derived_signature, whose
-- reader RAISES rather than defaulting. A worker that cannot read the version does not get to
-- compute a key at all.
create table if not exists public.data_versions (
    name       text primary key,     -- 'companies', 'logos', 'sponsors', 'visa_tags'
    version    text not null,        -- opaque; the builders write a content hash
    updated_at timestamptz not null default now()
);

-- LAST. Without this PostgREST answers from its cached schema and both tables read as missing
-- until it happens to reload.
notify pgrst, 'reload schema';


-- ============================================================================================
-- [2/5]  MIGRATION_jd_fingerprints.sql
--   jd_fp + facts_fp on jobs -- two columns, no new table.
-- ============================================================================================
-- JobMatch: PROVENANCE for every reading we derive from a job description.
-- Paste into the SQL editor and run. Safe to re-run (all 'if not exists').
--
-- Also lives in db.JOBS_DERIVED_SQL, which is what score_jobs prints when a write fails because
-- these columns do not exist yet. The two must stay in step.
--
-- WHY THIS EXISTS. A description is supposed to be immutable: score_jobs queues a fetch only
-- when the `jd` column is empty, so exp_max_years / jd_terms / sponsor_jd are normally a true
-- reading of the text the row holds. On 2026-09-04 the sweep wrote 1,783 descriptions onto rows
-- the table ALREADY HELD -- that path deliberately does not gate on whether the posting is new,
-- because a slice in which every aggregator row is already known would otherwise drop the
-- descriptions it just rescued. The text was replaced. The readings were not. Nothing noticed.
--
-- What that cost, measured on this corpus two days later:
--
--     778 rows lost exp_max_years entirely, so nine postings asking 3 to 10 years turned up
--         in a "0 to 2 Years" search -- the experience filter keeps a row it has no number for,
--         because many genuine entry-level posts state none
--     117 rows kept a number that was too HIGH (typically stored 3, description says 1), so
--         genuinely entry-level jobs were hidden from that same search. Nobody reports the job
--         they never saw, which is why this half went unnoticed longer
--     ~7% of the corpus still carries jd_terms -- the input to every match score -- derived
--         from text the row no longer stores
--
-- Finding those took a 200-second scan that re-read every description over the network. The
-- point of these two columns is that the same question is now a WHERE clause.
--
-- THE PATTERN IS NOT NEW HERE. user_scores already stores resume_fp, the md5 of the resume a
-- score was computed against, and filters every read on it -- so that table may be out of date
-- but cannot serve a number computed against something else. This is the same bargain applied
-- to the other half: a reading may be old, but it can no longer lie about which document it is
-- a reading OF.

-- The fingerprint of the description this row STORES. Written by db.update_jds, in the same
-- row of the same statement as the text, so there is no window where they disagree.
alter table public.jobs add column if not exists jd_fp text;

-- The fingerprint of the text the DERIVED columns beside it were read from. Written by
-- scraper/score_jobs.py::_persist_derived, in the same payload as the readings themselves.
alter table public.jobs add column if not exists facts_fp text;

-- THE QUERY THIS IS ALL FOR:
--
--     select url from public.jobs where jd_fp is distinct from facts_fp;
--
-- `is distinct from`, not `<>`. Both columns are NULL for a row we hold no description for --
-- that row has no reading to be stale, and `<>` would answer NULL and quietly drop it from
-- either side of the comparison. Both NULL must read as agreement.
--
-- PARTIAL, because that disagreement is the only query anyone runs against this pair and on a
-- healthy corpus it matches almost nothing. A plain index on two 32-char columns across 47k
-- rows would cost ~3 MB to answer a question about a handful of them.
create index if not exists jobs_facts_stale_idx on public.jobs (url)
  where jd_fp is distinct from facts_fp;

-- BACKFILL IS DELIBERATELY NOT DONE HERE, and that is the point rather than an omission.
--
-- There is no way to compute facts_fp for an existing row from inside the database: it is a
-- claim about which text produced a reading, and for rows already in the table that fact was
-- never recorded. Stamping both columns from the current `jd` would assert the readings are
-- current, which is exactly the false claim this migration exists to make impossible -- and it
-- would mark the ~2,300 known-bad rows as clean.
--
-- So both columns start NULL and fill in truthfully as rows are re-read:
--   * jd_fp    on the next write of that row's description (db.update_jds)
--   * facts_fp on the next scoring pass that analyses it (_persist_derived)
-- A full pass (`python -m scraper.score_jobs`) fills every readable row in one go.
--
-- Until then a NULL facts_fp means "we do not know what this reading is about", which is the
-- honest state and the one scripts/check_derived.py reports separately from a real mismatch.

-- LAST. Without this PostgREST answers from its cached schema and both columns above read as
-- missing until it happens to reload.
notify pgrst, 'reload schema';


-- ============================================================================================
-- [3/5]  MIGRATION_job_descriptions.sql
--   job_descriptions -- the 263 MB of text.
-- ============================================================================================
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
    -- Preserve the complete posting, including qualifications beyond the former 8,000-char
    -- application cap. update_jds and jd_fingerprint now use exactly the same full text.
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


-- ============================================================================================
-- [4/5]  MIGRATION_job_facts.sql
--   job_facts -- the nine derived columns.
-- ============================================================================================
-- JobMatch: what we DERIVED about a posting, separated from what the employer STATED.
-- Paste into the SQL editor and run. Safe to re-run (all 'if not exists').
--
-- Phase 3a. Independent of Phases 0-2; any order. Phase 3b (jd_terms into job_terms) is
-- deliberately NOT here -- see the note at the foot.
--
-- WHY. `jobs` mixes three kinds of fact and a reader cannot tell them apart. title, company,
-- location and found_date are what the EMPLOYER published. first_seen, last_seen, is_active and
-- miss_count are what WE track. And nine columns in the middle are what we DERIVED by reading
-- the description -- a reading that can be wrong, can go stale, and did both on 2026-09-04.
-- db.py::FIELDS already groups them with comments saying exactly this; the table does not.
--
-- Separating them makes the provenance of every column obvious from the table it is in, and it
-- gives the derived group somewhere to carry its own metadata: facts_fp (which text produced
-- this reading) and derived_at (when).
--
-- THIS IS THE PHASE THAT CAN GO WRONG QUIETLY, so the mechanism is worth stating. db.COLS_SCORE
-- names nine of these columns NOT because the scorer reads them but because it DIFFS against
-- them -- _persist_derived compares what it just computed with what is stored and writes only
-- the rows that changed. Its docstring is blunt about the consequence: "Drop those and every run
-- would think every derived field had changed and re-upsert the whole corpus." That failure does
-- not look like a bug. It looks like the scrape getting slower.
--
-- So the split keeps all nine reachable through db.load_jobs(cols=COLS_SCORE) whatever table
-- they physically live in, and scripts/backfill_job_facts.py --verify compares both stores row
-- for row before anything is allowed to read the new one.

create table if not exists public.job_facts (
    url            text primary key,

    -- ---- derived from the location string, and from the description for `remote` -----------
    loc_state      text,
    loc_metro      text,
    remote         boolean,

    -- ---- derived from the description ------------------------------------------------------
    salary_min     integer,
    salary_max     integer,
    salary_period  text,
    exp_max_years  integer,
    sponsor_jd     text,
    sponsor_reason text,

    -- WHICH TEXT these nine are a reading of. The same column Phase 0 added to `jobs`, now
    -- beside the facts it describes rather than beside the job. Both are written for now:
    -- jobs.facts_fp is what scripts/check_derived.py compares against jobs.jd_fp in a single
    -- read, and it keeps that job until the contract step drops it -- at which point
    -- check_derived reads this table instead. Expand, then contract; never both at once.
    facts_fp       text,
    derived_at     timestamptz not null default now()
);

-- The rows must die with the job. db.delete_urls deletes from `jobs` alone and prune_old_jobs
-- runs a 30-day window; without this every pruned posting leaves its readings behind for ever.
-- user_scores and job_descriptions learned this the same way.
--
-- DO block because Postgres has no ADD CONSTRAINT IF NOT EXISTS. It will FAIL if orphans exist:
--   delete from public.job_facts f
--    where not exists (select 1 from public.jobs j where j.url = f.url);
do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'job_facts_url_fkey'
                     and conrelid = 'public.job_facts'::regclass) then
        alter table public.job_facts
            add constraint job_facts_url_fkey
            foreign key (url) references public.jobs (url) on delete cascade;
    end if;
end $$;

-- THE FEED FILTERS ON THESE, which `jobs` never had indexes for: it carries two
-- (loc_state, is_active) for a feed that filters on ten-plus attributes. These are the columns
-- _filter_rows actually compares, and they matter now in a way they did not before -- the plan's
-- DB-fallback feed path pushes exactly these comparisons into the WHERE clause.
create index if not exists job_facts_exp_idx    on public.job_facts (exp_max_years);
create index if not exists job_facts_loc_idx    on public.job_facts (loc_state, remote);
create index if not exists job_facts_salary_idx on public.job_facts (salary_min);

-- NOT HERE, AND ON PURPOSE:
--
--   * jd_terms. It is 33 MB and the feed reads it corpus-wide -- web.job_analysis unpacks it off
--     every built row to score postings user_scores has no entry for. Moving it changes what the
--     FEED reads, not just where a writer writes, and that interacts with score_pending and the
--     stored per-user scores. A separate phase with its own verification, not a passenger on
--     this one.
--   * exp_src, roles, track, intern, jd_admit. Those are computed per request today and nothing
--     writes them; adding the columns now would ship five that stay NULL and later read as
--     something someone forgot to finish. They arrive with their writer, in Phase 4.
--   * No backfill. scripts/backfill_job_facts.py does it in batches and stamps
--     data_versions['job_facts'] only when a row-for-row verify passes -- which is also the flag
--     db.py reads to decide whether this table may be trusted. Until then `jobs` is authoritative
--     and this is a shadow copy.

-- LAST. Without this PostgREST answers from its cached schema and every column above reads as
-- missing until it happens to reload.
notify pgrst, 'reload schema';


-- ============================================================================================
-- [5/5]  MIGRATION_job_terms.sql
--   job_terms -- the packed analysis.
-- ============================================================================================
-- JobMatch: the packed keyword analysis moves into its own table.
-- Paste into the SQL editor and run. Safe to re-run (all 'if not exists').
--
-- Phase 3b. Independent of the others; any order.
--
-- WHY THIS ONE IS ABOUT MEMORY, not disk and not really seconds. Measured against the live box
-- on 2026-09-06, the feed's own corpus select:
--
--     with jd_terms      47,133 rows   69.8 MB   45.1 s
--     without jd_terms   47,133 rows   31.1 MB   27.2 s
--
-- So the column is 55% of the bytes and 40% of the time -- and 99.5% of rows carry it. But the
-- corpus read is cached, so those seconds are paid a few times a day, not per request. What is
-- paid CONTINUOUSLY is that the same 38.7 MB sits RESIDENT in every Passenger worker, on a
-- shared account capped at ~1.2 GB in total, where memory is the binding constraint on how many
-- users this can serve.
--
-- WHAT THIS MIGRATION DOES NOT DO, and the distinction is the whole reason Phase 3 was split in
-- two. It does not change what the feed reads. db.load_jobs still returns jd_terms on every row;
-- it simply fetches it from here and merges it back, exactly as Phase 2 did for the description.
-- Making the read LAZY -- fetching terms only for the rows a user still needs scoring for, which
-- is ~23% for an account with stored scores and 100% for a fresh one -- is a change to the
-- scoring path, which is the most carefully tuned code in the app. It deserves its own change
-- and its own measurement rather than arriving as a side effect of a storage move.
--
-- n_terms is stored now, unused, for that later change: it answers "does this row have an
-- analysis, and is it thin" without reading 38.7 MB, which is what web._row_pending needs and
-- what jobs_fingerprint's third component counts. Adding the column later would leave it NULL on
-- every existing row and require its own backfill to become useful; adding it with its writer
-- costs nothing.

create table if not exists public.job_terms (
    url       text primary key,
    -- TEXT, deliberately not jsonb -- the same reasoning JOBS_DERIVED_SQL gives for the column
    -- this replaces. jsonb normalises an object and does not preserve key order, which breaks
    -- this twice: score_jobs diffs the stored value against the one it just built to decide
    -- whether to write (a reordered read re-upserts the whole corpus every run), and the key
    -- order IS analyze_jd's frozen term order, which breaks ties between equal-weight terms in
    -- the panel's skill lists. Nothing ever queries inside this column.
    jd_terms  text,
    -- Length of the packed string. NULL text and 0 mean the same thing here, and
    -- mirror_job_terms writes NULL rather than '' so `n_terms > 0` is exactly the predicate
    -- `jd_terms is not null` used to be on `jobs`.
    n_terms   integer not null default 0,
    -- Which description this analysis was read from -- the same stamp Phase 0 put on `jobs`,
    -- carried here so a reading and its provenance cannot be separated by a partial write.
    facts_fp  text,
    updated_at timestamptz not null default now()
);

-- The rows must die with the job, or every pruned posting leaves ~730 B of packed analysis
-- behind for ever. db.delete_urls only ever deletes from `jobs`.
do $$
begin
    if not exists (select 1 from pg_constraint
                   where conname = 'job_terms_url_fkey'
                     and conrelid = 'public.job_terms'::regclass) then
        alter table public.job_terms
            add constraint job_terms_url_fkey
            foreign key (url) references public.jobs (url) on delete cascade;
    end if;
end $$;

-- jobs_fingerprint()'s third component counts rows carrying an analysis, on every corpus
-- revalidation. It is a HEAD, so this index is what keeps it from being a sequential scan of a
-- table whose rows are mostly TOAST pointers.
create index if not exists job_terms_present_idx on public.job_terms (url) where n_terms > 0;

-- No backfill here: 38.7 MB in one statement, on a box whose Passenger workers are serving the
-- site from the same ~1.2 GB budget, is how a run gets SIGKILLed. scripts/backfill_job_terms.py
-- does it in batches and stamps data_versions['job_terms'] only after a row-for-row verify --
-- which is also the flag db.py reads before believing this table.

-- LAST. Without this PostgREST answers from its cached schema and the table reads as missing
-- until it happens to reload.
notify pgrst, 'reload schema';

