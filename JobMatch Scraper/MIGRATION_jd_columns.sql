-- JobMatch: JD-derived filter + scoring columns.
-- Paste into Supabase -> SQL Editor -> Run. Safe to re-run (all 'if not exists').
-- Also lives in db.JOBS_DERIVED_SQL, which the app prints if a write hits a missing column.

-- JD-derived signals the FEED filters on. Same reasoning as the location/pay block above,
-- and the same reason match_score is a column: jdmeta.json is gitignored, is not in the
-- cPanel file list, and scripts/build_deploy_zip.py excludes it. It is also NOT regenerated
-- on the web host -- the scraper runs on an ephemeral GitHub Actions runner whose disk is
-- discarded when the run ends. So live, web._jdmeta was empty, every row fell back to
-- _EMPTY_META, and the experience and 'hide no-sponsorship' filters were silent no-ops.
-- A column reaches the live site through Supabase with no file deploy.
--
-- exp_max_years is the HIGHEST year count the JD states, not the lowest: '8+ years of
-- engineering experience; 2 years of SQL preferred' is an 8-year job, and reading the floor
-- let a senior req hide behind its most junior line item. NULL means the JD states no
-- number at all, which the filter must KEEP.
alter table public.jobs add column if not exists exp_max_years integer;
-- '' = no signal, 'blocked' = the JD rules a visa candidate out, 'open' = it sponsors.
alter table public.jobs add column if not exists sponsor_jd text;
-- LOAD-BEARING, not display copy: core.visa_tags_for_posting substring-matches this against
-- core._BLOCKS_EVERYONE to decide whether a blocked posting keeps STEM-OPT or loses every
-- route. The user-visible wording is fixed in static/app.js; this stays machine-readable.
alter table public.jobs add column if not exists sponsor_reason text;
-- No index on any of these: the feed loads the corpus and filters it in Python, which
-- is also why jobs_loc_state_idx above goes unused.

-- The JD's keyword weights (core.pack_analyzed), which is what lets the feed score a job
-- against ANY resume. Without it web.user_scores has nothing to score with and falls back
-- to match_score -- the baseline the cron computed against the repo's own resume.txt --
-- so EVERY signed-in user saw the owner's match percentages instead of their own.
-- ~600 B/row packed, ~3.8 MB gzipped across the corpus, read once per scrape.
--
-- TEXT, deliberately NOT jsonb. jsonb normalizes an object and does not preserve key
-- order, which would break this twice over: score_jobs diffs the stored value against the
-- one it just built to decide whether to write (a reordered read would re-upsert the whole
-- corpus on every run), and the key order IS analyze_jd's frozen term order, which breaks
-- ties between equal-weight terms in the panel's skill lists. Nothing ever queries inside
-- this column, so jsonb buys nothing to pay for that with. Postgres TOASTs it out of the
-- main row either way, so the columns above stay cheap to read on their own.
alter table public.jobs add column if not exists jd_terms text;

-- LAST. Without this PostgREST answers from its cached schema and every column added above
-- reads as missing until it happens to reload.
notify pgrst, 'reload schema';
