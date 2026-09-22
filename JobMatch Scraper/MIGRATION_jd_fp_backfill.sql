-- MIGRATION_jd_fp_backfill.sql -- give the descriptions we ALREADY hold their fingerprint.
--
-- MIGRATION_jd_fingerprints.sql added jobs.jd_fp and said, correctly, that it is written "on the
-- next write of that row's description (db.update_jds)". What that sentence missed is that a
-- description is normally IMMUTABLE -- score_jobs queues a fetch only when the `jd` column is
-- empty -- so for the 46,849 rows already fetched there is no next write, and jd_fp would have
-- stayed NULL for ever.
--
-- That did not merely leave the column empty. check_derived.py reads jd_fp IS NULL as "no
-- description at all", so its first run against the migrated database reported:
--
--     no description at all : 35754  100.0%
--
-- on a corpus where 99.4% of rows have a description. A tool whose failure mode is a reassuring
-- number is worse than one that crashes -- the same sentence that file's own docstring uses about
-- an earlier version of this same mistake. It is written there because it happened there; this is
-- the second time, in a new guise.
--
-- IT IS COMPUTED HERE RATHER THAN IN PYTHON because the input is already in the database. The
-- Python form reads 263 MB of description text over the proxy to hand back 32 characters a row;
-- this is one statement, no egress, and the box is 271 ms away.
--
-- THREE THINGS MAKE THE SQL AGREE WITH db.jd_fingerprint, and each is a way it could silently
-- disagree instead:
--
--   * Full stored text, including duties and requirements after character 8,000.
--   * md5 over UTF-8 -- confirmed: this database is UTF8, so md5(text) hashes the same bytes
--     Python's .encode("utf-8") produces.
--   * the whitespace class -- db.jd_fingerprint returns None, not a hash, when .strip() leaves
--     nothing, and Python strips every Unicode space. Postgres btrim() with no second argument
--     strips ASCII SPACE ONLY, so a description of "\n\n" would get a hash here and None there,
--     and land permanently on the wrong side of the comparison. Absence is not a mismatch:
--     they stay NULL.
--
--     The class below is ASCII whitespace plus three Unicode spaces, which is NOT every space
--     Python strips -- so it was checked rather than assumed. Counting whitespace-only
--     descriptions three ways on the live corpus: this class 14, EVERY Unicode space character
--     14, POSIX [:space:] 14. The classes are indistinguishable on this data, so the gap is
--     theoretical. If it ever stops being theoretical the symptom is a row whose jd_fp never
--     matches a facts_fp, i.e. one permanent false positive in check_derived.py -- visible and
--     harmless, which is the right direction for this to fail in.
--
-- facts_fp IS DELIBERATELY NOT BACKFILLED, and that is the whole point of the pair. We know what
-- text a row STORES; we do not know what text its exp_max_years was read from. Setting
-- facts_fp = jd_fp here would assert that every derived column in the corpus is current -- which
-- is precisely the claim the column exists to test, and precisely the claim that was false on
-- 2026-09-04. It stays NULL until a scoring pass stamps what it actually read, and until then
-- check_derived.py reports those rows as "not stamped yet", which is the honest answer.
--
-- Re-runnable: the WHERE clause only touches rows that have no fingerprint yet.

update public.jobs j
   set jd_fp = md5(d.jd)
  from public.job_descriptions d
 where d.url = j.url
   and j.jd_fp is null
   and btrim(d.jd, E' \t\n\r\f\v\u00a0\u2007\u202f') <> '';

-- What landed, so the run is not taken on trust.
select count(*) filter (where jd_fp is not null)  as fingerprinted,
       count(*) filter (where jd_fp is null)      as without,
       count(*) filter (where facts_fp is not null) as facts_stamped
  from public.jobs;

notify pgrst, 'reload schema';
