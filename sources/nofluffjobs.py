import html as htmllib
import json
import re
import time

from .common import fetch

NAME = "nofluffjobs"
BASE = "https://nofluffjobs.com"
MAX_PAGES = 5

STATE_RE = re.compile(r'<script id="serverApp-state" type="application/json">(.*?)</script>', re.S)
LD_JSON_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _extract_state(html_text):
    match = STATE_RE.search(html_text)
    if not match:
        return {}
    try:
        return json.loads(htmllib.unescape(match.group(1)))
    except json.JSONDecodeError:
        return {}


def list_candidate_urls(categories):
    hints = {}
    for category in categories:
        seen_slugs = set()
        page = 1
        while page <= MAX_PAGES:
            try:
                html_text = fetch(f"{BASE}/pl/{category}?page={page}").content.decode(
                    "utf-8", errors="ignore"
                )
            except Exception:
                break
            state = _extract_state(html_text)
            # The Angular hydration cache can hold more than one entry for the
            # same requested page (a stale cumulative one alongside the real
            # single-page one) -- the real one is the smallest of the bunch.
            search_blobs = [
                v for v in state.values()
                if isinstance(v, dict) and "postings" in v and v.get("params", {}).get("page") == page
            ]
            if not search_blobs:
                break
            blob = min(search_blobs, key=lambda v: len(v["postings"]))
            total_pages = blob.get("totalPages", page)

            new_count = 0
            for posting in blob["postings"]:
                slug = posting.get("url")
                if not slug or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                new_count += 1
                places = (posting.get("location") or {}).get("places") or []
                localities = sorted({p.get("city", "") for p in places if p.get("city")})
                hints[f"{BASE}/pl/job/{slug}"] = {
                    "title": posting.get("title", ""),
                    "company": posting.get("name", ""),
                    "localities": list(localities),
                    "is_remote": bool(posting.get("fullyRemote")),
                    "is_b2b": (posting.get("salary") or {}).get("type") == "b2b",
                }

            if new_count == 0 or page >= total_pages:
                break
            page += 1
            time.sleep(0.3)
    return hints


def fetch_offer(url, hint=None):
    hint = hint or {}
    html_text = fetch(url).content.decode("utf-8", errors="ignore")
    match = LD_JSON_RE.search(html_text)
    description = ""
    title = hint.get("title", "")
    company = hint.get("company", "")
    if match:
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            data = None
        if data:
            nodes = data.get("@graph", [data]) if isinstance(data, dict) else []
            job_posting = next((n for n in nodes if n.get("@type") == "JobPosting"), None)
            if job_posting:
                title = title or job_posting.get("title", "")
                description = re.sub(r"<[^>]+>", " ", job_posting.get("description", ""))
                company = company or (job_posting.get("hiringOrganization") or {}).get("name", "")

    if not title and not description:
        return None

    return {
        "source": NAME,
        "title": title,
        "description": description,
        "company": company,
        "localities": hint.get("localities", []),
        "is_remote": hint.get("is_remote", False),
        "is_b2b": hint.get("is_b2b", False),
    }
