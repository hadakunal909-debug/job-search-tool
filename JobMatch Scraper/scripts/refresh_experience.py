#!/usr/bin/env python3
"""Audit/recompute experience from authoritative descriptions in bounded DB batches.

Run on the database host: python scripts/refresh_experience.py --report outputs/experience.jsonl
Add --apply to repair differences. Reports contain the old/new values and source hash
for rollback. Other derived fields and their fingerprints are deliberately not stamped.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("EV_OFF", "1")
import core
import db


def connect():
    if not db.PG_DSN:
        raise RuntimeError("Run on the database host with PG_DSN; no local-file fallback")
    try:
        import psycopg
        return psycopg.connect(db.PG_DSN)
    except ImportError:
        import psycopg2
        return psycopg2.connect(db.PG_DSN)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", required=True)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--plan", help="restrict to URLs in a previous audit JSONL; descriptions are still re-read")
    args = ap.parse_args()
    path = Path(args.report)
    path.parent.mkdir(parents=True, exist_ok=True)
    counts, last = Counter(), ""
    targets = None
    if args.plan:
        with open(args.plan, encoding="utf-8") as stream:
            targets = sorted({json.loads(line)["url"] for line in stream if line.strip()})
    conn = connect()
    with conn, path.open("x", encoding="utf-8") as report:
        while True:
            with conn.cursor() as cur:
                query = """select j.url, j.title, j.company, coalesce(d.jd, ''),
                                      f.exp_max_years, f.url is not null, j.is_active
                                 from jobs j left join job_descriptions d on d.url=j.url
                                 left join job_facts f on f.url=j.url
                                where j.url > %s"""
                params = [last]
                if targets is not None:
                    query += " and j.url = any(%s)"
                    params.append(targets)
                cur.execute(query + " order by j.url limit 200", params)
                rows = cur.fetchall()
            if not rows:
                break
            for url, title, company, text, old, has_facts, active in rows:
                counts["rows"] += 1
                clean, quality = core.clean_jd(text)
                new = core.experience_years(clean)
                counts["missing_description" if not text else quality] += 1
                if old == new and has_facts:
                    counts["consistent"] += 1
                    continue
                record = {"url": url, "title": title, "company": company,
                          "old": old, "new": new, "jd_fp": db.jd_fingerprint(text),
                          "active": active, "quality": quality,
                          "evidence": core.experience_evidence(clean)}
                counts["mismatch"] += 1
                if args.apply:
                    # Optimistic guards prevent a concurrent source refresh or scoring
                    # write from being overwritten with an obsolete reading.
                    with conn.cursor() as cur:
                        if has_facts:
                            cur.execute("""update job_facts f set exp_max_years=%s, derived_at=now()
                                            from jobs j left join job_descriptions d on d.url=j.url
                                            where f.url=%s and j.url=f.url
                                              and coalesce(d.jd, '') = %s
                                              and f.exp_max_years is not distinct from %s""",
                                        (new, url, text, old))
                        else:
                            cur.execute("""insert into job_facts(url, exp_max_years)
                                            select j.url, %s from jobs j
                                            left join job_descriptions d on d.url=j.url
                                            where j.url=%s and coalesce(d.jd, '')=%s
                                            on conflict (url) do nothing""", (new, url, text))
                        record["written"] = cur.rowcount == 1
                    counts["written" if record["written"] else "concurrent_or_missing"] += 1
                report.write(json.dumps(record, ensure_ascii=False) + "\n")
            report.flush()
            conn.commit()
            last = rows[-1][0]
            if counts["rows"] % 5000 == 0:
                print(json.dumps(dict(counts)), flush=True)
        summary = {"apply": args.apply, "scope": "plan" if targets is not None else "all jobs", "counts": dict(counts)}
        path.with_suffix(".summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    conn.close()
    print(json.dumps(summary), flush=True)
    return int(args.check and (not counts["rows"] or bool(counts["mismatch"])))


if __name__ == "__main__":
    sys.exit(main())
