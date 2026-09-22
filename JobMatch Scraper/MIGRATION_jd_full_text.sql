-- Run after deploying full-text db.jd_fingerprint (2026-09-22 or later).
-- Correct prefix hashes left by the old 8,000-character application cap.
-- This never edits descriptions or claims their derived facts have been re-read.
begin;
update public.jobs j
   set jd_fp = case when btrim(d.jd, E' \t\n\r\f\v\u00a0\u2007\u202f') = '' then null
                    else md5(d.jd) end
  from public.job_descriptions d
 where d.url = j.url
   and j.jd_fp is distinct from
       case when btrim(d.jd, E' \t\n\r\f\v\u00a0\u2007\u202f') = '' then null
            else md5(d.jd) end;
commit;
