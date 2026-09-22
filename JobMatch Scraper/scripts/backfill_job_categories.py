#!/usr/bin/env python3
"""Classify existing postings from complete stored JDs, title, then employer background.

    python scripts/backfill_job_categories.py --report category-report.json
    python scripts/backfill_job_categories.py --apply --report category-report.json
    python scripts/backfill_job_categories.py --verify

Dry run is the default. Text is read in bounded batches, never downloaded twice.
Reports contain category provenance, counts and selected URLs, never full descriptions.
Apply writes only changed category records and verifies each batch immediately.
An interrupted run is safely resumable; --limit and --url support bounded investigations.
"""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EV_OFF", "1")
import db
from job_categories import CATEGORY_VERSION


def changed_record(row, jd, computed=None):
    """Return the new full record only if the stored classification differs."""
    desired = computed if computed is not None else db.category_record(row, jd)
    if any((desired.get(k) or "") != (row.get(k) or "") for k in db.CATEGORY_COLS):
        return desired
    return None


def run(apply=False, verify=False, limit=0, urls=None, batch_size=200):
    rows = db.load_job_category_inputs(urls)
    rows.sort(key=lambda row: row["url"])
    if limit:
        rows = rows[:limit]
    stored = db.job_category_rows([r["url"] for r in rows] if urls or limit else None,
                                  strict=apply or verify)
    report = {"version": CATEGORY_VERSION, "mode": "apply" if apply else "verify" if verify
              else "dry-run", "examined": 0, "changed": 0, "written": 0, "verified": 0,
              "jd_missing": 0, "legacy_8000_candidates": 0, "categories": Counter(),
              "sources": Counter(), "confidence": Counter(), "transitions": Counter(),
              "examples": []}
    started = time.monotonic()
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset:offset + batch_size]
        texts = db._jd_rows([r["url"] for r in batch]) if db.has_remote_db() else {
            r["url"]: r.get("jd") or "" for r in batch}
        pending = []
        for raw in batch:
            row = dict(raw, **{k: v for k, v in stored.get(raw["url"], {}).items()
                              if k != "url"})
            jd = texts.get(row["url"], "")
            new = db.category_record(row, jd)
            change = changed_record(row, jd, new)
            report["examined"] += 1
            report["jd_missing"] += not bool(jd.strip())
            report["legacy_8000_candidates"] += len(jd) == 8000
            report["categories"][new["category"]] += 1
            report["sources"][new["category_source"]] += 1
            report["confidence"][new["category_confidence"]] += 1
            if change:
                pending.append(change)
                report["changed"] += 1
                report["transitions"]["%s -> %s" % (row.get("category") or "missing",
                                                     new["category"])] += 1
                if len(report["examples"]) < 30:
                    report["examples"].append({"url": row["url"], "title": row.get("title"),
                        "company": row.get("company"), "before": row.get("category"),
                        **{k: new[k] for k in ("category", "category_source",
                                             "category_confidence", "category_evidence")}})
        if apply and pending:
            report["written"] += db.save_job_categories(pending)
            actual = db.job_category_rows([r["url"] for r in pending], strict=True)
            for expected in pending:
                got = actual.get(expected["url"]) or {}
                if any((got.get(k) or "") != (expected.get(k) or "")
                       for k in db.CATEGORY_COLS):
                    raise RuntimeError("Category verification failed for %s" % expected["url"])
                report["verified"] += 1
        if offset == 0 or (offset // batch_size) % 10 == 0:
            print("Categories: examined %d/%d, changed %d, written %d (%.1fs)" % (
                report["examined"], len(rows), report["changed"], report["written"],
                time.monotonic() - started), flush=True)
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true", help="exit nonzero when stored categories differ")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--url", action="append", dest="urls")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.limit < 0 or not 1 <= args.batch_size <= 1000:
        parser.error("--limit must be nonnegative; --batch-size must be between 1 and 1000")
    report = run(args.apply, args.verify, args.limit, args.urls, args.batch_size)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "examples"}, indent=2))
    if not args.apply and not args.verify:
        print("DRY RUN: no writes. Apply MIGRATION_job_categories.sql, then run with --apply.")
    return 1 if args.verify and report["changed"] else 0


if __name__ == "__main__":
    sys.exit(main())
