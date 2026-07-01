import json
import re
import xml.etree.ElementTree as ET

from .common import fetch

LD_JSON_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def get_locs(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]


def slug_of(url):
    return url.rsplit("/", 1)[-1].lower()


def discover_via_sitemap(sitemap_index_url, prefilter_keywords, target_employers, slug_endswith=None):
    index_xml = fetch(sitemap_index_url).content
    part_urls = get_locs(index_xml)
    job_urls = []
    for part_url in part_urls:
        job_urls.extend(get_locs(fetch(part_url).content))

    def is_candidate(url):
        slug = slug_of(url)
        if slug_endswith and slug.endswith(slug_endswith):
            return True
        if any(kw in slug for kw in prefilter_keywords):
            return True
        return any(re.search(rf"(^|-){e}(-|$)", slug) for e in target_employers)

    return [u for u in job_urls if is_candidate(u)]


def fetch_jsonld_offer(url, source_name, ld_json_re=LD_JSON_RE):
    """Sites running the schema.org JobPosting JSON-LD pattern shared by
    justjoin.it, rocketjobs.pl, and solid.jobs."""
    html = fetch(url).content.decode("utf-8", errors="ignore")
    match = ld_json_re.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    if isinstance(data, dict) and data.get("@graph"):
        data = next((n for n in data["@graph"] if n.get("@type") == "JobPosting"), data)
    if not isinstance(data, dict) or data.get("@type") != "JobPosting":
        return None

    title = data.get("title", "")
    description = re.sub(r"<[^>]+>", " ", data.get("description", ""))
    company = (data.get("hiringOrganization") or {}).get("name", "")
    location_type = data.get("jobLocationType", "")
    address = (data.get("jobLocation") or {}).get("address") or {}
    locality = address.get("addressLocality", "")
    is_remote = location_type == "TELECOMMUTE"

    salary_value = (data.get("baseSalary") or {}).get("value") or {}
    text = f"{title}\n{description}".lower()
    is_b2b = (
        bool(re.search(r"\bb2b\b", text))
        or salary_value.get("unitText") in ("HOUR", "Day")
        or data.get("employmentType") == "CONTRACTOR"
    )

    return {
        "source": source_name,
        "title": title,
        "description": description,
        "company": company,
        "localities": [locality] if locality else [],
        "is_remote": is_remote,
        "is_b2b": is_b2b,
    }
