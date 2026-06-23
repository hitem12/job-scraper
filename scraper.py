import argparse
import json
import re
import sys
import time
import webbrowser
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

from profile import PROFILE

SITEMAP_INDEX_URL = "https://justjoin.it/sitemaps/active-jobs.xml"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; personal-job-match-script/1.0)"}
REQUEST_DELAY_SECONDS = 0.3
REQUEST_TIMEOUT = 15

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "data" / "seen_urls.json"
LOG_PATH = BASE_DIR / "matches.log"
MATCHES_STORE_PATH = BASE_DIR / "data" / "matches.json"
MD_PATH = BASE_DIR / "matches.md"

STATUSES = ("new", "interesting", "cv_sent", "not_for_me")
STATUS_LABELS = {
    "new": "New",
    "interesting": "Interesting",
    "cv_sent": "CV sent",
    "not_for_me": "Not for me",
}

LD_JSON_RE = re.compile(
    r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)
LOG_LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\] \(score (?P<score>-?\d+)\) (?P<title>.+?) @ (?P<company>.+?)"
    r"(?: \[TARGET EMPLOYER\])? \((?P<where>[^)]+)\) skills: (?P<skills>.+?) -> (?P<url>\S+)$"
)


def get_locs(xml_bytes):
    root = ET.fromstring(xml_bytes)
    return [el.text.strip() for el in root.iter() if el.tag.endswith("loc") and el.text]


def fetch(url):
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def list_active_job_urls():
    index_xml = fetch(SITEMAP_INDEX_URL)
    part_urls = get_locs(index_xml)
    job_urls = []
    for part_url in part_urls:
        job_urls.extend(get_locs(fetch(part_url)))
    return job_urls


def slug_of(url):
    return url.rsplit("/", 1)[-1].lower()


def is_candidate(url):
    slug = slug_of(url)
    if slug.endswith("-c"):
        return True
    for kw in PROFILE["slug_prefilter_keywords"]:
        if kw in slug:
            return True
    for employer in PROFILE["target_employers"]:
        if re.search(rf"(^|-){employer}(-|$)", slug):
            return True
    return False


def text_contains(keyword, text):
    if re.fullmatch(r"[a-z0-9]+", keyword):
        return re.search(rf"\b{re.escape(keyword)}\b", text) is not None
    return keyword in text


def find_weighted_hits(text, weights):
    return {kw: weight for kw, weight in weights.items() if text_contains(kw, text)}


def parse_job_page(html):
    match = LD_JSON_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def normalize_title(title):
    return re.sub(r"\s+", " ", title.strip().lower())


def evaluate_offer(data, url):
    title = data.get("title", "")
    description = data.get("description", "")
    text = f"{title}\n{description}".lower()

    positive_hits = find_weighted_hits(text, PROFILE["skill_weights"])
    negative_hits = find_weighted_hits(text, PROFILE["negative_weights"])
    score = sum(positive_hits.values()) + sum(negative_hits.values())

    company = (data.get("hiringOrganization") or {}).get("name", "")
    is_target_employer = company.lower() in PROFILE["target_employers"]
    if is_target_employer:
        score += PROFILE["target_employer_bonus"]

    location_type = data.get("jobLocationType", "")
    address = ((data.get("jobLocation") or {}).get("address") or {})
    locality = address.get("addressLocality", "")

    is_remote = PROFILE["location"]["remote_ok"] and location_type == "TELECOMMUTE"
    is_target_city = locality.lower() in PROFILE["location"]["city_keywords"]
    location_match = is_remote or is_target_city

    base_salary = data.get("baseSalary") or {}
    is_b2b = text_contains("b2b", text) or base_salary.get("unitText") == "HOUR"
    if is_b2b:
        score += PROFILE["b2b_bonus"]

    is_match = location_match and score >= PROFILE["score_threshold"]

    return {
        "url": url,
        "title": title,
        "company": company,
        "locality": locality or location_type,
        "remote": is_remote,
        "b2b": is_b2b,
        "score": score,
        "positive_hits": positive_hits,
        "negative_hits": negative_hits,
        "is_target_employer": is_target_employer,
        "is_match": is_match,
    }


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def append_matches_log(matches):
    if not matches:
        return
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = []
    for m in sorted(matches, key=lambda m: m["score"], reverse=True):
        skills = ", ".join(m["positive_hits"].keys()) or "-"
        tag = " [TARGET EMPLOYER]" if m["is_target_employer"] else ""
        where = "remote" if m["remote"] else m["locality"]
        lines.append(
            f"[{timestamp}] (score {m['score']}) {m['title']} @ {m['company']}{tag} "
            f"({where}) skills: {skills} -> {m['url']}"
        )
    with LOG_PATH.open("a") as f:
        f.write("\n".join(lines) + "\n")


def load_matches_store():
    if MATCHES_STORE_PATH.exists():
        return json.loads(MATCHES_STORE_PATH.read_text())
    # First run after upgrading: rebuild from the existing plain-text log
    # so previously found matches show up in the browser UI too.
    store = {}
    if LOG_PATH.exists():
        for line in LOG_PATH.read_text().splitlines():
            m = LOG_LINE_RE.match(line)
            if not m:
                continue
            skills = [s.strip() for s in m.group("skills").split(",") if s.strip() and s.strip() != "-"]
            where = m.group("where")
            store[m.group("url")] = {
                "title": m.group("title"),
                "company": m.group("company"),
                "where": where,
                "remote": where == "remote",
                "b2b": False,
                "score": int(m.group("score")),
                "skills": skills,
                "flags": [],
                "target_employer": "[TARGET EMPLOYER]" in line,
                "first_seen": m.group("ts"),
                "status": "new",
                "notes": "",
                "cv_sent_at": None,
                "click_count": 0,
            }
    return store


def save_matches_store(store):
    MATCHES_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MATCHES_STORE_PATH.write_text(json.dumps(store, indent=2))


STATUS_PRIORITY = {"cv_sent": 3, "interesting": 2, "not_for_me": 1, "new": 0}


def dedup_store(store):
    groups = {}
    for url, m in store.items():
        key = (m["company"].strip().lower(), normalize_title(m["title"]))
        groups.setdefault(key, []).append((url, m))

    def location_rank(entry):
        where = entry[1].get("where", "")
        if where == "remote":
            return 0
        if where.lower() in PROFILE["location"]["city_keywords"]:
            return 1
        return 2

    deduped = {}
    for entries in groups.values():
        if len(entries) == 1:
            url, m = entries[0]
            deduped[url] = m
            continue
        # Prefer whichever duplicate has the most "advanced" human-applied
        # status, tie-broken by location, so notes/cv_sent_at/click_count
        # travel with the duplicate the user actually interacted with.
        best_status = max(
            (m.get("status", "new") for _, m in entries),
            key=lambda s: STATUS_PRIORITY.get(s, 0),
        )
        candidates = [e for e in entries if e[1].get("status", "new") == best_status]
        candidates.sort(key=location_rank)
        url, best = candidates[0]
        merged = dict(best)
        merged["click_count"] = sum(e[1].get("click_count", 0) for e in entries)
        other_locations = sorted({e[1]["where"] for e in entries if e[1]["where"] != best["where"]})
        if other_locations:
            merged["also_in"] = other_locations
        deduped[url] = merged
    return deduped


def update_matches_store(new_matches):
    store = load_matches_store()
    now = datetime.now().isoformat(timespec="seconds")
    for m in new_matches:
        store[m["url"]] = {
            "title": m["title"],
            "company": m["company"],
            "where": "remote" if m["remote"] else m["locality"],
            "remote": m["remote"],
            "b2b": m["b2b"],
            "score": m["score"],
            "skills": sorted(m["positive_hits"].keys()),
            "flags": sorted(m["negative_hits"].keys()),
            "target_employer": m["is_target_employer"],
            "first_seen": now,
            "status": "new",
            "notes": "",
            "cv_sent_at": None,
            "click_count": 0,
        }
    store = dedup_store(store)
    save_matches_store(store)


def export_markdown(store, path=MD_PATH):
    by_status = {s: [] for s in STATUSES}
    for url, m in store.items():
        by_status.setdefault(m.get("status", "new"), []).append((url, m))

    lines = [
        "# Job Matches Export",
        "",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "Source: justjoin.it, matched against the profile in profile.py",
        f"Total: {len(store)} offers",
        "",
    ]
    for status in STATUSES:
        entries = sorted(
            by_status.get(status, []),
            key=lambda kv: kv[1].get("score", 0),
            reverse=True,
        )
        lines.append(f"## {STATUS_LABELS[status]} ({len(entries)})")
        lines.append("")
        if not entries:
            lines.append("_None._")
            lines.append("")
            continue
        for url, m in entries:
            lines.append(f"### {m['title']} — {m['company']} (score: {m.get('score', 0)})")
            where = m["where"]
            if m.get("also_in"):
                where += f" (+{len(m['also_in'])} other location(s): {', '.join(m['also_in'])})"
            lines.append(f"- Location: {where}")
            lines.append(f"- B2B: {'Yes' if m.get('b2b') else 'Unclear/No'}")
            lines.append(f"- Skills matched: {', '.join(m.get('skills', [])) or '-'}")
            if m.get("flags"):
                lines.append(f"- Caution flags: {', '.join(m['flags'])}")
            lines.append(f"- Target employer: {'Yes' if m['target_employer'] else 'No'}")
            lines.append(f"- First seen: {m['first_seen']}")
            if m.get("cv_sent_at"):
                lines.append(f"- CV sent at: {m['cv_sent_at']}")
            if m.get("notes"):
                lines.append(f"- Notes: {m['notes']}")
            lines.append(f"- Link: {url}")
            lines.append("")
    path.write_text("\n".join(lines))


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Open every newly matched offer in the default web browser.",
    )
    parser.add_argument(
        "--export-md",
        action="store_true",
        help="Write all matches, grouped by status, to matches.md for easy LLM analysis.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    state = load_state()

    print("Fetching active job sitemap...", file=sys.stderr)
    all_urls = list_active_job_urls()
    candidates = [u for u in all_urls if is_candidate(u)]
    to_process = [u for u in candidates if u not in state]
    print(
        f"{len(all_urls)} active offers, {len(candidates)} candidates, "
        f"{len(to_process)} new to check.",
        file=sys.stderr,
    )

    total = len(to_process)
    new_matches = []
    for i, url in enumerate(to_process, 1):
        print(
            f"\r[{i}/{total}] checking offers... ({len(new_matches)} matches so far)",
            end="",
            file=sys.stderr,
            flush=True,
        )
        try:
            html = fetch(url).decode("utf-8", errors="ignore")
            data = parse_job_page(html)
            if data is None:
                state[url] = {
                    "checked_at": datetime.now().isoformat(timespec="seconds"),
                    "matched": False,
                    "no_ld_json": True,
                }
                continue
            result = evaluate_offer(data, url)
            state[url] = {
                "checked_at": datetime.now().isoformat(timespec="seconds"),
                "matched": result["is_match"],
            }
            if result["is_match"]:
                new_matches.append(result)
                print(
                    f"\n  match (score {result['score']}): {result['title']} @ {result['company']}",
                    file=sys.stderr,
                )
        except requests.RequestException as exc:
            print(f"\nFailed to fetch {url}: {exc}", file=sys.stderr)
        time.sleep(REQUEST_DELAY_SECONDS)
    if total:
        print(file=sys.stderr)

    append_matches_log(new_matches)
    update_matches_store(new_matches)
    save_state(state)

    new_matches.sort(key=lambda m: m["score"], reverse=True)
    print(f"Found {len(new_matches)} new matching offer(s).", file=sys.stderr)
    for m in new_matches:
        print(f"  - (score {m['score']}) {m['title']} @ {m['company']} -> {m['url']}")

    if args.open_browser:
        for m in new_matches:
            webbrowser.open(m["url"])

    if args.export_md:
        export_markdown(load_matches_store())
        print(f"Wrote {MD_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
