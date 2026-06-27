import json
import re
import xml.etree.ElementTree as ET

from .common import fetch

NAME = "theprotocol"
SITEMAP_INDEX_URL = "https://theprotocol.it/sitemaps/CurrentOffers/sitemap_index.xml"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _get_locs(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]


def _slug_of(url):
    # URLs look like .../szczegoly/praca/<slug>,oferta,<guid>
    tail = url.rsplit("/", 1)[-1].lower()
    return tail.split(",")[0]


def list_candidate_urls(prefilter_keywords, target_employers):
    index_xml = fetch(SITEMAP_INDEX_URL).content
    part_urls = _get_locs(index_xml)
    job_urls = []
    for part_url in part_urls:
        job_urls.extend(_get_locs(fetch(part_url).content))

    def is_candidate(url):
        slug = _slug_of(url)
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

    offer = (data.get("props") or {}).get("pageProps", {}).get("offer")
    if not offer:
        return None
    attrs = offer.get("attributes") or {}

    title = (attrs.get("title") or {}).get("value", "")
    company = (attrs.get("employer") or {}).get("name", "")

    localities = sorted({
        wp.get("city", "") for wp in (attrs.get("workplaces") or []) if wp.get("city")
    })

    employment = attrs.get("employment") or {}
    work_modes = employment.get("detailedWorkModes") or []
    is_remote = any(wm.get("code") == "home-office" for wm in work_modes)

    contracts = employment.get("typesOfContracts") or []
    is_b2b = any("b2b" in (c.get("name") or "").lower() for c in contracts)

    text_sections = offer.get("textSections") or []
    description = "\n".join(s.get("plainText", "") for s in text_sections)

    return {
        "source": NAME,
        "title": title,
        "description": description,
        "company": company,
        "localities": list(localities),
        "is_remote": is_remote,
        "is_b2b": is_b2b,
    }
