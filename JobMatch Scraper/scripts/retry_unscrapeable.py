#!/usr/bin/env python3
"""Preserve and retry the historical unscrapeable/JobSpy employer queue.

This script never writes a database. It snapshots every input and keeps one ordered
report per input, a master employer report, raw scraped postings and append-only
evidence. A resume uses the immutable manifest, not possibly changed input files.

    python scripts/retry_unscrapeable.py --snapshot snapshot.json --out-dir retry_2026_09_14 --prepare-only
    python scripts/retry_unscrapeable.py --out-dir retry_2026_09_14 --resume --workers 6 --timeout 180

Each employer runs in an isolated subprocess. The parent imposes a hard deadline;
file locks pace requests to each host across all subprocesses. An interruption
cannot change the old CSVs, board configuration, jobs, or prior evidence.
"""
import argparse
import collections
import concurrent.futures
import csv
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET
import zipfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
REPORT_FIELDS = ["candidate_id", "retry_status", "readable_boards", "confirmed_boards",
                 "fetched", "title_pass", "us_title_pass", "board_urls", "evidence_file"]


def normal_name(value):
    """Exact alphanumeric match only: Acme and Acme LLC remain different employers."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_new_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2, default=str)
        fh.write("\n")


def append_json(path, value):
    # Separate a killed writer's incomplete tail from the next complete event.
    path = Path(path)
    if path.exists() and path.stat().st_size:
        with path.open("rb+") as tail:
            tail.seek(-1, 2)
            if tail.read(1) != b"\n":
                tail.seek(0, 2)
                tail.write(b"\n")
    with Path(path).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl(path):
    if not Path(path).exists():
        return []
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # A killed process can leave its final line incomplete; preceding evidence survives.
            continue
    return rows


def xlsx_rows(path):
    """Read string/value cells with stdlib only; preserve worksheet row numbers."""
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as z:
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            strings = ["".join(t.text or "" for t in si.findall(".//s:t", ns))
                       for si in root.findall("s:si", ns)]
        for sheet in sorted(n for n in z.namelist()
                            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
            header = None
            for row in ET.fromstring(z.read(sheet)).findall(".//s:sheetData/s:row", ns):
                cells = {}
                for cell in row.findall("s:c", ns):
                    letters = re.match(r"[A-Z]+", cell.get("r", "A1")).group()
                    col = 0
                    for letter in letters:
                        col = col * 26 + ord(letter) - 64
                    raw = cell.find("s:v", ns)
                    value = raw.text if raw is not None and raw.text is not None else ""
                    if cell.get("t") == "s":
                        value = strings[int(value)] if value else ""
                    elif cell.get("t") == "inlineStr":
                        value = "".join(t.text or "" for t in cell.findall(".//s:t", ns))
                    cells[col - 1] = value
                if not cells:
                    continue
                values = [cells.get(i, "") for i in range(max(cells) + 1)]
                if header is None:
                    header = values
                    continue
                yield sheet, int(row.get("r", "0")), dict(zip(header, values + [""] * len(header)))


def input_paths(input_dir, explicit=()):
    base = Path(input_dir)
    if explicit:
        return [Path(p).resolve() for p in explicit]
    paths = []
    original = base / "unscrapeable_boards.csv"
    if original.exists():
        paths.append(original)
    paths.extend(sorted(base.glob("jobspy*.csv")))
    paths.extend(sorted(base.glob("*jobspy*.xlsx")))
    paths.extend(sorted(base.glob("review_findings*.xlsx")))
    if (base / "unscrapeable_boards.xlsx").exists():
        paths.append(base / "unscrapeable_boards.xlsx")
    return [p.resolve() for p in paths]


def prepare(out_dir, paths, snapshot=None, sources=None):
    """Create an immutable input manifest. Injectable sources keep tests offline."""
    out = Path(out_dir).resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError("output directory is not empty; choose a new one or use --resume")
    out.mkdir(parents=True, exist_ok=True)
    for folder in ("originals", "evidence", "raw_jobs", "workers", "locks", "reports"):
        (out / folder).mkdir(exist_ok=True)
    manifest = {"version": 1, "created_at": utcnow(), "sources": [], "rows": [], "candidates": []}
    by_name = collections.OrderedDict()

    def add_record(source, line, original, sheet=""):
        name = str(original.get("company") or original.get("employer") or "").strip()
        key = normal_name(name)
        cid = "employer_" + digest(key)[:20] if key else ""
        rid = "row_" + digest("%s:%s:%s" % (source["sha256"], sheet, line))[:24]
        item = {"row_id": rid, "source_id": source["source_id"], "source_file": source["path"],
                "source_line": line, "sheet": sheet, "candidate_id": cid, "original": original}
        manifest["rows"].append(item)
        if not key:
            return
        candidate = by_name.setdefault(key, {"candidate_id": cid, "company": name, "aliases": [],
                                             "source_row_ids": [], "records": [], "known_boards": []})
        if name not in candidate["aliases"]:
            candidate["aliases"].append(name)
        candidate["source_row_ids"].append(rid)
        candidate["records"].append(item)

    def snapshot_file(path):
        path = Path(path).resolve()
        data = path.read_bytes()
        index = len(manifest["sources"])
        target = out / "originals" / ("%03d_%s" % (index, path.name))
        with target.open("xb") as fh:
            fh.write(data)
        entry = {"source_id": "source_%03d" % index, "path": str(path),
                 "copy": str(target.relative_to(out)), "sha256": hashlib.sha256(data).hexdigest(),
                 "bytes": len(data)}
        manifest["sources"].append(entry)
        return entry, target

    for path in paths:
        source, copied = snapshot_file(path)
        if copied.suffix.lower() == ".xlsx":
            for sheet, line, row in xlsx_rows(copied):
                add_record(source, line, row, sheet)
        else:
            with copied.open(encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                reader.fieldnames  # Read the header before tracking physical line starts.
                for row in reader:
                    # source_line_end also records multi-line CSV fields without information loss.
                    line_end = reader.line_num
                    add_record(source, getattr(reader, "_previous_end", 1) + 1, row)
                    manifest["rows"][-1]["source_line_end"] = line_end
                    reader._previous_end = line_end
    snap = {}
    if snapshot:
        source, copied = snapshot_file(snapshot)
        snap = read_json(copied)
        for index, row in enumerate(snap.get("findings") or [], 1):
            add_record(source, index, row, "findings")
    if sources is None:
        import scraper
        sources = scraper.SOURCES
    boards = [{"url": u, "ats_type": ats, "company": name, "origin": "SOURCES"}
              for u, ats, name in sources]
    boards += [dict(b, origin="snapshot_boards") for b in (snap.get("boards") or [])]
    for board in boards:
        candidate = by_name.get(normal_name(board.get("company")))
        if candidate and board.get("url"):
            candidate["known_boards"].append(board)
    manifest["candidates"] = list(by_name.values())
    write_new_json(out / "manifest.json", manifest)
    render_reports(out, manifest)
    return manifest


def http_url(value):
    value = str(value or "").strip()
    return value if value.startswith(("https://", "http://")) else ""


def candidate_links(candidate):
    """Keep ALL distinct previously observed links, rather than the first per employer."""
    boards, pages, direct = [], [], []
    for board in candidate.get("known_boards", []):
        boards.append((board["url"], board["ats_type"], "held_exact_employer", "high"))
    for record in candidate["records"]:
        row = record["original"]
        url = http_url(row.get("board_url"))
        if url:
            boards.append((url, row.get("ats_type") or row.get("ats") or "", "prior_board", "low"))
        for field in ("careers_page", "career_page", "careers_url"):
            url = http_url(row.get(field))
            if url:
                pages.append(url)
        for field in ("url", "job_url_direct", "job_url", "direct_url", "example_url"):
            url = http_url(row.get(field))
            if url:
                direct.append(url)
    return list(dict.fromkeys(boards)), list(dict.fromkeys(pages)), list(dict.fromkeys(direct))


class DeadlineSession:
    """Request deadline and cross-process per-host lock, including adapter child threads."""
    def __init__(self, out, deadline, events):
        import requests
        self.session = requests.Session()
        self.out, self.deadline, self.events = Path(out), deadline, events
        self.local = threading.local()

    def __getattr__(self, name):
        return getattr(self.session, name)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        host = urlparse(url).netloc.lower()
        path = self.out / "locks" / (digest(host) + ".lock")
        with path.open("a+b") as fh:
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()
            locked = False
            while not locked:
                if time.monotonic() >= self.deadline:
                    raise TimeoutError("employer request budget exhausted")
                try:
                    fh.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except (OSError, BlockingIOError):
                    time.sleep(0.05)
            try:
                requested = kwargs.get("timeout", 12)
                if not isinstance(requested, (int, float)):
                    requested = 12
                request_cap = 30 if urlparse(url).path.endswith('/postings/all_jobs.atom') else 12
                remaining = max(0.1, min(requested, request_cap, self.deadline - time.monotonic()))
                kwargs["timeout"] = remaining
                kwargs.setdefault("allow_redirects", True)
                response = self.session.request(method, url, **kwargs)
                if response.status_code >= 400:
                    self.events.append({"url": url, "http_status": response.status_code})
                return response
            except Exception as exc:
                self.events.append({"url": url, "error": type(exc).__name__, "detail": str(exc)[:300]})
                raise
            finally:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_UN)


def summarize(candidate, attempts, status=None):
    boards = [a for a in attempts if a.get("kind") == "board"]
    readable = [a for a in boards if a.get("fetched", 0) > 0]
    confirmed = [a for a in readable if a.get("verdict") == "confirmed" and a.get("complete", True) and not a.get("error") and a.get("scope") != "single_posting"]
    if not status:
        status = ("scrapeable" if confirmed else
                  "partial_readable" if any(not a.get("complete", True) for a in readable) else
                  "saved_posting_readable" if readable and all(a.get("scope") == "single_posting" for a in readable) else
                  "identity_review" if readable else
                  "fetch_error" if any(a.get("error") for a in attempts) else
                  "empty" if boards else "unresolved")
    return {"candidate_id": candidate["candidate_id"], "company": candidate["company"],
            "retry_status": status, "readable_boards": len(readable), "confirmed_boards": len(confirmed),
            "fetched": sum(a.get("fetched", 0) for a in readable),
            "title_pass": sum(a.get("title_pass", 0) for a in readable),
            "us_title_pass": sum(a.get("us_title_pass", 0) for a in readable),
            "board_urls": " | ".join(a["url"] for a in readable),
            "evidence_file": "evidence/%s.jsonl" % candidate["candidate_id"],
            "completed_at": utcnow(), "attempts": attempts}


def retry_candidate(candidate, out_dir, timeout=180):
    """Run in a child process. All file writes are exclusive or append-only."""
    import scraper
    import core
    import db
    from scraper import find_everify_boards as feb
    from scraper.probe_migratemate import grade, board_reported_name
    out = Path(out_dir)
    deadline = time.monotonic() + timeout
    network_events, attempts = [], []
    scraper.SESSION = DeadlineSession(out, deadline, network_events)
    # Some existing adapters maintain database ledgers. A recovery run uses isolated
    # in-memory ledgers so even those adapters cannot mutate production or local data.
    ledger = {}
    db.get_kv = lambda key, *a, **kw: ledger.get(key)
    db.put_kv = lambda key, value, *a, **kw: ledger.update({key: value})
    db.list_boards = lambda: []
    def refuse_write(*args, **kwargs):
        raise RuntimeError("database writes disabled in retry worker")
    for name in ("add_board", "delete_board", "add_findings", "save_jobs", "insert_jobs", "_upsert", "_dump_json"):
        if hasattr(db, name):
            setattr(db, name, refuse_write)
    evidence = out / "evidence" / (candidate["candidate_id"] + ".jsonl")
    tried_boards, tried_pages = set(), set()

    def emit(item):
        item["at"] = utcnow()
        attempts.append(item)
        append_json(evidence, item)

    def check_time():
        if time.monotonic() >= deadline:
            raise TimeoutError("employer time budget exhausted")

    def fetch_board(url, ats, provenance, confidence):
        check_time()
        key = (url.rstrip("/"), ats)
        if key in tried_boards or not ats or ats not in scraper.SCRAPERS:
            return
        tried_boards.add(key)
        rec = {"kind": "board", "url": url, "ats_type": ats, "provenance": provenance,
               "confidence": confidence, "fetched": 0, "title_pass": 0, "us_title_pass": 0}
        if provenance == "posting_structured_data":
            rec["scope"] = "single_posting"
        start = len(network_events)
        trunc_start = len(scraper.TRUNCATED)
        rows = []
        try:
            reported = board_reported_name(url, ats)
            rec["reported_name"] = reported
            rec["verdict"], rec["identity_score"] = grade(candidate["company"], url, ats, reported, confidence)
            emit({"kind": "board_started", "url": url, "ats_type": ats})
            rows = scraper.SCRAPERS[ats](url)
            rows = rows if rows is not None else []
            rec["complete"] = getattr(rows, "complete", True)
            if not rec["complete"]:
                rec["error"] = getattr(rows, "reason", "") or "incomplete pagination"
        except Exception as exc:
            rows = getattr(exc, "rows", [])
            rec["complete"] = False
            rec["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:500])
        # Persist retrieved jobs BEFORE optional metrics. Statistics cannot erase a scrape.
        try:
            raw = "raw_jobs/%s_%s.json" % (candidate["candidate_id"], digest(url + ats)[:16])
            rec["raw_jobs_file"] = raw
            if not (out / raw).exists():
                write_new_json(out / raw, {"company": candidate["company"], "board_url": url,
                                           "ats_type": ats, "fetched_at": utcnow(), "complete": rec.get("complete"), "jobs": rows})
            rec["fetched"] = len(rows)
            kept = [r for r in rows if scraper.title_verdict(r.get("title", ""))[0]]
            rec["title_pass"] = len(kept)
            rec["us_title_pass"] = sum(scraper.is_us_location(r.get("location", "")) for r in kept)
            rec["network_issues"] = network_events[start:]
            rec["fetch_status"] = "readable" if rows else "error" if rec["network_issues"] else "empty"
            if rows and rec["network_issues"]:
                rec["complete"] = False
                rec["completeness_note"] = "One or more requests failed; returned listings are preserved, total coverage unverified"
            if not rows and rec["network_issues"]:
                rec["error"] = "HTTP/request failure; the adapter returned no rows"
            rec["truncation"] = scraper.TRUNCATED[trunc_start:]
            if rec["truncation"]:
                rec["complete"] = False
        except Exception as exc:
            rec["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:500])
            rec["fetch_status"] = "error"
            rec["network_issues"] = network_events[start:]
        emit(rec)

    def inspect_page(url, provenance, confidence):
        check_time()
        if url in tried_pages:
            return
        tried_pages.add(url)
        event = {"kind": "page", "url": url, "provenance": provenance}
        try:
            hit = scraper.detect_board(url)
            if hit:
                fetch_board(hit[0], hit[1], provenance, confidence)
                return
            response = scraper._safe_get(url, timeout=12)
            event.update(http_status=response.status_code, resolved_url=response.url)
            if response.status_code != 200:
                event["error"] = "HTTP %s" % response.status_code
                emit(event)
                return
            # Keep the FULL redirect and original careers path; origin-only resolution loses tenants.
            hits = []
            hit = scraper.detect_board(response.url)
            if hit:
                hits.append(hit)
            html = (response.text or "").replace("\\/", "/").replace("&amp;", "&")
            if 'pinpoint-block--jobs' in html:
                hits.append((urljoin(response.url, '/'), 'pinpoint', ''))
            if 'avature.portal.urlPath' in html:
                hit = scraper.avature_from_html(html, response.url)
                if hit:
                    hits.append(hit)
            if '/postings/all_jobs.atom' in html:
                from scraper.peopleadmin import listing_info
                feed, _count = listing_info(html, response.url)
                if feed:
                    hits.append((urljoin(feed, '/postings/search'), 'peopleadmin', ''))
            for match in scraper._ATS_LINK_RE.finditer(html):
                hit = scraper.detect_board(match.group(0))
                if hit and hit not in hits:
                    hits.append(hit)
            for hit in hits:
                fetch_board(hit[0], hit[1], provenance, confidence)
            if not hits:
                detectors = [scraper.detect_paylocity] if "paylocity.com" in urlparse(response.url).netloc else []
                lower = html.lower()
                markers = [("phenom", scraper.detect_phenom), ("phapp", scraper.detect_phenom),
                           ("successfactors", scraper.detect_successfactors), ("jobtitle-link", scraper.detect_successfactors),
                           ("jibe", scraper.detect_jibe), ("icims", scraper.detect_jibe),
                           ("eightfold", scraper.detect_eightfold), ("/search-jobs/results", scraper.detect_radancy)]
                detectors += list(dict.fromkeys(fn for marker, fn in markers if marker in lower))
                for detector in detectors:
                    check_time()
                    hit = detector(response.url)
                    if hit:
                        fetch_board(hit[0], hit[1], provenance, confidence)
                        if any(a.get("fetched") for a in attempts if a.get("url") == hit[0]):
                            break
                if provenance == "posting_direct_url" and 'jobposting' in lower and not any(a.get("fetched") for a in attempts):
                    fetch_board(response.url, "jsonld", "posting_structured_data", "low")
                # Current careers pages often link to a separate employer-owned listing.
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html.parser")
                for anchor in soup.select('a[href]'):
                    target = urljoin(response.url, anchor.get("href", ""))
                    if re.search(r"/search-jobs/?(?:\?|$)", target) and target not in tried_pages:
                        hit = scraper.detect_radancy(target)
                        if hit:
                            fetch_board(hit[0], hit[1], provenance, confidence)
                            break
            event["matched_links"] = len(hits)
        except Exception as exc:
            event["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        emit(event)

    try:
        prior_boards, pages, directs = candidate_links(candidate)
        for url, ats, provenance, confidence in prior_boards:
            hit = scraper.detect_board(url) if not ats else None
            if hit:
                url, ats = hit[:2]
            fetch_board(url, ats, provenance, confidence)
            if not ats:
                inspect_page(url, provenance, confidence)
        if any(a.get("fetched") and a.get("complete", True) and a.get("verdict") == "confirmed" for a in attempts):
            return summarize(candidate, attempts)
        for url in pages:
            inspect_page(url, "original_careers_page", "high")
        for url in directs:
            if core.is_aggregator_url(url):
                emit({"kind": "page", "url": url, "provenance": "posting",
                      "result": "aggregator URL preserved; no direct employer endpoint"})
                continue
            inspect_page(url, "posting_direct_url", "low")
            if any(a.get("fetched") and a.get("complete", True) and a.get("verdict") == "confirmed" for a in attempts):
                break
        if not any(a.get("fetched", 0) for a in attempts):
            check_time()
            _name, url, ats, count, confidence = feb.discover(candidate["company"])
            emit({"kind": "discovery", "url": url or "", "ats_type": ats,
                  "probe_count": count, "confidence": confidence})
            if url:
                fetch_board(url, ats, "name_discovery", confidence)
    except TimeoutError as exc:
        emit({"kind": "budget", "error": str(exc)})
        return summarize(candidate, attempts, "timeout_partial" if any(a.get("fetched") for a in attempts) else "timeout")
    if time.monotonic() >= deadline and not any(a.get("fetched") for a in attempts):
        return summarize(candidate, attempts, "timeout")
    return summarize(candidate, attempts)


def run_one(candidate, out, timeout):
    """Hard timeout isolates adapters that hang or swallow request timeouts."""
    out = Path(out)
    cid = candidate["candidate_id"]
    request = out / "workers" / (cid + ".input.json")
    if not request.exists():
        write_new_json(request, candidate)
    command = [sys.executable, str(Path(__file__).resolve()), "--_worker", str(request),
               "--out-dir", str(out), "--timeout", str(timeout)]
    env = dict(os.environ, EV_OFF="1", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    for key in ("PG_DSN", "DB_PROXY_URL", "DB_PROXY_SECRET", "DB_REQUIRE"):
        env.pop(key, None)
    log = out / "workers" / (cid + ".log")
    try:
        with log.open("ab") as fh:
            completed = subprocess.run(command, cwd=APP, env=env, stdout=fh, stderr=subprocess.STDOUT,
                                       timeout=timeout + 15, check=False)
        result_path = out / "workers" / (cid + ".result.json")
        if completed.returncode == 0 and result_path.exists():
            return read_json(result_path)
        error = "worker exit %s; see workers/%s.log" % (completed.returncode, cid)
    except subprocess.TimeoutExpired:
        error = "hard employer timeout after %s seconds" % (timeout + 15)
    attempts = read_jsonl(out / "evidence" / (cid + ".jsonl"))
    attempts.append({"kind": "worker", "error": error, "at": utcnow()})
    append_json(out / "evidence" / (cid + ".jsonl"), attempts[-1])
    return summarize(candidate, attempts, "timeout_partial" if any(a.get("fetched") for a in attempts) else "timeout")


def render_reports(out_dir, manifest=None):
    """Derived reports may be regenerated; originals/manifest/checkpoints are immutable."""
    out = Path(out_dir)
    manifest = manifest or read_json(out / "manifest.json")
    done = {r["candidate_id"]: r for r in read_jsonl(out / "checkpoints.jsonl")}
    master = []
    for candidate in manifest["candidates"]:
        result = done.get(candidate["candidate_id"], {"candidate_id": candidate["candidate_id"],
                                                     "retry_status": "pending"})
        master.append(dict(result, company=candidate["company"], source_rows=len(candidate["records"])))

    def write_csv(path, fields, rows):
        temp = path.with_suffix(path.suffix + ".tmp")
        with temp.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp, path)

    write_csv(out / "master.csv", ["company", "source_rows"] + REPORT_FIELDS, master)
    for source in manifest["sources"]:
        records = [r for r in manifest["rows"] if r["source_id"] == source["source_id"]]
        original_fields = list(dict.fromkeys(k for r in records for k in r["original"] if k is not None))
        appended = ["retry_" + f if f in original_fields else f for f in REPORT_FIELDS]
        rows = []
        for record in records:
            result = done.get(record["candidate_id"], {"candidate_id": record["candidate_id"],
                        "retry_status": "pending" if record["candidate_id"] else "no_employer_name"})
            row = dict(record["original"])
            row.update({new: result.get(old, "") for old, new in zip(REPORT_FIELDS, appended)})
            row.update(source_file=record["source_file"], source_line=record["source_line"],
                       source_sheet=record["sheet"], source_row_id=record["row_id"],
                       original_json=json.dumps(record["original"], ensure_ascii=False))
            rows.append(row)
        target = out / "reports" / (source["source_id"] + "_" + Path(source["path"]).stem + ".csv")
        write_csv(target, original_fields + appended + ["source_file", "source_line", "source_sheet",
                                                        "source_row_id", "original_json"], rows)
    summary = {"updated_at": utcnow(), "source_files": len(manifest["sources"]),
               "source_rows": len(manifest["rows"]), "employers": len(master), "completed": len(done),
               "pending": len(master) - len(done),
               "statuses": dict(collections.Counter(r["retry_status"] for r in master)),
               "employers_with_readable_jobs": sum(bool(r.get("readable_boards")) for r in master),
               "raw_postings": sum(r.get("fetched", 0) for r in master),
               "note": "Counts are observed rows, not deduplicated jobs. No database writes or adoption."}
    temp = out / "summary.json.tmp"
    temp.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, out / "summary.json")
    return summary


def recover_completed_workers(out, manifest):
    """A stopped report writer must not cause completed employer work to be rerun."""
    out = Path(out)
    known = {c['candidate_id']: c for c in manifest['candidates']}
    done = {r['candidate_id'] for r in read_jsonl(out/'checkpoints.jsonl')}
    recovered = 0
    for path in sorted((out/'workers').glob('*.result.json')):
        try:
            result = read_json(path)
        except (OSError, ValueError):
            continue
        cid = result.get('candidate_id')
        if cid in known and cid not in done and result.get('retry_status'):
            append_json(out/'checkpoints.jsonl', result)
            done.add(cid)
            recovered += 1
    for cid, candidate in known.items():
        if cid in done:
            continue
        attempts = read_jsonl(out/'evidence'/(cid+'.jsonl'))
        if attempts and attempts[-1].get('kind') == 'worker':
            status = 'timeout_partial' if any(a.get('fetched') for a in attempts) else 'timeout'
            append_json(out/'checkpoints.jsonl', summarize(candidate, attempts, status))
            done.add(cid)
            recovered += 1
    return recovered


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--input-dir", default=str(APP))
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--snapshot")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--_worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    out = Path(args.out_dir).resolve()
    if args._worker:
        candidate = read_json(args._worker)
        result = retry_candidate(candidate, out, args.timeout)
        path = out / "workers" / (candidate["candidate_id"] + ".result.json")
        if not path.exists():
            write_new_json(path, result)
        return 0
    if args.timeout <= 0 or args.workers < 1:
        parser.error("timeout and workers must be positive")
    if args.resume:
        manifest = read_json(out / "manifest.json")
        recovered = recover_completed_workers(out, manifest)
        if recovered:
            print('Recovered %d completed worker results.' % recovered, flush=True)
    else:
        manifest = prepare(out, input_paths(args.input_dir, args.input), args.snapshot)
    if args.prepare_only:
        print(json.dumps(render_reports(out, manifest), indent=2))
        return 0
    done = {r["candidate_id"] for r in read_jsonl(out / "checkpoints.jsonl")}
    pending = [c for c in manifest["candidates"] if c["candidate_id"] not in done]
    if args.limit:
        pending = pending[:args.limit]
    print("Retrying %d employers; %d already checkpointed. No database writes." % (len(pending), len(done)), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(run_one, c, out, args.timeout): c for c in pending}
        for future in concurrent.futures.as_completed(futures):
            candidate = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = summarize(candidate, [{"kind": "parent", "error": "%s: %s" % (type(exc).__name__, exc)}], "worker_error")
            append_json(out / "checkpoints.jsonl", result)
            print("%s: %s; %d rows from %d readable boards" % (candidate["company"], result["retry_status"],
                  result["fetched"], result["readable_boards"]), flush=True)
            if len(read_jsonl(out / "checkpoints.jsonl")) % 25 == 0:
                try:
                    render_reports(out, manifest)
                except OSError as exc:
                    # A spreadsheet viewer or antivirus can briefly lock a derived CSV.
                    # Checkpoint every result anyway; regenerate reports on the next batch.
                    print('Report refresh deferred: %s' % exc, flush=True)
    print(json.dumps(render_reports(out, manifest), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
