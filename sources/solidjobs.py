import re

from .common import fetch
from .jsonld_sitemap import fetch_jsonld_offer, get_locs

NAME = "solidjobs"
SITEMAP_INDEX_URL = "https://solid.jobs/sitemap.xml"
LD_JSON_RE = re.compile(r'<script id="structured-data-jsonld"[^>]*>(.*?)</script>', re.S)


def list_candidate_urls(prefilter_keywords, target_employers):
    offers_sitemap_url = next(
        (loc for loc in get_locs(fetch(SITEMAP_INDEX_URL).content) if "sitemap-offers" in loc),
        None,
    )
    if not offers_sitemap_url:
        return []

    job_urls = get_locs(fetch(offers_sitemap_url).content)

    def is_candidate(url):
        slug = url.rsplit("/", 1)[-1].lower()
        if any(kw in slug for kw in prefilter_keywords):
            return True
        return any(re.search(rf"(^|-){e}(-|$)", slug) for e in target_employers)

    return [u for u in job_urls if is_candidate(u)]


def fetch_offer(url, hint=None):
    return fetch_jsonld_offer(url, NAME, ld_json_re=LD_JSON_RE)
