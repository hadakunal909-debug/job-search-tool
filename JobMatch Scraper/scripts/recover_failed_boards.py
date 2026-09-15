"""Triage saved failures offline, then retry a supported platform without slug guessing.

Examples:
  python scripts/recover_failed_boards.py --out outputs/recovery_2026-09-15
  python scripts/recover_failed_boards.py --out outputs/recovery_2026-09-15 --adp

Existing results are reused. Requests are paced, have no automatic retries, and
each employer has a 75-second budget. Nothing here writes jobs or adopts boards.
"""
import argparse
import collections
import csv
import json
import os
from pathlib import Path
import sys
import time

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
os.environ.setdefault("EV_OFF", "1")
import core
import scraper
from scraper import adp, paycom, trinethire
from scripts.retry_unscrapeable import candidate_links, normal_name

FAILED = {"unresolved", "timeout", "fetch_error"}


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def triage(base, out, platform="adp"):
    manifest = json.loads((base / "run/manifest.json").read_text(encoding="utf-8"))
    results = {r["candidate_id"]: r for r in csv.DictReader((base / "final/employer_results.csv").open(encoding="utf-8-sig"))}
    snapshot = json.loads((base / "snapshot.json").read_text(encoding="utf-8"))
    names = {normal_name(c) for u, a, c in scraper.SOURCES}
    names.update(normal_name(b["company"]) for b in snapshot["boards"])
    rows, targets = [], []
    for c in manifest["candidates"]:
        r = results[c["candidate_id"]]
        if r["status"] not in FAILED:
            continue
        boards, pages, direct = candidate_links(c)
        employer_links = [u for u in direct if not core.is_aggregator_url(u)]
        bucket = ("already_configured_name" if normal_name(c["company"]) in names else
                  "prior_board_available" if boards else "careers_page_available" if pages else
                  "direct_posting_available" if employer_links else "aggregator_links_only")
        urls = list(dict.fromkeys([u for u, a, p, f in boards] + pages + employer_links))
        found = {}
        for url in urls:
            hit = scraper.detect_board(url)
            if hit and hit[1] == platform:
                found[hit[0]] = url
        rows.append({"company": c["company"], "candidate_id": c["candidate_id"],
                     "previous_status": r["status"], "next_route": bucket,
                     "careers_pages": " | ".join(pages), "direct_links": " | ".join(employer_links),
                     platform + "_boards": " | ".join(found)})
        for board, evidence in found.items():
            targets.append({"company": c["company"], "candidate_id": c["candidate_id"],
                            "previous_status": r["status"], "board_url": board, "source_url": evidence})
    write_csv(out / "triage.csv", rows)
    (out / (platform + "_targets.json")).write_text(json.dumps(targets, indent=2), encoding="utf-8")
    counts = dict(collections.Counter(r["next_route"] for r in rows))
    (out / "triage_summary.json").write_text(json.dumps({"employers": len(rows), "buckets": counts,
        platform + "_boards": len(targets), "note": "Configured names are based on current built-ins and the saved board snapshot, not a fresh scrape."}, indent=2))
    print("Offline triage:", counts, platform + " targets:", len(targets), flush=True)
    return targets


class PacedSession:
    def __init__(self):
        import requests
        self.session = requests.Session()
        self.deadline = 0
        self.last = 0

    def get(self, url, **kwargs):
        return self.request("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("post", url, **kwargs)

    def request(self, method, url, **kwargs):
        time.sleep(max(0, 0.3 - (time.monotonic() - self.last)))
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("Recovery employer budget exhausted")
        kwargs["timeout"] = min(float(kwargs.get("timeout", 15)), left)
        try:
            return self.session.request(method, url, **kwargs)
        finally:
            self.last = time.monotonic()


def run_platform(targets, out, retry_errors=False, platform="adp"):
    adapter = {"adp": adp, "paycom": paycom, "trinethire": trinethire}[platform]
    checkpoint = out / (platform + "_results.jsonl")
    prior = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()] if checkpoint.exists() else []
    latest = {(r["candidate_id"], r["board_url"]): r for r in prior}
    done = {key for key, r in latest.items() if not retry_errors or r["status"] != "fetch_error"}
    session = PacedSession()
    original = scraper.SESSION
    scraper.SESSION = session
    (out / "raw_jobs").mkdir(exist_ok=True)
    try:
        for target in targets:
            if (target["candidate_id"], target["board_url"]) in done:
                continue
            session.deadline = time.monotonic() + 75
            result = dict(target, fetched=0, us_title_pass=0, description_chars=0, identity_text="", status="fetch_error")
            rows = []
            try:
                try:
                    result["identity_text"] = adapter.identity_text(target["board_url"])
                except Exception as exc:
                    result["identity_error"] = type(exc).__name__
                rows = scraper.SCRAPERS[platform](target["board_url"])
                result["status"] = "readable" if rows else "empty"
                eligible = [r for r in rows if scraper.title_verdict(r["title"])[0] and scraper.is_us_location(r["location"])]
                result["us_title_pass"] = len(eligible)
                sample = (eligible or rows)[:1]
                if sample:
                    jd, date = adapter.detail_jd(sample[0]["url"])
                    sample[0]["jd"] = jd
                    result["description_chars"] = len(jd)
                    result["sample_title"] = sample[0]["title"]
                # Identity is deliberately a separate human review; a readable API is not proof.
            except Exception as exc:
                rows = getattr(exc, "rows", rows)
                result["status"] = "partial_readable" if rows else "fetch_error"
                result["error"] = type(exc).__name__ + ": " + str(exc)[:200]
            result["fetched"] = len(rows)
            result["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            raw = out / "raw_jobs" / (target["candidate_id"] + "_" + str(len(prior)) + ".json")
            raw.write_text(json.dumps({"company": target["company"], "board_url": target["board_url"], "jobs": rows}), encoding="utf-8")
            result["raw_file"] = str(raw)
            with checkpoint.open("a", encoding="utf-8") as f:
                f.write(json.dumps(result) + "\n")
            prior.append(result)
            latest[(target["candidate_id"], target["board_url"])] = result
            print(result["company"], result["status"], result["fetched"], "jobs;", result["us_title_pass"], "US title matches", flush=True)
    finally:
        scraper.SESSION = original
    fields = ["company", "previous_status", "status", "fetched", "us_title_pass", "description_chars", "board_url", "source_url", "identity_text", "raw_file"]
    write_csv(out / (platform + "_review.csv"), [{k: r.get(k, "") for k in fields} for r in latest.values()])


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", type=Path, default=APP / "outputs/retry_2026-09-14")
    p.add_argument("--out", type=Path, required=True)
    platforms = p.add_mutually_exclusive_group()
    platforms.add_argument("--adp", action="store_true")
    platforms.add_argument("--paycom", action="store_true")
    platforms.add_argument("--trinethire", action="store_true")
    p.add_argument("--retry-errors", action="store_true", help="Retry only failed platform targets after an adapter fix")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    platform = "trinethire" if args.trinethire else "paycom" if args.paycom else "adp"
    targets = triage(args.base, args.out, platform)
    if args.adp or args.paycom or args.trinethire:
        run_platform(targets, args.out, args.retry_errors, platform)
