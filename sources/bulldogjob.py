import gzip
import json
import re

from .common import fetch
from .jsonld_sitemap import get_locs

NAME = "bulldogjob"
SITEMAP_INDEX_URL = "https://bulldogjob.pl/sitemap.xml.gz"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _fetch_gz_xml(url):
    return gzip.decompress(fetch(url).content)


def list_candidate_urls(prefilter_keywords, target_employers):
    jobs_sitemap_url = next(
        (loc for loc in get_locs(_fetch_gz_xml(SITEMAP_INDEX_URL)) if "jobs.xml" in loc),
        None,
    )
    if not jobs_sitemap_url:
        return []

    job_urls = get_locs(_fetch_gz_xml(jobs_sitemap_url))

    def is_candidate(url):
        slug = url.rsplit("/", 1)[-1].lower()
        if any(kw in slug for kw in prefilter_keywords):
            return True
        return any(re.search(rf"(^|-){e}(-|$)", slug) for e in target_employers)

    return [u for u in job_urls if is_candidate(u)]


def fetch_offer(url, hint=None):
    html = fetch(url).content.decode("utf-8", errors="ignore")
    match = NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    job = (data.get("props") or {}).get("pageProps", {}).get("data", {}).get("job")
    if not job:
        return None

    title = job.get("position", "")
    company = (job.get("company") or {}).get("name", "")
    localities = sorted({
        (loc.get("location") or {}).get("cityEn", "")
        for loc in (job.get("locations") or [])
        if (loc.get("location") or {}).get("cityEn")
    })

    text_parts = [
        job.get("offer", ""),
        job.get("requirements", ""),
        ", ".join(job.get("technologyTags") or []),
    ]
    description = re.sub(r"<[^>]+>", " ", "\n".join(t for t in text_parts if t))

    is_remote = bool(job.get("remote"))
    is_b2b = bool(job.get("contractB2b"))

    return {
        "source": NAME,
        "title": title,
        "description": description,
        "company": company,
        "localities": list(localities),
        "is_remote": is_remote,
        "is_b2b": is_b2b,
    }
