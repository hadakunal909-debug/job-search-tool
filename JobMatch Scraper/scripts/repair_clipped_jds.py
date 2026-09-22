#!/usr/bin/env python3
"""Recover descriptions suspected of hitting the former 8k/12k character caps.

Dry run by default; --apply persists text, re-derives facts/terms/categories and
invalidates scores. Only a longer text retaining the entire old text is accepted.
Candidates and progress are bounded; a checkpoint resumes interrupted writes.

    python scripts/repair_clipped_jds.py --cache-only --limit 100 --report work/jd-repair.json
    python scripts/repair_clipped_jds.py --apply --limit 50 --max-seconds 180
    python scripts/repair_clipped_jds.py --url https://example.com/job/123 --apply
"""
import argparse
from collections import Counter
import datetime
import gzip
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("EV_OFF", "1")
import core
import db
from scraper import score_jobs as sj

LEGACY_CAPS = (8000, 12000)


def replacement_reason(stored, candidate):
    """Empty means safe extension; length alone cannot establish the same posting."""
    candidate = str(candidate or "")
    if not candidate.strip():
        return "source_unavailable"
    if core.jd_read_status(candidate)["status"] != "readable":
        return "source_unusable_or_still_clipped"
    if len(candidate) <= len(stored or ""):
        return "not_longer"
    # Formatting may have improved since capture. Compare every letter/number in
    # order, including the final partial word, so added bullets do not look like loss.
    if not core.jd_extends(stored, candidate):
        return "source_changed_requires_review"
    return ""


def repair_fields(row, text, idf):
    """Re-read every persisted fact from the replacement, including its recovered tail."""
    meta = core.job_meta(text, idf)
    loc = core.parse_location(row.get("location") or "", text)
    pay = core.parse_salary(text)
    verdict, reason = meta.get("sponsor_jd") or ("", "")
    return {"url": row["url"], "loc_state": loc["state"], "loc_metro": loc["metro"],
            "remote": bool(loc["remote"]), "salary_min": pay["min"],
            "salary_max": pay["max"], "salary_period": pay["period"],
            "exp_max_years": meta.get("exp_years"), "sponsor_jd": verdict,
            "sponsor_reason": reason, "facts_fp": db.jd_fingerprint(text),
            "jd_terms": core.pack_analyzed(meta.get("analyzed") or {}) or None}


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path):
    if not Path(path).exists():
        return {}
    # A broken checkpoint must fail loudly, not silently restart a network sweep.
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _cache_entries(path):
    """Stream the flat gzip JSON map; full cache loads exceed shared-host memory."""
    path = Path(path)
    if not path.exists():
        return
    decoder = json.JSONDecoder()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        buf, pos, eof = "", 0, False

        def fill():
            nonlocal buf, pos, eof
            chunk = fh.read(65536)
            buf, pos = buf[pos:] + chunk, 0
            eof = not chunk

        def peek():
            nonlocal pos
            while True:
                while pos < len(buf) and buf[pos].isspace():
                    pos += 1
                if pos < len(buf):
                    return buf[pos]
                if eof:
                    return ""
                fill()

        def consume(char):
            nonlocal pos
            if peek() != char:
                raise ValueError("Malformed JD cache; refused to overwrite it")
            pos += 1

        def value():
            nonlocal pos
            peek()
            while True:
                try:
                    result, pos = decoder.raw_decode(buf, pos)
                    return result
                except json.JSONDecodeError:
                    if eof:
                        raise
                    fill()

        consume("{")
        if peek() == "}":
            consume("}")
            return
        while True:
            key = value()
            consume(":")
            text = value()
            if not isinstance(key, str) or not isinstance(text, str):
                raise ValueError("JD cache must map URLs to text")
            yield key, text
            if peek() == "}":
                consume("}")
                if peek():
                    raise ValueError("Unexpected data after JD cache")
                return
            consume(",")


def _selected_cache(wanted):
    return {url: text for url, text in _cache_entries(sj.JD_CACHE_FILE) if url in wanted}


def _save_cache(replacements):
    """Merge from the latest cache with bounded memory and an atomic final rename."""
    path = Path(sj.JD_CACHE_FILE)
    tmp = path.with_name(path.name + ".repair.tmp")
    pending = dict(replacements)
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        fh.write("{")
        first = True
        for url, text in _cache_entries(path):
            if not first:
                fh.write(",")
            first = False
            json.dump(url, fh, ensure_ascii=False)
            fh.write(":")
            json.dump(pending.pop(url, text), fh, ensure_ascii=False)
        for url, text in pending.items():
            if not first:
                fh.write(",")
            first = False
            json.dump(url, fh, ensure_ascii=False)
            fh.write(":")
            json.dump(text, fh, ensure_ascii=False)
        fh.write("}")
    os.replace(tmp, path)


def persist_repairs(rows, replacements, idf=None):
    """Write complete text, refreshed analysis and cache; verify text before success."""
    db.update_jds(replacements)
    fields = [repair_fields(row, replacements[row["url"]], idf) for row in rows]
    db.update_job_fields(fields, keys=tuple(fields[0]))
    db.clear_scores_for_urls(replacements)
    db.requeue_analysis(replacements)
    _save_cache(replacements)
    if db.has_remote_db():
        actual = db._jd_rows(list(replacements))
    else:
        actual = db._load_json(db.JDS_FILE)
    if any(actual.get(url) != text for url, text in replacements.items()):
        raise RuntimeError("Full description verification failed; checkpoint retains pending rows")


def candidate_urls(urls=None):
    if urls:
        return sorted(set(urls))
    if db.has_remote_db():
        # Only URL/length metadata crosses the wire here. _fetch_all controls its own
        # page size, so --limit is applied before downloading any description text.
        rows = []
        for cap in LEGACY_CAPS:
            rows.extend(db._fetch_all(db.JD_TABLE, {"select": "url,jd_chars",
                                     "jd_chars": "eq.%d" % cap}))
        return sorted({r["url"] for r in rows if r.get("url")})
    return sorted({r["url"] for r in db.load_jobs(cols=["url", "jd"])
                   if r.get("url") and len(r.get("jd") or "") in LEGACY_CAPS})


def run(args):
    started = time.monotonic()
    state = _read_json(args.state)
    mode = "apply" if args.apply else "dry_run"
    ledger = state.setdefault(mode, {})
    now = time.time()
    urls = set(candidate_urls(args.url))
    urls.update(u for u, r in ledger.items() if r.get("status") == "pending")
    if args.host:
        urls = {u for u in urls if any((urlparse(u).hostname or "") == h
                or (urlparse(u).hostname or "").endswith("." + h) for h in args.host)}
    urls = sorted(u for u in urls if args.url or ledger.get(u, {}).get("status") == "pending"
                  or (not args.cache_only and ledger.get(u, {}).get("source") == "cache")
                  or now - ledger.get(u, {}).get("attempt_at", 0) >= args.retry_after_hours * 3600)
    eligible_count = len(urls)
    urls = urls[:args.limit]
    wanted = set(urls)
    cache = _selected_cache(wanted)
    idf = core.load_idf() if args.apply else None
    records = []
    report = {"mode": mode, "eligible": eligible_count, "selected": len(urls), "results": records,
              "note": "Legacy cap lengths are suspected truncation, not proof of incompleteness."}

    def checkpoint():
        report["counts"] = dict(Counter(r["status"] for r in records))
        report["elapsed_seconds"] = round(time.monotonic() - started, 1)
        report["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        _write_json(args.state, state)
        _write_json(args.report, report)

    for offset in range(0, len(urls), args.batch_size):
        if time.monotonic() - started >= args.max_seconds:
            break
        batch_urls = urls[offset:offset + args.batch_size]
        rows = {r["url"]: r for r in db.load_jobs_by_urls(batch_urls) if r.get("url")}
        replacements, replacement_rows, pending_records = {}, [], []
        for url in batch_urls:
            if time.monotonic() - started >= args.max_seconds:
                break
            row = rows.get(url)
            stored = (row or {}).get("jd") or ""
            rec = {"url": url, "host": urlparse(url).hostname, "old_chars": len(stored),
                   "new_chars": 0, "source": "", "status": ""}
            previous = ledger.get(url) or {}
            if row is None:
                rec["status"] = "job_missing"
            elif str(row.get("is_active", "true")).lower() == "false" and not args.url:
                rec["status"] = "inactive_skipped"
            elif previous.get("status") == "pending" and db.jd_fingerprint(stored) == previous.get("new_fp"):
                replacements[url] = stored
                rec.update(status="recovered", source="resume_pending_write", new_chars=len(stored))
            elif len(stored) not in LEGACY_CAPS:
                rec["status"] = "not_at_legacy_cap"
            else:
                candidate = cache.get(url) or ""
                reason = replacement_reason(stored, candidate)
                rec["source"] = "cache"
                if reason and not args.cache_only:
                    rec["source"] = "source_page"
                    try:
                        candidate = sj.detail_jd(url)[1] or ""
                        reason = replacement_reason(stored, candidate)
                    except Exception:
                        candidate, reason = "", "source_fetch_failed"
                rec["new_chars"] = len(candidate)
                rec["status"] = reason or ("recovered" if args.apply else "would_recover")
                if not reason and args.apply:
                    replacements[url] = candidate
            records.append(rec)
            if url in replacements:
                replacement_rows.append(row)
                pending_records.append(rec)
                ledger[url] = {"status": "pending", "attempt_at": now,
                               "new_fp": db.jd_fingerprint(replacements[url])}
            else:
                ledger[url] = {"status": rec["status"], "attempt_at": now, "source": rec["source"]}
        # Record pending intent BEFORE any database mutation. A crash after text writes
        # resumes the facts/cache/score steps even though that row no longer hits a cap.
        checkpoint()
        if replacements:
            try:
                persist_repairs(replacement_rows, replacements, idf)
            except Exception:
                for rec in pending_records:
                    rec["status"] = "write_failed_pending_retry"
                checkpoint()
                raise
            for rec in pending_records:
                ledger[rec["url"]] = {"status": "recovered", "attempt_at": now}
            checkpoint()
        print("%s: checked %d/%d; %s" % (mode, len(records), len(urls), report["counts"]), flush=True)
    report["remaining_selected"] = len(urls) - len(records)
    checkpoint()
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="explicitly select the default read-only mode")
    ap.add_argument("--cache-only", action="store_true", help="recover cached full text without fetching employer pages")
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--host", action="append", default=[])
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--batch-size", type=int, default=20)
    ap.add_argument("--max-seconds", type=float, default=180)
    ap.add_argument("--retry-after-hours", type=float, default=24)
    ap.add_argument("--state", default="work/jd-repair-state.json")
    ap.add_argument("--report", default="work/jd-repair-report.json")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        ap.error("choose --apply or --dry-run")
    if args.limit < 1 or args.batch_size < 1 or args.max_seconds <= 0:
        ap.error("limit, batch-size and max-seconds must be positive")
    report = run(args)
    print(json.dumps({k: v for k, v in report.items() if k != "results"}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
