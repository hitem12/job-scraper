import json
import re
import xml.etree.ElementTree as ET

from .common import fetch

NAME = "justjoin"
SITEMAP_INDEX_URL = "https://justjoin.it/sitemaps/active-jobs.xml"
LD_JSON_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def _get_locs(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]


def _slug_of(url):
    return url.rsplit("/", 1)[-1].lower()


def list_candidate_urls(prefilter_keywords, target_employers):
    index_xml = fetch(SITEMAP_INDEX_URL).content
    part_urls = _get_locs(index_xml)
    job_urls = []
    for part_url in part_urls:
        job_urls.extend(_get_locs(fetch(part_url).content))

    def is_candidate(url):
        slug = _slug_of(url)
        if slug.endswith("-c"):
            return True
        if any(kw in slug for kw in prefilter_keywords):
            return True
        return any(re.search(rf"(^|-){e}(-|$)", slug) for e in target_employers)

    return [u for u in job_urls if is_candidate(u)]


def fetch_offer(url, hint=None):
    html = fetch(url).content.decode("utf-8", errors="ignore")
    match = LD_JSON_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    title = data.get("title", "")
    description = data.get("description", "")
    company = (data.get("hiringOrganization") or {}).get("name", "")
    location_type = data.get("jobLocationType", "")
    address = (data.get("jobLocation") or {}).get("address") or {}
    locality = address.get("addressLocality", "")
    is_remote = location_type == "TELECOMMUTE"

    base_salary = data.get("baseSalary") or {}
    text = f"{title}\n{description}".lower()
    is_b2b = bool(re.search(r"\bb2b\b", text)) or base_salary.get("unitText") == "HOUR"

    return {
        "source": NAME,
        "title": title,
        "description": description,
        "company": company,
        "localities": [locality] if locality else [],
        "is_remote": is_remote,
        "is_b2b": is_b2b,
    }
