#!/usr/bin/env python3
"""Compare stored filter years with a fresh reading of the SAME cached description.

    python scripts/audit_experience.py --report outputs/experience_audit.json
    python scripts/audit_experience.py --live --check

Offline and read-only. A matching text fingerprint establishes cache consistency, not
that the employer's current page still contains this text. Missing/mismatched text is
reported separately, never counted as verified. Use the regular scoring pass to repair
stored facts after deploying parser changes.
"""
import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import db


def inspect_row(row, text):
    result = {"url": row["url"], "title": row.get("title", ""),
              "stored_years": row.get("exp_max_years")}
    if not text:
        return dict(result, status="missing_description")
    fingerprint = row.get("jd_fp")
    if not fingerprint:
        return dict(result, status="unverified_text")
    if fingerprint != db.jd_fingerprint(text):
        return dict(result, status="different_description")
    clean, quality = core.clean_jd(text)
    required, preferred = core.experience_floors(clean)
    parsed = required if required is not None else preferred
    stored = row.get("exp_max_years")
    try:
        stored = int(stored) if stored is not None and stored != "" else None
    except (TypeError, ValueError):
        stored = "invalid"
    result.update(parsed_years=parsed, required_years=required, preferred_years=preferred,
                  description_quality=quality)
    result["status"] = "consistent" if stored == parsed else "mismatch"
    if quality == "not-a-posting":
        result["status"] = "invalid_description"
    if result["status"] != "consistent":
        result["evidence"] = core.experience_evidence(clean)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", default="jobs_snapshot.json.gz")
    ap.add_argument("--live", action="store_true", help="read current filter values and text fingerprints from the configured database")
    ap.add_argument("--cache", default="jd_cache.json.gz")
    ap.add_argument("--report", default="outputs/experience_audit.json")
    ap.add_argument("--check", action="store_true", help="fail unless every inspected row is comparable and consistent")
    args = ap.parse_args()
    if args.live:
        if not db.has_remote_db():
            ap.error("--live needs a configured remote database; no fallback to local files")
        snapshot = db.load_jobs(cols="url,title,exp_max_years,jd_fp")
    else:
        with gzip.open(args.snapshot, "rt", encoding="utf-8") as stream:
            snapshot = json.load(stream)
    with gzip.open(args.cache, "rt", encoding="utf-8") as stream:
        descriptions = json.load(stream)
    rows = snapshot["rows"] if isinstance(snapshot, dict) else snapshot
    counts, findings = Counter(), []
    for row in rows:
        if not row.get("url"):
            continue
        result = inspect_row(row, descriptions.get(row["url"]))
        counts[result["status"]] += 1
        if result["status"] != "consistent":
            findings.append(result)
    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    scope = "Current database" if args.live else "Local snapshot"
    report.write_text(json.dumps({"scope": scope + " and cached descriptions; no live employer-page verification",
                                 "counts": dict(counts), "findings": findings},
                                ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(counts), indent=2))
    print("Report:", report.resolve())
    return int(args.check and (not counts or any(
        count for status, count in counts.items() if status != "consistent")))


if __name__ == "__main__":
    sys.exit(main())
