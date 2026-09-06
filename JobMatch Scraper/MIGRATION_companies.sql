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
