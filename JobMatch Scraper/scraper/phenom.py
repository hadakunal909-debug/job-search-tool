"""Complete listings from the public widget used by Phenom career searches."""
from urllib.parse import urlparse
import time

from scraper.infosys import PartialScrapeError

PAGE_SIZE = 500
MAX_JOBS = 50000
RESULT_WINDOW = 9500
MAX_REQUESTS = 250


def scrape_phenom(board_url):
    import scraper
    p = urlparse(board_url)
    base = "%s://%s" % (p.scheme or "https", p.netloc)
    rows, seen, requests = [], set(), [0]

    def consume(jobs, local):
        for job in jobs:
            title = (job.get("title") or "").strip()
            jid = job.get("jobId") or ""
            target = job.get("applyUrl") or ""
            if not scraper.is_http_url(target):
                target = "%s/us/en/job/%s" % (base, jid) if jid else ""
            if not title or not target:
                raise ValueError("Phenom posting is missing its title or public identity")
            if target in local:
                raise ValueError("Phenom repeated a posting during pagination")
            local.add(target)
            if target in seen:
                continue  # A preview or multi-category posting can occur in several facets.
            seen.add(target)
            loc = ", ".join(str(x) for x in (job.get("city"), job.get("state"), job.get("country")) if x)
            row = {"title": title, "url": target, "location": loc or (job.get("cityState") or "")}
            date = str(job.get("postedDate") or job.get("dateCreated") or "")[:10]
            if date:
                row['found_date'] = date
            if jid:
                row.update({"_phenom_jid": jid, "_phenom_origin": base})
            rows.append(row)

    def read(offset, fields):
        requests[0] += 1
        if requests[0] > MAX_REQUESTS:
            raise ValueError("Phenom pagination exceeded its request safety limit")
        body = scraper._phenom_body(offset, PAGE_SIZE)
        body['selected_fields'] = fields
        response = scraper._safe_post(base + "/widgets", body, timeout=25)
        response.raise_for_status()
        data = response.json().get("refineSearch")
        if not isinstance(data, dict) or data.get("status") not in (200, "200"):
            raise ValueError("Phenom public search response is missing or unsuccessful")
        advertised = data.get("totalHits")
        if isinstance(advertised, bool) or not isinstance(advertised, int) or advertised < 0:
            raise ValueError("Phenom search total is missing or invalid")
        jobs = (data.get("data") or {}).get("jobs")
        if not isinstance(jobs, list):
            raise ValueError("Phenom job collection is missing")
        return data, advertised, jobs

    def collect(fields, first=None, expected=None):
        data, total, jobs = first or read(0, fields)
        if expected is not None and total != expected:
            raise ValueError("Phenom listing changed during pagination; retry for a complete snapshot")
        if total > RESULT_WINDOW:
            consume(jobs, set())  # Preserve the readable preview if partitioning later fails.
        if total > MAX_JOBS:
            raise ValueError("Phenom listing exceeds the %d posting safety limit" % MAX_JOBS)
        # Large Elasticsearch-backed tenants return a synthetic empty response
        # beyond their result window. Use the same public facets as the career UI
        # and prove that the union covers the unfiltered advertised total.
        if total > RESULT_WINDOW:
            aggregations = (data.get('data') or {}).get('aggregations') or []
            for field in ('category', 'state', 'country', 'city'):
                if field in fields:
                    continue
                values = next((a.get('value') for a in aggregations if a.get('field') == field), None)
                if not isinstance(values, dict) or not values:
                    continue
                if not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in values.values()):
                    continue
                if sum(values.values()) < total or max(values.values()) >= total:
                    continue
                union = set()
                for value, count in values.items():
                    union.update(collect(dict(fields, **{field:[value]}), expected=count))
                if len(union) != total:
                    raise ValueError("Phenom facet union differs from its advertised total")
                return union
            raise ValueError("Phenom listing exceeds its result window without a complete public facet partition")
        offset, local = 0, set()
        while True:
            if not jobs and offset < total:
                raise ValueError("Phenom pagination ended before its advertised total")
            consume(jobs, local)
            offset += len(jobs)
            if offset >= total:
                if len(local) != total:
                    raise ValueError("Phenom unique posting count differs from its advertised total")
                return local
            time.sleep(0.2)
            data, current_total, jobs = read(offset, fields)
            if current_total != total:
                raise ValueError("Phenom listing changed during pagination; retry for a complete snapshot")
    try:
        catalog = collect({})
        if catalog != seen:
            raise ValueError("Phenom preview and complete facet catalog disagree")
        return rows
    except Exception as exc:
        raise PartialScrapeError(str(exc), rows) from exc
