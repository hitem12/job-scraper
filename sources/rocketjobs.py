from .jsonld_sitemap import discover_via_sitemap, fetch_jsonld_offer

NAME = "rocketjobs"
SITEMAP_INDEX_URL = "https://rocketjobs.pl/sitemaps/active-jobs.xml"


def list_candidate_urls(prefilter_keywords, target_employers):
    return discover_via_sitemap(SITEMAP_INDEX_URL, prefilter_keywords, target_employers)


def fetch_offer(url, hint=None):
    return fetch_jsonld_offer(url, NAME)
